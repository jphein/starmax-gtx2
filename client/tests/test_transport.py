"""Transport-level notification de-duplication (issue #14 dup-delivery fix).

The watch delivers every GATT notification EXACTLY TWICE, back-to-back. Dropping the
duplicate before reassembly stops the "orphan continuation" errors — but must NOT drop
genuine repeat fragments: a large binary health record has runs of byte-identical
fragments (empty samples encode as `ff 00` …), so two DISTINCT consecutive fragments can
be byte-equal (seen live as a run of 6 identical PDUs = 3 real fragments each doubled).

Also covers the ATT-MTU acquisition path (issue #30): the transport MUST force the MTU
exchange after connect so D-plane frames go out as single PDUs, and MUST degrade to the
23-byte floor (still wire-correct) when the backend won't raise it.
"""
import asyncio

from starmax_client import framing
from starmax_client.commands import files
from starmax_client.transport import (CMD_NOTIFY, CMD_WRITE, StarmaxClient,
                                       _NotifyDedup)


def _double(pdus):
    """The real link behaviour: each notification is delivered twice, consecutively."""
    out = []
    for p in pdus:
        out += [p, p]
    return out


def _refragment(frame: bytes, mtu: int, seq: int) -> list:
    pdus = [frame[:mtu]]
    rest, step = frame[mtu:], mtu - 2
    chunks = [rest[i:i + step] for i in range(0, len(rest), step)]
    for i, ch in enumerate(chunks):
        typ = framing.CONT if i == len(chunks) - 1 else framing.MIDDLE
        pdus.append(bytes([typ, seq]) + ch)
    return pdus


# --------------------------------------------------------------- unit: skip-toggle
def test_dedup_plain_pairs():
    d = _NotifyDedup()
    assert [p for p in [b"A", b"A", b"B", b"B"] if d.accept(p)] == [b"A", b"B"]


def test_dedup_preserves_genuine_identical_fragments():
    # THE danger case: N genuine identical consecutive fragments, each doubled (=> 2N PDUs)
    # must yield exactly N — a naive "drop == previous" would collapse them to 1 (data loss).
    d2 = _NotifyDedup()
    assert [p for p in [b"Y"] * 4 if d2.accept(p)] == [b"Y", b"Y"]        # 2 real -> 2
    d3 = _NotifyDedup()
    assert [p for p in [b"X"] * 6 if d3.accept(p)] == [b"X", b"X", b"X"]  # 3 real -> 3 (live run-of-6)


def test_dedup_self_heals_lost_duplicate():
    # B's duplicate lost in transit -> still recovered (mismatch re-arms).
    d = _NotifyDedup()
    assert [p for p in [b"A", b"A", b"B", b"C", b"C"] if d.accept(p)] == [b"A", b"B", b"C"]


def test_dedup_reset():
    d = _NotifyDedup()
    assert d.accept(b"A") is True
    assert d.accept(b"A") is False   # armed -> duplicate dropped
    d.reset()
    assert d.accept(b"A") is True     # fresh session: first copy kept again


# --------------------------------------------------------------- end-to-end vs reassembler
def test_doubled_delivery_orphans_without_dedup_but_reassembles_with_it():
    # A large 0x0e flag=1 binary record with an all-identical body => every continuation
    # fragment is byte-identical (the maximal 'ff 00' empty-sample run). LEN == total.
    total = 900
    hdr = bytes([framing.SOF, 0x82, framing.DIR_WATCH_TO_APP, framing.PROTO_VER,
                 1, framing.OP_HEALTH_SYNC, total & 0xFF, (total >> 8) & 0xFF, 0, 0, 0])
    frame = hdr + bytes(total - len(hdr))          # all-zero body
    assert (frame[6] | (frame[7] << 8)) == total   # LEN == total
    pdus = _refragment(frame, 240, seq=0x82)
    conts = pdus[1:]
    assert any(conts[i] == conts[i + 1] for i in range(len(conts) - 1))  # identical neighbours exist

    # WITHOUT dedup: the doubled stream orphans in the reassembler (reproduces the live bug).
    r_raw = framing.Reassembler(direction=framing.DIR_WATCH_TO_APP)
    orph = 0
    for p in _double(pdus):
        try:
            r_raw.feed(p)
        except framing.FrameError as e:
            if "orphan" in str(e):
                orph += 1
    assert orph > 0

    # WITH dedup: the same doubled stream reassembles to exactly the original frame, no orphan.
    d = _NotifyDedup()
    r = framing.Reassembler(direction=framing.DIR_WATCH_TO_APP)
    done, orph2 = [], 0
    for p in _double(pdus):
        if not d.accept(p):
            continue
        try:
            done += r.feed(p)
        except framing.FrameError:
            orph2 += 1
    assert orph2 == 0
    assert len(done) == 1 and done[0].raw == frame   # byte-exact: no fragment lost


# =============================================================================
# ATT-MTU acquisition (issue #30) — the OTA push crawls at the 23-byte floor unless
# _acquire_mtu() forces the real (247) MTU. bleak 3.x moved _acquire_mtu onto the client's
# *backend*; the old code called it on the wrapper, so it silently AttributeError'd and the
# MTU was never raised. These lock in: (1) the exchange is attempted after connect, on either
# the wrapper (bleak <3.0) or the backend (bleak 3.x); (2) the char max-write fallback; (3) a
# graceful degrade to the 20-byte floor; and (4) that a low MTU leaves the D2 chunk plan intact.
# =============================================================================
class _FakeChar:
    def __init__(self, max_write=0):
        self.max_write_without_response_size = max_write


class _FakeServices:
    """Stand-in GATT collection. ``raise_on_get`` proves the char-lookup guard."""

    def __init__(self, char, raise_on_get=False):
        self._char = char
        self._raise = raise_on_get

    def get_characteristic(self, uuid):
        if self._raise:
            raise RuntimeError("services not resolved")
        return self._char


class _FakeBackend:
    def __init__(self, acquire):
        self._acquire_mtu = acquire


class _FakeBleak:
    """Configurable stand-in for a connected bleak client.

    ``acquire_on``: 'wrapper' (bleak <3.0) | 'backend' (bleak 3.x) | None (absent).
    A successful acquire installs ``acquire_sets`` as ``mtu_size`` (like the real exchange);
    ``acquire_raises`` makes it blow up mid-exchange.
    """

    def __init__(self, *, acquire_on="backend", initial_mtu=23, acquire_sets=247,
                 acquire_raises=False, char_max_write=0, services_raise=False):
        self.mtu_size = initial_mtu
        self.calls = []
        self.services = _FakeServices(_FakeChar(char_max_write), raise_on_get=services_raise)

        async def _do_acquire():
            self.calls.append("acquire")
            if acquire_raises:
                raise RuntimeError("AcquireWrite failed")
            self.mtu_size = acquire_sets

        if acquire_on == "wrapper":
            self._acquire_mtu = _do_acquire
        elif acquire_on == "backend":
            self._backend = _FakeBackend(_do_acquire)


def _client_with(fake) -> StarmaxClient:
    c = StarmaxClient("00:11:22:33:44:55")
    c._client = fake
    return c


def test_acquire_att_mtu_uses_backend_method_bleak3x():
    # bleak 3.x: _acquire_mtu lives on the backend; the wrapper does NOT expose it.
    fake = _FakeBleak(acquire_on="backend", acquire_sets=247)
    c = _client_with(fake)
    payload = asyncio.run(c._acquire_att_mtu())
    assert fake.calls == ["acquire"]                # the exchange was actually forced...
    assert fake.mtu_size == 247                     # ...raising the MTU off the 23 floor
    assert payload == 244 == c._mtu_payload         # 247 - 3-byte ATT header


def test_acquire_att_mtu_uses_wrapper_method_bleak2x():
    # pre-3.0 layout: _acquire_mtu on the wrapper — must still be found and awaited.
    fake = _FakeBleak(acquire_on="wrapper", acquire_sets=185)
    c = _client_with(fake)
    payload = asyncio.run(c._acquire_att_mtu())
    assert fake.calls == ["acquire"]
    assert payload == 182 == c._mtu_payload


def test_acquire_att_mtu_char_maxwrite_fallback():
    # Live-verified BlueZ quirk: mtu_size still reports 23 but the D-Bus char MTU property
    # (surfaced as max_write_without_response_size) is the real 247-3=244 — trust the larger.
    fake = _FakeBleak(acquire_on=None, initial_mtu=23, char_max_write=244)
    c = _client_with(fake)
    payload = asyncio.run(c._acquire_att_mtu())
    assert fake.calls == []                         # no acquire method present -> not called
    assert payload == 244 == c._mtu_payload         # rescued by the char max-write fallback


def test_acquire_att_mtu_degrades_when_acquire_raises():
    # The exact #30 failure mode (AttributeError under bleak 3.x) generalised: acquire blows up
    # and nothing else raises MTU -> we clamp to the 20-byte floor and DO NOT propagate.
    fake = _FakeBleak(acquire_on="backend", acquire_raises=True, initial_mtu=23, char_max_write=0)
    c = _client_with(fake)
    payload = asyncio.run(c._acquire_att_mtu())
    assert fake.calls == ["acquire"]                # attempted...
    assert payload == 20 == c._mtu_payload          # ...failed, floored, still usable (correct)


def test_acquire_att_mtu_degrades_when_no_acquire_anywhere():
    # Non-BlueZ backend: neither wrapper nor backend exposes _acquire_mtu, no char fallback.
    fake = _FakeBleak(acquire_on=None, initial_mtu=23, char_max_write=0)
    c = _client_with(fake)
    payload = asyncio.run(c._acquire_att_mtu())
    assert fake.calls == []
    assert payload == 20 == c._mtu_payload


def test_acquire_att_mtu_guards_char_lookup():
    # If services aren't resolvable the char lookup must be swallowed, not crash the connect.
    fake = _FakeBleak(acquire_on="backend", acquire_sets=247, services_raise=True)
    c = _client_with(fake)
    payload = asyncio.run(c._acquire_att_mtu())
    assert payload == 244 == c._mtu_payload         # acquired MTU still used despite the char miss


def test_acquire_att_mtu_no_client_is_noop():
    c = StarmaxClient("00:11:22:33:44:55")          # never connected -> _client is None
    assert asyncio.run(c._acquire_att_mtu()) == 20  # returns the conservative default, no crash


# --------------------------------------------------------------- connect() ordering
class _ConnectFakeClient:
    """Fake bleak.BleakClient for connect(): records call order; acquire raises MTU via backend."""

    instances = []

    def __init__(self, device, timeout=None):
        self.device = device
        self.timeout = timeout
        self.order = []
        self.mtu_size = 23
        self.services = _FakeServices(_FakeChar(0))
        parent = self

        class _B:
            async def _acquire_mtu(self_b):
                parent.order.append("acquire")
                parent.mtu_size = 247

        self._backend = _B()
        _ConnectFakeClient.instances.append(self)

    async def connect(self):
        self.order.append("connect")

    async def start_notify(self, uuid, cb):
        self.order.append(("start_notify", uuid))

    async def stop_notify(self, uuid):
        self.order.append("stop_notify")

    async def disconnect(self):
        self.order.append("disconnect")


class _FakeScanner:
    @staticmethod
    async def find_device_by_address(address, timeout=None):
        return None                                  # connect() falls back to the address string


def test_connect_acquires_mtu_after_connect_before_notify(monkeypatch):
    import bleak
    _ConnectFakeClient.instances.clear()
    monkeypatch.setattr(bleak, "BleakClient", _ConnectFakeClient)
    monkeypatch.setattr(bleak, "BleakScanner", _FakeScanner)

    c = StarmaxClient("00:11:22:33:44:55")
    asyncio.run(c.connect())

    fake = _ConnectFakeClient.instances[-1]
    assert "acquire" in fake.order, "connect() must force the ATT-MTU exchange"
    # ordering contract: connect -> acquire MTU -> start_notify (MTU set before any write path)
    assert fake.order.index("connect") < fake.order.index("acquire")
    assert fake.order.index("acquire") < fake.order.index(("start_notify", CMD_NOTIFY))
    assert c._mtu_payload == 244                     # 247 negotiated -> single-PDU D-plane frames


# --------------------------------------------------------------- MTU-independent chunk plan
def test_low_mtu_still_yields_correct_chunk_plan():
    # Acceptance rider (#30): degrading to the 23-byte floor must NOT change the D2 chunk plan —
    # the plan is fixed by CHUNK_MAX (234), independent of the link MTU. Data still tiles exactly.
    fake = _FakeBleak(acquire_on=None, initial_mtu=23, char_max_write=0)
    c = _client_with(fake)
    assert asyncio.run(c._acquire_att_mtu()) == 20   # floored MTU

    image = bytes(range(256)) * 9 + b"\x00" * 7      # 2311 B -> ceil(2311/234) = 10 chunks
    d3, d1, *d2s, d4 = files.plan_bulk_transfer(files.FILE_FIRMWARE, image)
    assert d3[0] == 0xD3 and d1[0] == 0xD1 and d4[0] == 0xD4
    assert len(d2s) == 10
    assert all(f[0] == 0xD2 and len(f) - 2 <= files.CHUNK_MAX for f in d2s)   # every chunk within cap
    assert b"".join(f[2:] for f in d2s) == image     # chunks tile the whole image, in order
    # and the whole-file finalize CRC is unchanged by the MTU
    import struct
    assert struct.unpack_from("<I", d4, 3)[0] & 0xFFFF == files.crc16_xmodem(image)
