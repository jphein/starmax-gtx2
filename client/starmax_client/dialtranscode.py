"""ZIP -> native-blob transcoder for GTX2 dials.

Turns a **distributed dial ``.bin``** (a ZIP of ``dial.json`` + PNG/BMP assets, see
``docs/watchface-format.md``) into the **native container the watch consumes**
(:mod:`starmax_client.dialfmt`) — so you can author a custom face from images and push it,
not just replay a captured blob. This is the piece that makes ``dial-push`` accept a normal
dial ``.bin``.

Per-asset image codec — [CAP], reverse-engineered byte-exact
------------------------------------------------------------
Decoded from a captured install (``docs/watchface-install.md`` §2.1) by comparing the streamed
container against the dial's source ZIP pulled from the CDN. Every image asset is::

    <type:1> <(height << 13) | (width << 2) : u24 LE> <lz4.block payload>

* ``type`` ``0x18`` = RGBA8888 (uncompressed = W*H*4, channel order R,G,B,A)
* ``type`` ``0x04`` = RGB565 **little-endian** (uncompressed = W*H*2)
* the payload is a single raw LZ4 block (``lz4.block``, no stored size); uncompressed size is
  derived from W*H*bpp.

``dial.json`` / ``file.json`` are embedded verbatim. Verified: this decoder reproduces the
source pixels of all 22 image assets in the sample dial exactly (a few vendor assets differ
by a small % because the app repacked from master art, not the distributed PNGs — not a codec
issue). Container assembly is :func:`starmax_client.dialfmt.build_blob`.

Requires **Pillow + lz4** (the optional ``transcode`` extra). They are imported lazily so the
core client and ``dial-push`` of an already-native blob work without them.

Clean-room: capture-derived (PORTABLE). The image codec + container are safe to inform the
Gadgetbridge coordinator.
"""
from __future__ import annotations

import json
import struct
import zipfile
from typing import List, Tuple

from starmax_client import dialfmt

ASSET_RGBA8888 = 0x18
ASSET_RGB565 = 0x04

# firmware/ assets NOT streamed to the watch (phone-app side only).
_SKIP_ASSETS = {"app_preview.png"}


class TranscodeError(ValueError):
    """Raised when a dial ZIP cannot be transcoded to a native container."""


def _require_deps():
    try:
        from PIL import Image  # noqa: F401
        import lz4.block  # noqa: F401
    except ImportError as e:  # pragma: no cover - exercised only without the extra
        raise TranscodeError(
            "the dial transcoder needs Pillow + lz4 — install the extra: "
            "pip install 'starmax-client[transcode]'") from e
    from PIL import Image
    import lz4.block
    return Image, lz4.block


def pack_field(width: int, height: int) -> bytes:
    """The 3-byte u24-LE resolution field: ``(height << 13) | (width << 2)``."""
    if not (0 < width < 2048 and 0 < height < 2048):
        raise TranscodeError(f"dimensions out of range: {width}x{height}")
    return struct.pack("<I", (height << 13) | (width << 2))[:3]


def unpack_field(b3: bytes) -> Tuple[int, int]:
    """Inverse of :func:`pack_field` -> ``(width, height)``."""
    field = b3[0] | (b3[1] << 8) | (b3[2] << 16)
    return (field & 0x1FFF) >> 2, field >> 13


def _rgb565_le(image) -> bytes:
    rgb = image.convert("RGB").tobytes()  # R,G,B row-major
    out = bytearray(len(rgb) // 3 * 2)
    j = 0
    for i in range(0, len(rgb), 3):
        v = ((rgb[i] & 0xF8) << 8) | ((rgb[i + 1] & 0xFC) << 3) | (rgb[i + 2] >> 3)
        out[j] = v & 0xFF
        out[j + 1] = v >> 8
        j += 2
    return bytes(out)


def asset_type_for(name: str) -> int:
    """Pick the native pixel type from the asset filename suffix."""
    low = name.lower()
    if low.endswith("_8888.png"):
        return ASSET_RGBA8888
    if low.endswith("_0565.bmp"):
        return ASSET_RGB565
    raise TranscodeError(f"unrecognized image asset suffix: {name!r} "
                         "(expected *_8888.png or *_0565.bmp)")


def encode_image(name: str, source_bytes: bytes) -> bytes:
    """Encode one source PNG/BMP into a native image asset (header + lz4 block)."""
    Image, lz4block = _require_deps()
    import io
    atype = asset_type_for(name)
    im = Image.open(io.BytesIO(source_bytes))
    w, h = im.size
    if atype == ASSET_RGBA8888:
        raw = im.convert("RGBA").tobytes()  # R,G,B,A row-major
    else:
        raw = _rgb565_le(im)
    block = lz4block.compress(raw, store_size=False)
    return bytes([atype]) + pack_field(w, h) + block


def decode_image(asset_bytes: bytes) -> Tuple[int, int, int, bytes]:
    """Inverse of :func:`encode_image` -> ``(type, width, height, raw_pixels)``."""
    _, lz4block = _require_deps()
    atype = asset_bytes[0]
    w, h = unpack_field(asset_bytes[1:4])
    bpp = 4 if atype == ASSET_RGBA8888 else 2
    raw = lz4block.decompress(asset_bytes[4:], uncompressed_size=w * h * bpp)
    return atype, w, h, raw


def _firmware_entries(zf: zipfile.ZipFile) -> List[Tuple[str, str]]:
    """[(arcname, basename)] for the firmware/ subtree, minus dirs + phone-only assets."""
    out = []
    for n in zf.namelist():
        if n.endswith("/"):
            continue
        parts = n.split("/")
        if "firmware" not in parts:
            continue  # skip app/ and any non-firmware tree
        base = parts[-1]
        if base in _SKIP_ASSETS:
            continue
        out.append((n, base))
    return out


def transcode_zip(zip_bytes: bytes) -> bytes:
    """Transcode a distributed dial ``.bin`` (ZIP) into the native container to stream.

    ``dial.json`` / ``file.json`` are embedded verbatim; ``*_8888.png`` / ``*_0565.bmp`` assets
    are decoded and re-encoded as native image assets. ``app_preview.png`` and the phone-side
    ``app/`` tree are dropped (the watch never receives them). The container name is the
    manifest ``name``. Asset order: ``dial.json``, ``file.json``, then remaining assets in ZIP
    order (the asset table is keyed by name, so order is not load-bearing).
    """
    if zip_bytes[:2] != b"PK":
        raise TranscodeError("not a ZIP dial .bin")
    import io
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise TranscodeError(f"corrupt dial ZIP: {e}") from e
    entries = _firmware_entries(zf)
    if not entries:
        raise TranscodeError("no firmware/ assets found in the dial ZIP")

    dj = next((a for a, b in entries if b == "dial.json"), None)
    if dj is None:
        raise TranscodeError("dial ZIP has no firmware/dial.json")
    manifest = json.loads(zf.read(dj))
    name = manifest.get("name")
    if not name:
        raise TranscodeError("dial.json has no 'name'")

    def rank(base: str) -> int:
        return {"dial.json": 0, "file.json": 1}.get(base, 2)
    entries.sort(key=lambda ab: (rank(ab[1]),))  # stable: keeps ZIP order within rank 2

    assets: List[Tuple[str, bytes]] = []
    for arc, base in entries:
        data = zf.read(arc)
        if base.endswith(".json"):
            assets.append((base, data))  # verbatim
        else:
            assets.append((base, encode_image(base, data)))
    return dialfmt.build_blob(name, assets)
