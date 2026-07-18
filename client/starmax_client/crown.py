"""Rotary-crown-over-BLE consumer for the *custom* GTX2 firmware.

**Status: [CFW] — targets firmware that DOES NOT EXIST YET.** Stock GTX2 firmware handles the crown
entirely on-device (the encoder scrolls the local UI); it emits **no** crown data over BLE — no
characteristic, no opcode, nothing on the wire. This module implements the client half of the
protocol specified in ``docs/cfw-crown-protocol.md`` so the decoder + framing are complete and
test-green the day a custom firmware (SDK rebuild tapping the ``input_knob`` / keypad events and
adding the 0xA1 opcode) starts emitting frames.

It is the twin of :mod:`starmax_client.rawaccel`: same 0x0FF0 notify plane, same framing, a new
collision-free opcode ``0xA1`` (raw-accel owns ``0xA0``). Everything here is transport-independent
and offline-testable.

Wire summary (full detail: docs/cfw-crown-protocol.md)
------------------------------------------------------
* New C1 opcode **``0xA1`` (CROWN)** — outside the stock 0x01-0x22 namespace, the Java-SDK REV block
  (0x31-0x3C), the SDK realtime toggle (0x70) and the raw-accel opcode (0xA0). Rides the existing
  0x0FF0 write/notify chars — no new GATT characteristic.
* ``flag=0`` **control** (protobuf, both directions): enable / disable / ack.
* ``flag=1`` **data** (watch->app): a fixed 12-byte binary header + N x ``[ev_type:u8][ev_detail:u8]
  [value:i16]`` little-endian events. The data frame is a normal watch->app **CRC-bearing** C1 frame
  (LEN = total-2, CRC-16/CCITT-FALSE trailer) — so the existing :func:`framing.parse_frame` decodes
  and CRC-verifies it with **zero** framing changes; ``frame.payload`` is the binary batch this
  module parses. Crown frames are always single-PDU (no fragmentation).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from . import framing
from .crc import crc16_bytes
from .protobuf import ProtobufWriter, to_dict

# --------------------------------------------------------------------------- opcode / flags
OP_CROWN = 0xA1            # [CFW] custom-firmware crown opcode (see module doc / spec §2)
FLAG_CONTROL = 0x00        # flag=0: control plane (protobuf) — enable/disable/ack
FLAG_DATA = 0x01           # flag=1: data plane (binary event batch, watch->app, CRC-bearing)

# Control-plane command codes (enable request f1)
CMD_STOP = 0
CMD_START = 1

# ACK status codes (ack f1)
ACK_OK = 0
ACK_BUSY = 1
ACK_NO_CROWN = 2           # board has no crown wired (EVB / un-ported board)

# Data-frame payload-format version (header off0)
DATA_VERSION = 1
DATA_HEADER_LEN = 12       # bytes; struct "<BBBBHIH"
EVENT_LEN = 4              # bytes; struct "<BBh" (ev_type, ev_detail, value i16 LE)

# Event types (event record byte 0)
EV_ROTATE = 1              # value = signed step delta (+ clockwise, - counter-clockwise)
EV_BUTTON = 2              # ev_detail = button action code below; value = 0

# Button action codes (ev_detail when ev_type == EV_BUTTON) — mapped 1:1 from the SDK KEY_TYPE_*
BTN_DOWN = 1
BTN_UP = 2
BTN_CLICK = 3              # short press, released
BTN_LONG = 4              # long-press threshold reached
BTN_DOUBLE = 5            # double-click
BTN_LONG_UP = 6          # release after a long-press
BTN_ACTION_NAMES = {
    BTN_DOWN: "down", BTN_UP: "up", BTN_CLICK: "click",
    BTN_LONG: "long", BTN_DOUBLE: "double", BTN_LONG_UP: "long_up",
}


class CrownError(Exception):
    """Raised when a crown data frame cannot be decoded."""


# --------------------------------------------------------------------------- data model
@dataclass
class CrownEvent:
    """One crown event: a rotation delta or a button edge (spec §4.1)."""
    ev_type: int
    ev_detail: int
    value: int

    @property
    def is_rotation(self) -> bool:
        return self.ev_type == EV_ROTATE

    @property
    def is_button(self) -> bool:
        return self.ev_type == EV_BUTTON

    @property
    def rotation_delta(self) -> int:
        """Signed step delta for a rotation event (0 for a button event)."""
        return self.value if self.ev_type == EV_ROTATE else 0

    @property
    def button_action(self) -> Optional[str]:
        """Human name of the button action (``down``/``up``/``click``/…), or ``None`` if not a button."""
        return BTN_ACTION_NAMES.get(self.ev_detail) if self.ev_type == EV_BUTTON else None


@dataclass
class CrownBatch:
    """A decoded data frame: header fields + the batch of events (spec §4)."""
    version: int
    clockwise_positive: bool   # cfg bit0 convention: True = clockwise is a positive delta
    count: int
    frame_seq: int             # per-stream frame counter (wraps 0x10000); for drop detection
    base_ts_ms: int            # device-monotonic ms of events[0]
    events: List[CrownEvent]

    def net_rotation(self) -> int:
        """Sum of every rotation delta in the batch (button events contribute 0)."""
        return sum(e.rotation_delta for e in self.events)

    def button_events(self) -> List[CrownEvent]:
        """Just the button events, in order."""
        return [e for e in self.events if e.is_button]


# --------------------------------------------------------------------------- event constructors
def rotate(delta: int) -> Tuple[int, int, int]:
    """A rotation event tuple: ``+delta`` clockwise, ``-delta`` counter-clockwise."""
    return (EV_ROTATE, 0, delta)


def button(action: int) -> Tuple[int, int, int]:
    """A button event tuple for one of the ``BTN_*`` action codes."""
    if action not in BTN_ACTION_NAMES:
        raise ValueError(f"unsupported button action {action!r}; supported: {sorted(BTN_ACTION_NAMES)}")
    return (EV_BUTTON, action, 0)


# --------------------------------------------------------------------------- control builders
def build_crown_enable(report_rotation: bool = True, report_button: bool = True,
                       coalesce_ms: int = 0, seq: int = 0) -> bytes:
    """[CFW] Build the app->watch ENABLE control frame (spec §3.1). flag=0, protobuf.

    Payload ``f1=CMD_START, f2=report_rotation, f3=report_button, f4=coalesce_ms``. ``coalesce_ms``
    is the rotation coalescing window (0 = emit each detent immediately); the watch clamps
    unsupported values and reports the ACTUAL settings in the ack (spec §3.2).
    """
    if coalesce_ms < 0:
        raise ValueError(f"coalesce_ms must be >= 0, got {coalesce_ms}")
    payload = (ProtobufWriter()
               .varint(1, CMD_START)
               .varint(2, 1 if report_rotation else 0)
               .varint(3, 1 if report_button else 0)
               .varint(4, coalesce_ms)
               .to_bytes())
    return framing.build_command(OP_CROWN, payload, flag=FLAG_CONTROL, seq=seq)


def build_crown_disable(seq: int = 0) -> bytes:
    """[CFW] Build the app->watch DISABLE control frame (spec §3.1). flag=0, ``f1=CMD_STOP``."""
    payload = ProtobufWriter().varint(1, CMD_STOP).to_bytes()
    return framing.build_command(OP_CROWN, payload, flag=FLAG_CONTROL, seq=seq)


# --------------------------------------------------------------------------- control parser
def parse_crown_ack(payload: bytes) -> dict:
    """[CFW] Parse a watch->app control ACK (spec §3.2).

    ``{status, report_rotation, report_button, detents_per_rev, base_ts_ms}`` from protobuf fields
    f1..f5. ``status`` is one of the ``ACK_*`` codes (0 = ok/streaming).
    """
    d = to_dict(payload)
    return {
        "status": d.get(1, 0),
        "report_rotation": d.get(2, 0),
        "report_button": d.get(3, 0),
        "detents_per_rev": d.get(4, 0),
        "base_ts_ms": d.get(5, 0),
    }


# --------------------------------------------------------------------------- data parser
def parse_crown_frame(payload: bytes) -> CrownBatch:
    """[CFW] Decode a data-frame payload (the 12-byte header + N events) into a batch (spec §4).

    ``payload`` is ``frame.payload`` from a parsed watch->app 0xA1 flag=1 frame (the CRC has already
    been verified by :func:`framing.parse_frame`). Raises :class:`CrownError` on an unknown version,
    a bad length, or an event count the body cannot satisfy.
    """
    if len(payload) < DATA_HEADER_LEN:
        raise CrownError(f"crown frame too short for header: {len(payload)} < {DATA_HEADER_LEN}")
    version, cfg, _reserved0, count, frame_seq, base_ts_ms, _reserved1 = struct.unpack_from(
        "<BBBBHIH", payload, 0)
    if version != DATA_VERSION:
        raise CrownError(f"unsupported crown-frame version {version} (expected {DATA_VERSION})")

    body = payload[DATA_HEADER_LEN:]
    need = count * EVENT_LEN
    if len(body) < need:
        raise CrownError(
            f"truncated batch: header count={count} needs {need} bytes, have {len(body)}")

    events: List[CrownEvent] = []
    for i in range(count):
        ev_type, ev_detail, value = struct.unpack_from("<BBh", body, i * EVENT_LEN)
        events.append(CrownEvent(ev_type, ev_detail, value))
    return CrownBatch(version, not (cfg & 0x01), count, frame_seq, base_ts_ms, events)


def detect_drops(seqs: Sequence[int], *, modulo: int = 1 << 16) -> int:
    """Count dropped frames across an ordered sequence of ``frame_seq`` values (spec §4.1).

    Sums the gap between consecutive frame-seqs (modulo wrap). ``[3,4,6]`` -> 1 dropped; a clean run
    -> 0. A single frame or empty input -> 0. (Crown rotation is relative, so a drop just loses that
    delta — there is no absolute state to desync — but the count is still useful for diagnostics.)
    """
    dropped = 0
    for prev, cur in zip(seqs, list(seqs)[1:]):
        dropped += (cur - prev - 1) % modulo
    return dropped


# --------------------------------------------------------------------------- firmware-emulation
def build_crown_data_frame(events: Sequence[Tuple[int, int, int]], *, frame_seq: int = 0,
                           base_ts_ms: int = 0, clockwise_positive: bool = True, seq: int = 0,
                           version: int = DATA_VERSION) -> bytes:
    """[CFW] Build a byte-exact watch->app DATA frame — the firmware's on-wire output (spec §4).

    Reference encoder used by the tests (and by the firmware author as a golden-vector source): a
    watch->app CRC-bearing C1 frame carrying the 12-byte binary header + ``events``. ``events`` is a
    list of ``(ev_type, ev_detail, value)`` tuples (use :func:`rotate` / :func:`button` to build
    them). LEN = header+payload (bytes before the CRC); the CRC-16/CCITT-FALSE trailer is appended so
    the stock :func:`framing.parse_frame` verifies it unchanged.
    """
    cfg = 0x00 if clockwise_positive else 0x01
    count = len(events)
    header = struct.pack("<BBBBHIH", version, cfg, 0, count,
                         frame_seq & 0xFFFF, base_ts_ms & 0xFFFFFFFF, 0)
    body = b"".join(struct.pack("<BBh", t & 0xFF, d & 0xFF, v) for t, d, v in events)
    payload = header + body

    length_field = framing.HEADER_LEN + len(payload)   # bytes up to (not incl.) the CRC
    c1 = bytes([
        framing.SOF,
        (seq & 0xFF) | framing.SEQ_HIGH_BIT,   # watch->app frames OR-in 0x80
        framing.DIR_WATCH_TO_APP,
        framing.PROTO_VER,
        FLAG_DATA,
        OP_CROWN,
        length_field & 0xFF,
        (length_field >> 8) & 0xFF,
        0x00, 0x00, 0x00,
    ])
    frame_wo_crc = c1 + payload
    return frame_wo_crc + crc16_bytes(frame_wo_crc)


def build_crown_ack_frame(*, status: int = ACK_OK, report_rotation: int = 1, report_button: int = 1,
                          detents_per_rev: int = 0, base_ts_ms: int = 0, seq: int = 0) -> bytes:
    """[CFW] Build a byte-exact watch->app control ACK frame (spec §3.2) — reference/test helper.

    A normal watch->app protobuf reply (flag=0, CRC-bearing), so :func:`framing.parse_frame` decodes
    it via the existing path.
    """
    payload = (ProtobufWriter()
               .varint(1, status)
               .varint(2, report_rotation)
               .varint(3, report_button)
               .varint(4, detents_per_rev)
               .varint(5, base_ts_ms)
               .to_bytes())
    length_field = framing.HEADER_LEN + len(payload)
    c1 = bytes([
        framing.SOF,
        (seq & 0xFF) | framing.SEQ_HIGH_BIT,
        framing.DIR_WATCH_TO_APP,
        framing.PROTO_VER,
        FLAG_CONTROL,
        OP_CROWN,
        length_field & 0xFF,
        (length_field >> 8) & 0xFF,
        0x00, 0x00, 0x00,
    ])
    frame_wo_crc = c1 + payload
    return frame_wo_crc + crc16_bytes(frame_wo_crc)


def max_events_per_frame(mtu_payload: int) -> int:
    """Largest event count that fits ONE PDU of ``mtu_payload`` ATT bytes (spec §4.2).

    Budget = mtu_payload - C1 header(11) - data header(12) - CRC(2), floored at 0, div event(4).
    At MTU 247 (mtu_payload=244) this is 54; at the 20-byte floor it is 0. Crown traffic is
    human-paced, so a real frame is always well under this.
    """
    budget = mtu_payload - framing.HEADER_LEN - DATA_HEADER_LEN - 2
    return max(0, budget // EVENT_LEN)
