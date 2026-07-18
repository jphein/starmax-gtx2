"""Native GTX2 dial-resource container ("blob") codec.

This is the **on-wire form the watch actually consumes** for a dial install — *not* the
distributed dial ``.bin`` (which is a ZIP of ``dial.json`` + PNG/BMP assets). The Runmefit
app **transcodes** the ZIP's ``firmware/`` subtree into this container, then streams the
container over the D-plane (see ``starmax_client.commands.dials``).

Provenance — [CAP], byte-exact
------------------------------
Reverse-engineered from our own BLE capture @ t=5806.83s, where the app pushed dial
``CWR05G_23687`` as ``custom_id_25022.bin`` (231 293 B) over the
bulk plane. Two independent checksums nail the format:

  * the D4 finalize's whole-file **CRC-16/XMODEM** equals ``crc16_xmodem(blob)`` = ``0xB735``;
  * the header word at 0x28 equals ``zlib.crc32(blob[0x2c:])`` = ``0xE5FD3A7B``.

The 989 reassembled D2 chunks reproduce the 231 293-byte container exactly, and the 24-entry
asset table's ``(offset, length)`` pairs are contiguous and cover the file with zero padding.

Layout (all integers little-endian)
------------------------------------
======  ====  ==============================================================
offset  size  field
======  ====  ==============================================================
0x00    30    ``name``      dial internal name, NUL-padded ASCII
0x1e    u16   ``MAGIC1``    = 0x4321  (little-endian)
0x20    u16   ``MAGIC2``    = 0x5AA5  (little-endian)
0x22    2B    ``CONST_A``   = 06 04   (constant in our one sample; preserved opaque)
0x24    u16   ``count``     number of asset entries — **big-endian** (00 18 = 24)
0x26    2B    ``CONST_B``   = 00 04   (constant; == dial.json ``dial_version``? preserved opaque)
0x28    u32   ``crc32``     zlib CRC-32 over ``blob[0x2c:]`` (table + data), little-endian
0x2c    ...   ``entries``   ``count`` × 38 B: ``name[30]`` + ``u32 offset`` + ``u32 length``
....    ...   ``data``      asset payloads, contiguous, table order; ``offset`` is absolute
======  ====  ==============================================================

Asset payloads: ``dial.json`` / ``file.json`` are embedded verbatim (UTF-8). Image assets
(``*_8888.png`` / ``*_0565.bmp``) are stored **transcoded** — decoded from PNG/BMP and
(per their sizes) compressed — NOT as the original file bytes. This codec treats each asset
payload as opaque bytes, so it round-trips a captured/authored blob byte-for-byte; a full
ZIP→blob *transcoder* (which must re-encode the images) is a separate concern.

Clean-room: STANDALONE lane (Track B). Capture-derived and therefore PORTABLE — safe to
inform the Gadgetbridge coordinator later. Never derived from the APK/SDK.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import List, Tuple

# --- header constants (byte-exact from the captured CWR05G_23687 install) --------------
# The 6-byte region between MAGIC2 and the CRC is `CONST_A(2) | count(BE u16) | CONST_B(2)`.
# Only `count` (0x0018 = 24) has an observed meaning in our single sample; CONST_A/CONST_B
# are preserved opaquely (defaults = the observed bytes). Magics + CRC are little-endian;
# `count` is big-endian — reproduced byte-exact below and asserted by the round-trip test.
MAGIC1 = 0x4321
MAGIC2 = 0x5AA5
CONST_A = b"\x06\x04"  # 0x22..0x23, constant in our sample
CONST_B = b"\x00\x04"  # 0x26..0x27, constant in our sample (matches dial_version=4?)
NAME_LEN = 30          # dial-name field AND per-entry name field share this width
ENTRY_LEN = 38         # name[30] + u32 offset + u32 length
HEADER_LEN = 0x2C      # name[30] + magics + region + crc = 44 bytes; asset table starts here


class DialFormatError(ValueError):
    """Raised when bytes do not parse as a native dial blob."""


@dataclass
class DialAsset:
    """One entry in the blob's asset table."""
    name: str
    data: bytes


@dataclass
class DialBlob:
    """A parsed native dial-resource container."""
    name: str
    assets: List[DialAsset]
    const_a: bytes = CONST_A
    const_b: bytes = CONST_B

    @property
    def asset_names(self) -> List[str]:
        return [a.name for a in self.assets]

    def get(self, name: str) -> bytes:
        for a in self.assets:
            if a.name == name:
                return a.data
        raise KeyError(name)


def _fixed_name(name: str) -> bytes:
    """Encode a name into the fixed NAME_LEN field (ASCII, NUL-padded)."""
    raw = name.encode("ascii")
    if len(raw) >= NAME_LEN:
        raise DialFormatError(f"name {name!r} too long for {NAME_LEN}-byte field")
    return raw + b"\x00" * (NAME_LEN - len(raw))


def _read_name(buf: bytes, off: int) -> str:
    return buf[off:off + NAME_LEN].split(b"\x00", 1)[0].decode("ascii", "replace")


def parse_blob(buf: bytes, *, verify_crc: bool = True) -> DialBlob:
    """Parse a native dial blob. Verifies the header CRC-32 unless ``verify_crc`` is False."""
    if len(buf) < HEADER_LEN:
        raise DialFormatError(f"too short for a dial header: {len(buf)} < {HEADER_LEN}")
    name = _read_name(buf, 0)
    magic1, magic2 = struct.unpack_from("<HH", buf, NAME_LEN)
    if magic1 != MAGIC1 or magic2 != MAGIC2:
        raise DialFormatError(
            f"bad magic: {magic1:#06x} {magic2:#06x} (want {MAGIC1:#06x} {MAGIC2:#06x})")
    const_a = buf[0x22:0x24]
    count = struct.unpack_from(">H", buf, 0x24)[0]   # big-endian in the captured container
    const_b = buf[0x26:0x28]
    crc = struct.unpack_from("<I", buf, 0x28)[0]
    if verify_crc:
        calc = zlib.crc32(buf[HEADER_LEN:]) & 0xFFFFFFFF
        if calc != crc:
            raise DialFormatError(f"header crc32 mismatch: stored {crc:#010x} != calc {calc:#010x}")
    table_end = HEADER_LEN + count * ENTRY_LEN
    if table_end > len(buf):
        raise DialFormatError(f"asset table ({count} entries) overruns buffer")
    assets: List[DialAsset] = []
    for i in range(count):
        eoff = HEADER_LEN + i * ENTRY_LEN
        ename = _read_name(buf, eoff)
        doff, dlen = struct.unpack_from("<II", buf, eoff + NAME_LEN)
        if doff + dlen > len(buf):
            raise DialFormatError(f"entry {ename!r} data [{doff}:{doff + dlen}] overruns buffer")
        assets.append(DialAsset(ename, buf[doff:doff + dlen]))
    return DialBlob(name=name, assets=assets, const_a=const_a, const_b=const_b)


def build_blob(name: str, assets: List[Tuple[str, bytes]], *,
               const_a: bytes = CONST_A, const_b: bytes = CONST_B) -> bytes:
    """Serialize a native dial blob from ``(asset_name, payload)`` pairs.

    Reproduces the captured container byte-for-byte when given that dial's name + assets in
    the original order: the asset table is laid out contiguously starting right after the
    table, offsets are absolute, ``count`` is a big-endian u16, and the header CRC-32 is
    computed over ``blob[0x2c:]``.
    """
    count = len(assets)
    data_start = HEADER_LEN + count * ENTRY_LEN
    table = bytearray()
    data = bytearray()
    cursor = data_start
    for aname, payload in assets:
        table += _fixed_name(aname) + struct.pack("<II", cursor, len(payload))
        data += payload
        cursor += len(payload)
    body = bytes(table) + bytes(data)
    crc = zlib.crc32(body) & 0xFFFFFFFF
    header = (_fixed_name(name) + struct.pack("<HH", MAGIC1, MAGIC2)
              + bytes(const_a) + struct.pack(">H", count) + bytes(const_b)
              + struct.pack("<I", crc))
    return header + body


def rebuild_blob(blob: DialBlob) -> bytes:
    """Round-trip a parsed :class:`DialBlob` back to bytes (byte-identical to a clean source)."""
    return build_blob(blob.name, [(a.name, a.data) for a in blob.assets],
                       const_a=blob.const_a, const_b=blob.const_b)
