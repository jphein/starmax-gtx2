"""Minimal, dependency-free protobuf writer/reader for the GTX2 command payloads.

The watch speaks unlabelled protobuf (no .proto schema is shipped), so we work at the
raw wire level: (field_number, wire_type, value). Only the wire types the GTX2 uses are
supported: varint (0), 64-bit (1), length-delimited (2), 32-bit (5).

The writer preserves insertion order. The GTX2 app always emits fields in ascending
field-number order, so builders add fields ascending to reproduce captured frames
byte-for-byte.
"""
from __future__ import annotations

import struct
from typing import Iterator, List, Optional, Tuple, Union

WIRE_VARINT = 0
WIRE_I64 = 1
WIRE_LEN = 2
WIRE_I32 = 5


# --------------------------------------------------------------------------- varint
def encode_varint(value: int) -> bytes:
    """Base-128 varint. Negative ints are treated as unsigned 64-bit (two's complement)."""
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def decode_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    """Return (value, new_pos). Raises ValueError on a truncated varint."""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


# --------------------------------------------------------------------------- writer
class ProtobufWriter:
    """Accumulate protobuf fields (in insertion order) and serialise to bytes."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def _tag(self, field: int, wire: int) -> None:
        self._buf += encode_varint((field << 3) | wire)

    def varint(self, field: int, value: int) -> "ProtobufWriter":
        self._tag(field, WIRE_VARINT)
        self._buf += encode_varint(value)
        return self

    def bool(self, field: int, value: bool) -> "ProtobufWriter":
        return self.varint(field, 1 if value else 0)

    def bytes(self, field: int, value: bytes) -> "ProtobufWriter":
        self._tag(field, WIRE_LEN)
        self._buf += encode_varint(len(value))
        self._buf += value
        return self

    def string(self, field: int, value: str) -> "ProtobufWriter":
        return self.bytes(field, value.encode("utf-8"))

    def message(self, field: int, sub: Union["ProtobufWriter", bytes]) -> "ProtobufWriter":
        raw = sub.to_bytes() if isinstance(sub, ProtobufWriter) else sub
        return self.bytes(field, raw)

    def fixed32(self, field: int, value: int) -> "ProtobufWriter":
        self._tag(field, WIRE_I32)
        self._buf += struct.pack("<I", value & 0xFFFFFFFF)
        return self

    def fixed64(self, field: int, value: int) -> "ProtobufWriter":
        self._tag(field, WIRE_I64)
        self._buf += struct.pack("<Q", value & 0xFFFFFFFFFFFFFFFF)
        return self

    def to_bytes(self) -> bytes:
        return bytes(self._buf)


# --------------------------------------------------------------------------- reader
Field = Tuple[int, int, object]  # (field_number, wire_type, value)


def parse(buf: bytes) -> List[Field]:
    """Parse a protobuf message into a flat list of (field, wire_type, value).

    Values: varint/i32/i64 -> int; length-delimited -> raw bytes (caller decides whether
    it is a nested message, UTF-8 string, or opaque blob).
    """
    fields: List[Field] = []
    pos = 0
    n = len(buf)
    while pos < n:
        key, pos = decode_varint(buf, pos)
        field = key >> 3
        wire = key & 0x07
        if wire == WIRE_VARINT:
            val, pos = decode_varint(buf, pos)
            fields.append((field, wire, val))
        elif wire == WIRE_I64:
            if pos + 8 > n:
                raise ValueError("truncated i64")
            fields.append((field, wire, struct.unpack_from("<Q", buf, pos)[0]))
            pos += 8
        elif wire == WIRE_LEN:
            length, pos = decode_varint(buf, pos)
            if pos + length > n:
                raise ValueError("truncated length-delimited field")
            fields.append((field, wire, buf[pos:pos + length]))
            pos += length
        elif wire == WIRE_I32:
            if pos + 4 > n:
                raise ValueError("truncated i32")
            fields.append((field, wire, struct.unpack_from("<I", buf, pos)[0]))
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire} for field {field}")
    return fields


def to_dict(buf: bytes) -> dict:
    """Convenience: {field_number: value} keeping the LAST value for repeated fields.

    Use :func:`parse` when field order or repeated fields matter.
    """
    out: dict = {}
    for field, _wire, val in parse(buf):
        out[field] = val
    return out


def iter_fields(buf: bytes, field_number: int) -> Iterator[object]:
    """Yield every value for a repeated ``field_number`` in wire order."""
    for field, _wire, val in parse(buf):
        if field == field_number:
            yield val


def get(buf: bytes, field_number: int, default: Optional[object] = None) -> object:
    """Return the first value for ``field_number`` (or ``default``)."""
    for field, _wire, val in parse(buf):
        if field == field_number:
            return val
    return default
