"""GTX2 0xC1 command-channel frame codec (transport-independent).

Envelope (docs/protocol-spec.md §1):

    off 0     0xC1 start-of-frame  (0xC3 = continuation fragment)
    off 1     seq   sequence counter; watch->app frames OR-in 0x80
    off 2     dir   0x01 = app->watch (command), 0x00 = watch->app (reply/push)
    off 3     0x01  protocol version (constant)
    off 4     flag  per-opcode sub-type / bank byte
    off 5     opcode
    off 6-7   LEN   16-bit LITTLE-ENDIAN
    off 8-10  00 00 00   reserved
    off 11..  payload (protobuf, or a binary record for opcode 0x0e flag=1)
    [tail]    CRC-16/CCITT-FALSE, little-endian -- watch->app protobuf frames ONLY

LEN / CRC rules are direction- and class-dependent (§1.1):

    * app->watch command            : LEN = whole frame length, NO CRC
    * watch->app protobuf           : LEN = total - 2, CRC over frame[0:LEN]
    * 0x0e flag=1 binary record      : LEN = whole frame length, NO CRC (either direction)

The bind (0x01) command is not a special case here: it is simply an app->watch command
with an empty payload, which the general rule already frames as the 11-byte header-only
frame observed on the wire.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, Optional

from .crc import crc16_ccitt_false

SOF = 0xC1
CONT = 0xC3
MIDDLE = 0xC2  # watch->app middle fragment: payload past a 2-byte [type][seq] header, same as CONT
DIR_APP_TO_WATCH = 0x01
DIR_WATCH_TO_APP = 0x00
PROTO_VER = 0x01
HEADER_LEN = 11
SEQ_HIGH_BIT = 0x80

OP_HEALTH_SYNC = 0x0E  # opcode whose flag=1 records are raw binary (no CRC)


class FrameError(Exception):
    """Raised when a frame cannot be parsed or fails CRC verification."""


def _is_binary_record(opcode: int, flag: int) -> bool:
    """0x0e flag=1 carries a binary health record: LEN = total, no CRC (§1.1/§6)."""
    return opcode == OP_HEALTH_SYNC and flag == 1


# --------------------------------------------------------------------------- build
def build_command(opcode: int, payload: bytes = b"", *, flag: int = 0, seq: int = 0) -> bytes:
    """Build an app->watch command frame.

    LEN = whole frame length, and NO CRC is appended (§1.1). This is correct for every
    outbound opcode, including bind (empty payload -> 11-byte header) and the 0x0e flag=1
    health-sync request (its request body is protobuf but still carries no CRC).
    """
    total = HEADER_LEN + len(payload)
    header = bytes([
        SOF,
        seq & 0xFF,
        DIR_APP_TO_WATCH,
        PROTO_VER,
        flag & 0xFF,
        opcode & 0xFF,
        total & 0xFF,
        (total >> 8) & 0xFF,
        0x00, 0x00, 0x00,
    ])
    return header + payload


def frame_to_pdus(frame: bytes, mtu: int) -> List[bytes]:
    """Split a built frame into wire PDUs for a given ATT payload size (``mtu``).

    The first PDU is the frame's leading ``mtu`` bytes (a valid 0xC1 start whose LEN
    field already declares the full length). Each continuation is ``0xC3 | seq | chunk``
    carrying the next ``mtu - 2`` bytes of the frame byte-stream, reusing the frame's seq
    (§1.3).
    """
    if mtu < HEADER_LEN + 1:
        raise ValueError(f"mtu {mtu} too small for a C1 header")
    if len(frame) <= mtu:
        return [frame]
    seq = frame[1]
    pdus = [frame[:mtu]]
    rest = frame[mtu:]
    step = mtu - 2  # C3 header is 2 bytes (0xC3, seq)
    for i in range(0, len(rest), step):
        pdus.append(bytes([CONT, seq]) + rest[i:i + step])
    return pdus


# --------------------------------------------------------------------------- parse
@dataclass
class Frame:
    opcode: int
    flag: int
    seq: int
    direction: int          # DIR_APP_TO_WATCH or DIR_WATCH_TO_APP
    payload: bytes
    raw: bytes
    length_field: int       # the LEN value from off6-7
    crc_ok: Optional[bool]  # None when the frame class carries no CRC
    is_binary: bool         # True for 0x0e flag=1 records (payload is not protobuf)


def _declared_total(buf: bytes, direction: int) -> int:
    """Total on-wire length declared by a C1 header, for reassembly completion."""
    length_field = buf[6] | (buf[7] << 8)
    if direction == DIR_APP_TO_WATCH:
        return length_field
    # watch->app: protobuf frames add a 2-byte CRC; 0x0e flag=1 records do not.
    if _is_binary_record(buf[5], buf[4]):
        return length_field
    return length_field + 2


def parse_frame(buf: bytes, *, direction: Optional[int] = None) -> Frame:
    """Parse a fully-reassembled 0xC1 frame.

    ``direction`` may be forced (e.g. from the characteristic a notification arrived on);
    otherwise it is inferred from the dir byte at off2.
    """
    if len(buf) < HEADER_LEN:
        raise FrameError(f"frame too short: {len(buf)} bytes")
    if buf[0] != SOF:
        raise FrameError(f"not a C1 frame (off0=0x{buf[0]:02x})")
    if direction is None:
        direction = DIR_WATCH_TO_APP if buf[2] == DIR_WATCH_TO_APP else DIR_APP_TO_WATCH

    opcode = buf[5]
    flag = buf[4]
    seq = buf[1]
    length_field = buf[6] | (buf[7] << 8)
    binary = _is_binary_record(opcode, flag)

    if direction == DIR_APP_TO_WATCH or binary:
        # LEN = total, no CRC.
        payload = buf[HEADER_LEN:length_field]
        return Frame(opcode, flag, seq, direction, payload, buf, length_field,
                     crc_ok=None, is_binary=binary)

    # watch->app protobuf: LEN = total - 2, CRC over frame[0:LEN], stored LE at [LEN:LEN+2].
    if len(buf) < length_field + 2:
        raise FrameError(
            f"frame shorter than declared LEN+CRC (have {len(buf)}, need {length_field + 2})")
    payload = buf[HEADER_LEN:length_field]
    stored = struct.unpack_from("<H", buf, length_field)[0]
    calc = crc16_ccitt_false(buf[:length_field])
    return Frame(opcode, flag, seq, direction, payload, buf, length_field,
                 crc_ok=(stored == calc), is_binary=False)


# ------------------------------------------------------------------- reassembly
class Reassembler:
    """Accumulate inbound NUS PDUs, joining 0xC3 fragments into whole frames.

    Feed it each notification value via :meth:`feed`, which returns a list of completed
    :class:`Frame` objects (usually 0 or 1). Fragments are matched to the open C1 frame by
    seq, and the frame completes once the accumulated bytes reach the header-declared total.
    """

    def __init__(self, direction: int = DIR_WATCH_TO_APP) -> None:
        self._direction = direction
        self._buf: Optional[bytearray] = None
        self._seq: Optional[int] = None
        self._declared: int = 0

    def feed(self, pdu: bytes) -> List[Frame]:
        out: List[Frame] = []
        if not pdu:
            return out
        if pdu[0] == SOF:
            if self._buf is not None:
                # A new frame started before the previous one completed; flush it best-effort.
                out.extend(self._try_complete(force=True))
            if len(pdu) < 8:
                raise FrameError("C1 PDU too short to contain a length field")
            self._buf = bytearray(pdu)
            self._seq = pdu[1]
            self._declared = _declared_total(pdu, self._direction)
            out.extend(self._try_complete())
        elif pdu[0] in (CONT, MIDDLE):
            if self._buf is None:
                raise FrameError("orphan continuation (0xC2/0xC3) with no open frame")
            # Both 0xC2 (middle) and 0xC3 (last) continuations carry payload past a 2-byte
            # [type][seq] header (seq matches the open C1 frame). Dropping 0xC2 desyncs any frame
            # spanning >2 PDUs (e.g. the dial-list reply or a full sleep record) — same fix as GB.
            self._buf.extend(pdu[2:])
            out.extend(self._try_complete())
        else:
            raise FrameError(f"unexpected NUS channel byte 0x{pdu[0]:02x}")
        return out

    def _try_complete(self, force: bool = False) -> List[Frame]:
        assert self._buf is not None
        if force or len(self._buf) >= self._declared:
            frame = parse_frame(bytes(self._buf[:self._declared]) if not force else bytes(self._buf),
                                 direction=self._direction)
            self._buf = None
            self._seq = None
            self._declared = 0
            return [frame]
        return []
