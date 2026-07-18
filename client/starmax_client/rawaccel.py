"""Raw-accelerometer-over-BLE consumer for the *custom* GTX2 firmware.

**Status: [CFW] — targets firmware that DOES NOT EXIST YET.** The stock GTX2 firmware exposes
**no** raw-accel path (docs/custom-firmware-poc.md Part 3: no realtime receiver, no data model,
nothing to piggyback). This module implements the client half of the protocol specified in
``docs/cfw-rawaccel-protocol.md`` so the decoder + framing are complete and test-green the day a
custom firmware (SDK rebuild adding a LIS2DH12 sampler + the 0xA0 opcode) starts emitting frames.

Everything here is transport-independent and offline-testable: builders return app->watch control
frames (via the shared :mod:`framing`), the parser decodes the binary sample batch, and
:func:`build_rawaccel_data_frame` synthesises a byte-exact watch->app data frame (the firmware's
output) for tests + reference vectors.

Wire summary (full detail: docs/cfw-rawaccel-protocol.md)
--------------------------------------------------------
* New C1 opcode **``0xA0`` (RAW_ACCEL)** — outside the stock 0x01-0x22 namespace, the Java-SDK REV
  block (0x31-0x3C) and the SDK realtime toggle (0x70), so it cannot collide with stock traffic.
  Rides the existing 0x0FF0 write/notify chars — no new GATT characteristic.
* ``flag=0`` **control** (protobuf, both directions): enable / disable / ack.
* ``flag=1`` **data** (watch->app): a fixed 12-byte binary header + N x ``[x:i16][y:i16][z:i16]``
  little-endian samples. The data frame is a normal watch->app **CRC-bearing** C1 frame (LEN =
  total-2, CRC-16/CCITT-FALSE trailer) — so the existing :func:`framing.parse_frame` decodes and
  CRC-verifies it with **zero** framing changes; ``frame.payload`` is the binary batch this module
  parses. Data frames are sized to fit ONE MTU-247 PDU (no fragmentation) for firmware simplicity.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from . import framing
from .crc import crc16_bytes
from .protobuf import ProtobufWriter, to_dict

# --------------------------------------------------------------------------- opcode / flags
OP_RAW_ACCEL = 0xA0        # [CFW] custom-firmware raw-accel opcode (see module doc / spec §2)
FLAG_CONTROL = 0x00        # flag=0: control plane (protobuf) — enable/disable/ack
FLAG_DATA = 0x01           # flag=1: data plane (binary sample batch, watch->app, CRC-bearing)

# Control-plane command codes (enable request f1)
CMD_STOP = 0
CMD_START = 1

# ACK status codes (ack f1)
ACK_OK = 0
ACK_UNSUPPORTED_RATE = 1
ACK_UNSUPPORTED_RANGE = 2
ACK_BUSY = 3
ACK_UNSUPPORTED_RES = 4

# Data-frame payload-format version (header off0)
DATA_VERSION = 1
DATA_HEADER_LEN = 12       # bytes; struct "<BBBBHIH"
SAMPLE_LEN = 6             # bytes; struct "<hhh" (x, y, z i16 LE)

# Supported sample rates (Hz) and their compact data-header codes.
RATE_CODES = {0: 25, 1: 50, 2: 100, 3: 200}
RATE_TO_CODE = {hz: c for c, hz in RATE_CODES.items()}

# Supported full-scale ranges (+/- g) and their data-header codes.
RANGE_CODES = {0: 2, 1: 4, 2: 8, 3: 16}
RANGE_TO_CODE = {g: c for c, g in RANGE_CODES.items()}

# Supported output resolutions (useful bits) and their data-header codes = LIS2DH12 operating mode
# (8=low-power, 10=normal, 12=high-resolution).
RES_CODES = {0: 8, 1: 10, 2: 12}
RES_TO_CODE = {bits: c for c, bits in RES_CODES.items()}

# LIS2DH12 sensitivity, mg per digit, keyed (res_bits, range_g) — ST datasheet
# (DocID026799, Table "Mechanical characteristics"). The wire value is the LEFT-justified 16-bit
# register pair; :func:`to_g` right-shifts to the useful N-bit value first, then applies this.
_SENSITIVITY_MG = {
    (12, 2): 1, (12, 4): 2, (12, 8): 4, (12, 16): 12,   # high-resolution (12-bit)
    (10, 2): 4, (10, 4): 8, (10, 8): 16, (10, 16): 48,  # normal (10-bit)
    (8, 2): 16, (8, 4): 32, (8, 8): 64, (8, 16): 192,   # low-power (8-bit)
}
_RES_SHIFT = {12: 4, 10: 6, 8: 8}   # left-justification shift per operating mode


class RawAccelError(Exception):
    """Raised when a raw-accel data frame cannot be decoded."""


# --------------------------------------------------------------------------- conversion
def to_g(raw: int, range_g: int, res_bits: int) -> float:
    """Convert one LEFT-justified 16-bit LIS2DH12 axis word to g.

    Right-shifts the left-justified word to the useful N-bit value (per operating mode), then
    applies the datasheet mg/digit for (``res_bits``, ``range_g``). Python's ``>>`` on a signed
    int is arithmetic, so the sign is preserved. Raises ``KeyError`` on an unsupported combo.
    """
    shift = _RES_SHIFT[res_bits]
    value = raw >> shift
    return value * _SENSITIVITY_MG[(res_bits, range_g)] / 1000.0


# --------------------------------------------------------------------------- data model
@dataclass
class AccelSample:
    """One tri-axial sample: raw left-justified i16 words + g-converted floats."""
    x: int
    y: int
    z: int
    gx: float
    gy: float
    gz: float


@dataclass
class RawAccelBatch:
    """A decoded data frame: header fields + the batch of samples (spec §4)."""
    version: int
    range_g: int
    rate_hz: Optional[int]     # None if the header rate_code is outside the known set
    res_bits: int
    count: int
    frame_seq: int             # per-stream frame counter (wraps 0x10000); for drop detection
    base_ts_ms: int            # device-monotonic ms of samples[0]
    samples: List[AccelSample]

    def sample_period_ms(self) -> Optional[float]:
        """Inter-sample period in ms from the header rate (``None`` if rate unknown)."""
        return 1000.0 / self.rate_hz if self.rate_hz else None

    def timestamps_ms(self) -> List[float]:
        """Per-sample monotonic-ms timestamps: ``base_ts_ms + k * period`` (empty if rate unknown)."""
        period = self.sample_period_ms()
        if period is None:
            return []
        return [self.base_ts_ms + k * period for k in range(self.count)]


# --------------------------------------------------------------------------- control builders
def _check_choice(name: str, value: int, table) -> int:
    if value not in table:
        raise ValueError(f"unsupported {name} {value!r}; supported: {sorted(table)}")
    return value


def build_rawaccel_enable(rate_hz: int = 50, range_g: int = 8, res_bits: int = 12,
                          seq: int = 0) -> bytes:
    """[CFW] Build the app->watch ENABLE control frame (spec §3.1). flag=0, protobuf.

    Payload ``f1=CMD_START, f2=rate_hz, f3=range_g, f4=res_bits``. Rate/range/res are validated
    against the supported sets; the watch clamps to the nearest it supports and reports the ACTUAL
    values back in the ack (spec §3.2) and in every data-frame header (spec §4).
    """
    _check_choice("rate_hz", rate_hz, RATE_TO_CODE)
    _check_choice("range_g", range_g, RANGE_TO_CODE)
    _check_choice("res_bits", res_bits, RES_TO_CODE)
    payload = (ProtobufWriter()
               .varint(1, CMD_START)
               .varint(2, rate_hz)
               .varint(3, range_g)
               .varint(4, res_bits)
               .to_bytes())
    return framing.build_command(OP_RAW_ACCEL, payload, flag=FLAG_CONTROL, seq=seq)


def build_rawaccel_disable(seq: int = 0) -> bytes:
    """[CFW] Build the app->watch DISABLE control frame (spec §3.1). flag=0, ``f1=CMD_STOP``."""
    payload = ProtobufWriter().varint(1, CMD_STOP).to_bytes()
    return framing.build_command(OP_RAW_ACCEL, payload, flag=FLAG_CONTROL, seq=seq)


# --------------------------------------------------------------------------- control parser
def parse_rawaccel_ack(payload: bytes) -> dict:
    """[CFW] Parse a watch->app control ACK (spec §3.2).

    ``{status, rate_hz, range_g, res_bits, base_ts_ms}`` from protobuf fields f1..f5. ``status``
    is one of the ``ACK_*`` codes (0 = ok/streaming). rate/range/res are the ACTUAL values the
    watch applied (may differ from the request if it clamped).
    """
    d = to_dict(payload)
    return {
        "status": d.get(1, 0),
        "rate_hz": d.get(2, 0),
        "range_g": d.get(3, 0),
        "res_bits": d.get(4, 0),
        "base_ts_ms": d.get(5, 0),
    }


# --------------------------------------------------------------------------- data parser
def parse_rawaccel_frame(payload: bytes) -> RawAccelBatch:
    """[CFW] Decode a data-frame payload (the 12-byte header + N samples) into a batch (spec §4).

    ``payload`` is ``frame.payload`` from a parsed watch->app 0xA0 flag=1 frame (the CRC has
    already been verified by :func:`framing.parse_frame`). Raises :class:`RawAccelError` on an
    unknown version, a bad length, or a sample count the body cannot satisfy.
    """
    if len(payload) < DATA_HEADER_LEN:
        raise RawAccelError(f"data frame too short for header: {len(payload)} < {DATA_HEADER_LEN}")
    version, cfg, rate_code, count, frame_seq, base_ts_ms, _reserved = struct.unpack_from(
        "<BBBBHIH", payload, 0)
    if version != DATA_VERSION:
        raise RawAccelError(f"unsupported data-frame version {version} (expected {DATA_VERSION})")

    range_g = RANGE_CODES.get(cfg & 0x03)
    res_bits = RES_CODES.get((cfg >> 2) & 0x03)
    if range_g is None or res_bits is None:
        raise RawAccelError(f"bad cfg byte 0x{cfg:02x} (range/res code out of range)")
    rate_hz = RATE_CODES.get(rate_code)   # None tolerated: an unknown rate is non-fatal

    body = payload[DATA_HEADER_LEN:]
    need = count * SAMPLE_LEN
    if len(body) < need:
        raise RawAccelError(
            f"truncated batch: header count={count} needs {need} bytes, have {len(body)}")

    samples: List[AccelSample] = []
    for i in range(count):
        x, y, z = struct.unpack_from("<hhh", body, i * SAMPLE_LEN)
        samples.append(AccelSample(x, y, z,
                                   to_g(x, range_g, res_bits),
                                   to_g(y, range_g, res_bits),
                                   to_g(z, range_g, res_bits)))
    return RawAccelBatch(version, range_g, rate_hz, res_bits, count, frame_seq, base_ts_ms, samples)


def detect_drops(seqs: Sequence[int], *, modulo: int = 1 << 16) -> int:
    """Count dropped frames across an ordered sequence of ``frame_seq`` values (spec §4.1).

    Sums the gap between consecutive frame-seqs (modulo wrap). ``[3,4,6]`` -> 1 dropped; a clean
    run -> 0. A single frame or empty input -> 0.
    """
    dropped = 0
    for prev, cur in zip(seqs, list(seqs)[1:]):
        gap = (cur - prev - 1) % modulo
        dropped += gap
    return dropped


# --------------------------------------------------------------------------- firmware-emulation
def build_rawaccel_data_frame(samples: Sequence[Tuple[int, int, int]], *, rate_hz: int = 50,
                              range_g: int = 8, res_bits: int = 12, frame_seq: int = 0,
                              base_ts_ms: int = 0, seq: int = 0,
                              version: int = DATA_VERSION) -> bytes:
    """[CFW] Build a byte-exact watch->app DATA frame — the firmware's on-wire output (spec §4).

    Reference encoder used by the tests (and by the firmware author as a golden-vector source): a
    watch->app CRC-bearing C1 frame carrying the 12-byte binary header + ``samples``. ``samples``
    is a list of ``(x, y, z)`` left-justified i16 words (as read from the LIS2DH12 OUT registers).
    LEN = header+payload (bytes before the CRC); the CRC-16/CCITT-FALSE trailer is appended so the
    stock :func:`framing.parse_frame` verifies it unchanged.
    """
    cfg = RANGE_TO_CODE[range_g] | (RES_TO_CODE[res_bits] << 2)
    count = len(samples)
    header = struct.pack("<BBBBHIH", version, cfg, RATE_TO_CODE[rate_hz], count,
                         frame_seq & 0xFFFF, base_ts_ms & 0xFFFFFFFF, 0)
    body = b"".join(struct.pack("<hhh", x, y, z) for x, y, z in samples)
    payload = header + body

    length_field = framing.HEADER_LEN + len(payload)   # bytes up to (not incl.) the CRC
    c1 = bytes([
        framing.SOF,
        (seq & 0xFF) | framing.SEQ_HIGH_BIT,   # watch->app frames OR-in 0x80
        framing.DIR_WATCH_TO_APP,
        framing.PROTO_VER,
        FLAG_DATA,
        OP_RAW_ACCEL,
        length_field & 0xFF,
        (length_field >> 8) & 0xFF,
        0x00, 0x00, 0x00,
    ])
    frame_wo_crc = c1 + payload
    return frame_wo_crc + crc16_bytes(frame_wo_crc)


def build_rawaccel_ack_frame(*, status: int = ACK_OK, rate_hz: int = 50, range_g: int = 8,
                             res_bits: int = 12, base_ts_ms: int = 0, seq: int = 0) -> bytes:
    """[CFW] Build a byte-exact watch->app control ACK frame (spec §3.2) — reference/test helper.

    A normal watch->app protobuf reply (flag=0, CRC-bearing), so :func:`framing.parse_frame`
    decodes it via the existing path.
    """
    payload = (ProtobufWriter()
               .varint(1, status)
               .varint(2, rate_hz)
               .varint(3, range_g)
               .varint(4, res_bits)
               .varint(5, base_ts_ms)
               .to_bytes())
    length_field = framing.HEADER_LEN + len(payload)
    c1 = bytes([
        framing.SOF,
        (seq & 0xFF) | framing.SEQ_HIGH_BIT,
        framing.DIR_WATCH_TO_APP,
        framing.PROTO_VER,
        FLAG_CONTROL,
        OP_RAW_ACCEL,
        length_field & 0xFF,
        (length_field >> 8) & 0xFF,
        0x00, 0x00, 0x00,
    ])
    frame_wo_crc = c1 + payload
    return frame_wo_crc + crc16_bytes(frame_wo_crc)


def max_samples_per_frame(mtu_payload: int) -> int:
    """Largest sample count that fits ONE PDU of ``mtu_payload`` ATT bytes (spec §4.2).

    Budget = mtu_payload - C1 header(11) - data header(12) - CRC(2), floored at 0, div sample(6).
    At MTU 247 (mtu_payload=244) this is 36; at the 20-byte floor it is 0 -> the firmware must
    fall back to fragmentation or a smaller header there (see spec §4.2).
    """
    budget = mtu_payload - framing.HEADER_LEN - DATA_HEADER_LEN - 2
    return max(0, budget // SAMPLE_LEN)
