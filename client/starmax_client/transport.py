"""Custom command-service transport for the GTX2, built on bleak.

    Service : 00000FF0-0000-1000-8000-00805F9B34FB — custom 16-bit GATT service 0x0FF0
              (NOT Nordic UART; verified vs live GATT discovery + the capture). The SDK/APK
              references NUS-style UUIDs, but the GTX2 firmware exposes 0x0FF0.
    Write   : 00000001-...  ATT handle 0x0026 (app->watch, Write Without Response)
    Notify  : 00000002-...  ATT handle 0x0028 (watch->app, Handle Value Notification)

Everything here is async. The codec (framing/commands) is transport-independent and fully
unit-tested offline; this module only moves bytes over BLE and reassembles inbound frames.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, List, Optional, Tuple

from . import framing
from .framing import Frame

log = logging.getLogger("starmax_client.transport")

# Custom command service 0x0FF0 (verified vs live GATT discovery + capture) — NOT Nordic UART.
# The SDK/APK references NUS-style UUIDs, but the GTX2 firmware exposes this service instead.
CMD_SERVICE = "00000ff0-0000-1000-8000-00805f9b34fb"
CMD_WRITE = "00000001-0000-1000-8000-00805f9b34fb"   # app -> watch, ATT handle 0x0026
CMD_NOTIFY = "00000002-0000-1000-8000-00805f9b34fb"  # watch -> app, ATT handle 0x0028

DEFAULT_NAME_PREFIX = "GTX2"


class SeqCounter:
    """8-bit sequence counter for outbound frames (wraps 1..255, skips 0)."""

    def __init__(self, start: int = 1) -> None:
        self._n = start & 0xFF

    def next(self) -> int:
        n = self._n
        self._n = (self._n + 1) & 0xFF
        if self._n == 0:
            self._n = 1
        return n


class _NotifyDedup:
    """Collapse the watch's back-to-back duplicate notifications WITHOUT losing real data.

    On this link every GATT notification is delivered EXACTLY TWICE, consecutively (verified
    on a live sync: strict pairwise, ``raw[2k] == raw[2k+1]`` for all k). A naive "drop a PDU
    equal to the previous raw one" is UNSAFE: a large binary health record contains runs of
    byte-identical fragments (empty samples encode as ``ff 00`` …), so two *distinct*
    consecutive fragments can be byte-equal — seen live as a run of 6 identical PDUs = 3
    genuine fragments each doubled. Collapsing that run would silently delete real data.

    Skip-toggle: keep a PDU, then drop the NEXT one only when it equals the last KEPT PDU
    (i.e. we are "armed" for that PDU's duplicate). A mismatch re-arms, self-healing a lost
    duplicate. So N genuine identical fragments (⇒ 2N delivered) yield exactly N, while a
    plain doubled pair (X, X) yields one X.
    """

    def __init__(self) -> None:
        self._last: Optional[bytes] = None
        self._armed = False

    def reset(self) -> None:
        self._last = None
        self._armed = False

    def accept(self, pdu: bytes) -> bool:
        """Return True to keep ``pdu``, False to drop it as the 2nd copy of a delivered pair."""
        if self._armed and pdu == self._last:
            self._armed = False          # absorbed the duplicate of the last kept PDU
            return False
        self._last = pdu
        self._armed = True
        return True


async def scan(timeout: float = 8.0, name_prefix: str = DEFAULT_NAME_PREFIX
               ) -> List[Tuple[str, str, Optional[int]]]:
    """Scan for advertising watches. Returns [(name, address, rssi), ...].

    Matches any advertised name starting with ``name_prefix`` (default ``GTX2``). Pass
    ``name_prefix=""`` to list every device.
    """
    from bleak import BleakScanner

    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    results: List[Tuple[str, str, Optional[int]]] = []
    for address, (device, adv) in found.items():
        name = adv.local_name or device.name or ""
        if name_prefix and not name.startswith(name_prefix):
            continue
        results.append((name, address, getattr(adv, "rssi", None)))
    results.sort(key=lambda r: (r[2] is None, -(r[2] or 0)))
    return results


class StarmaxClient:
    """Async BLE session with a GTX2 watch.

    Use as an async context manager::

        async with StarmaxClient(address) as w:
            await w.bind()
            await w.set_time()
    """

    def __init__(self, address: str, *, name_prefix: str = DEFAULT_NAME_PREFIX,
                 connect_timeout: float = 20.0) -> None:
        self.address = address
        self.name_prefix = name_prefix
        self.connect_timeout = connect_timeout
        self._client = None
        self._seq = SeqCounter()
        self._reasm = framing.Reassembler(direction=framing.DIR_WATCH_TO_APP)
        self._dedup = _NotifyDedup()  # drops the watch's doubled notifications (see class doc)
        self._listeners: List[Callable[[Frame], None]] = []
        self._raw_listeners: List[Callable[[bytes], None]] = []  # raw inbound-PDU taps (OTA probe)
        self._inbox: "asyncio.Queue[Frame]" = asyncio.Queue()
        self._mtu_payload = 20  # conservative until connected

    # ---------------------------------------------------------------- lifecycle
    async def __aenter__(self) -> "StarmaxClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        from bleak import BleakClient, BleakScanner

        # Resolve the address to a BLEDevice via a short scan first — on BlueZ, connecting to a
        # freshly-resolved device is far more reliable than a bare address string (avoids the
        # "Device ... not found" error when it is not already in the adapter cache).
        device = await BleakScanner.find_device_by_address(self.address, timeout=self.connect_timeout)
        self._client = BleakClient(device or self.address, timeout=self.connect_timeout)
        await self._client.connect()
        # Acquire the real ATT MTU BEFORE any write. bleak reports the 23-byte floor until the MTU
        # is acquired, which fragments every outbound frame at the 20-byte payload floor — that is
        # what made the D-plane OTA push crawl (issue #30). The GTX2 negotiates 247, matching the
        # vendor app's requestMtu(247). Best-effort: on failure we stay correct, just slower.
        await self._acquire_att_mtu()
        self._dedup.reset()  # fresh de-dup state per session (survives reconnects)
        await self._client.start_notify(CMD_NOTIFY, self._on_notify)
        log.info("connected to %s (mtu=%d, pdu payload=%d)",
                 self.address, getattr(self._client, "mtu_size", 23) or 23, self._mtu_payload)

    async def _acquire_att_mtu(self) -> int:
        """Force the BlueZ ATT-MTU exchange and record the usable per-PDU payload; returns it.

        bleak reports the 23-byte ATT floor until the MTU is acquired — it logs *"Using default
        MTU value. Call _acquire_mtu() or set _mtu_size first"* — so outbound frames fragment at
        the 20-byte floor and the D-plane OTA push (each ≤234-byte D2 chunk) becomes a slow
        multi-PDU long-write (issue #30). ``_acquire_mtu()`` triggers the AcquireWrite/AcquireNotify
        D-Bus exchange so ``mtu_size`` then reflects the real negotiated MTU (the GTX2 negotiates
        247). On BlueZ there is no explicit requestMtu in bleak — the kernel negotiates the max both
        sides support and this call just forces the exchange to complete and be read.

        Version-robustness: in **bleak 3.x** ``_acquire_mtu`` lives on the client's *backend*
        (``client._backend._acquire_mtu``); pre-3.0 it was on the ``BleakClient`` wrapper. The old
        code called it on the wrapper, so under bleak 3.0.2 it raised ``AttributeError`` (silently
        swallowed) and the MTU was never acquired — the root cause of #30. We now try the wrapper
        first, then the backend, so both layouts work.

        Everything here is best-effort: if neither object exposes ``_acquire_mtu`` (a non-BlueZ
        backend) or the exchange fails, we fall back to the write characteristic's
        ``max_write_without_response_size`` (derived from the D-Bus MTU property, which can report
        the real 247 even when ``mtu_size`` still says 23 — live-verified) and finally the 20-byte
        floor. Every fallback path is still wire-correct, just slower.
        """
        client = self._client
        if client is None:
            return self._mtu_payload
        acquire = getattr(client, "_acquire_mtu", None)          # bleak <3.0: on the wrapper
        if acquire is None:
            acquire = getattr(getattr(client, "_backend", None), "_acquire_mtu", None)  # bleak 3.x
        if acquire is not None:
            try:
                await acquire()
            except Exception:  # noqa: BLE001 - MTU acquisition is best-effort
                log.debug("ATT MTU acquire failed; falling back to the char MTU property / floor",
                          exc_info=True)
        # ATT MTU minus the 3-byte ATT write header = usable payload per PDU.
        payload = (getattr(client, "mtu_size", 23) or 23) - 3
        try:
            char = client.services.get_characteristic(CMD_WRITE)
            payload = max(payload, getattr(char, "max_write_without_response_size", 0) or 0)
        except Exception:  # noqa: BLE001 - best-effort; the 20-byte floor always works
            pass
        self._mtu_payload = max(20, payload)
        return self._mtu_payload

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.stop_notify(CMD_NOTIFY)
            except Exception:
                pass
            await self._client.disconnect()
            self._client = None

    # ---------------------------------------------------------------- inbound
    def _on_notify(self, _char, data: bytearray) -> None:
        pdu = bytes(data)
        # The watch delivers every notification twice; drop the duplicate BEFORE reassembly
        # so a >2-PDU record's real fragments aren't stranded as "orphan continuation"
        # errors. _NotifyDedup preserves genuine identical fragments (empty 'ff 00' runs).
        if not self._dedup.accept(pdu):
            return
        # Raw tap (read-only): observe D-plane PDUs (d1/d2/d3/d4 acks+replies) that the C1
        # Reassembler drops — used by the OTA probe to read the D3 resume/state reply. Runs after
        # de-dup, before reassembly; must never affect the normal frame path below.
        for cb in self._raw_listeners:
            try:
                cb(pdu)
            except Exception:  # noqa: BLE001 - a tap must never break inbound processing
                pass
        try:
            frames = self._reasm.feed(pdu)
        except framing.FrameError as e:
            log.warning("dropping malformed notification: %s (%s)", e, pdu.hex())
            return
        for fr in frames:
            log.debug("recv op=0x%02x flag=%d crc_ok=%s len=%d",
                      fr.opcode, fr.flag, fr.crc_ok, len(fr.payload))
            self._inbox.put_nowait(fr)
            for cb in self._listeners:
                cb(fr)

    def add_listener(self, cb: Callable[[Frame], None]) -> None:
        """Register a callback invoked for every fully-reassembled inbound frame."""
        self._listeners.append(cb)

    def add_raw_listener(self, cb: Callable[[bytes], None]) -> None:
        """Register a callback for RAW inbound PDUs, before C1 reassembly (after de-dup).

        The C1 Reassembler drops raw D-plane frames (d1/d2/d3/d4 acks+replies), so a caller that
        must observe them — e.g. the read-only OTA probe reading the D3 resume/state reply — taps
        here. Read-only: taps never affect delivery to :meth:`add_listener` / :meth:`request`.
        """
        self._raw_listeners.append(cb)

    def remove_raw_listener(self, cb: Callable[[bytes], None]) -> None:
        """Unregister a raw-PDU tap added via :meth:`add_raw_listener` (no-op if absent)."""
        try:
            self._raw_listeners.remove(cb)
        except ValueError:
            pass

    # ---------------------------------------------------------------- outbound
    def next_seq(self) -> int:
        return self._seq.next()

    async def send_raw(self, frame: bytes, response: bool = False) -> None:
        """Write a pre-built frame, fragmenting into 0xC1/0xC3 PDUs if it exceeds the MTU.

        ``response=True`` uses ATT Write-With-Response (each PDU acked at the link layer). The
        bulk D-plane (dial/firmware push) needs it: fire-hosing Write-Without-Response overruns
        the watch's receive buffer and drops the tail — live-verified, the watch stopped acking a
        231 KB dial push at ~84 KB, so the finalize CRC failed. Command-plane writes keep the
        default (False)."""
        if self._client is None:
            raise RuntimeError("not connected")
        for pdu in framing.frame_to_pdus(frame, self._mtu_payload):
            await self._client.write_gatt_char(CMD_WRITE, pdu, response=response)

    async def request(self, frame: bytes, expect_opcode: int, *, timeout: float = 5.0
                      ) -> Optional[Frame]:
        """Send ``frame`` and wait for the next inbound frame with ``expect_opcode``."""
        # Drain any stale frames so we match this request's reply.
        while not self._inbox.empty():
            self._inbox.get_nowait()
        await self.send_raw(frame)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                fr = await asyncio.wait_for(self._inbox.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            if fr.opcode == expect_opcode:
                return fr
