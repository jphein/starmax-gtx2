"""CRC-16/CCITT-FALSE — the checksum the GTX2 appends to watch->app protobuf frames.

Spec: poly 0x1021, init 0xFFFF, no input/output reflection, xorout 0x0000, stored
little-endian on the wire (see docs/protocol-spec.md §1.2). The canonical check value
for the ASCII string "123456789" is 0x29B1.
"""
from __future__ import annotations

_POLY = 0x1021
_INIT = 0xFFFF


def crc16_ccitt_false(data: bytes) -> int:
    """Return the CRC-16/CCITT-FALSE of ``data`` as a 16-bit int."""
    crc = _INIT
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ _POLY) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def crc16_bytes(data: bytes) -> bytes:
    """Return the CRC as the 2 little-endian bytes stored on the wire."""
    import struct
    return struct.pack("<H", crc16_ccitt_false(data))
