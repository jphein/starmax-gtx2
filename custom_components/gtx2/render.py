"""Vendored FLAT-STATIC grid-watts renderer — self-contained (Pillow + lz4 + stdlib only).

Why vendored (not a package dep): HAOS installs the component from this folder with no private-repo
pip step, so the render primitives must live here. This is a byte-exact copy of the STATIC subset of
three canonical sources — do NOT edit the algorithm; keep it in sync and let the parity test guard it:

  * ha-bridge/gtx2_bridge/faces.py            -> render_grid_static / _hard_text / _fmt_watts /
                                                 _load_font / image_to_blob / _manifest / _GRID_* consts
  * starmax_client/dialtranscode.py           -> encode_image / _rgb565_le / pack_field / asset_type_for
  * starmax_client/dialfmt.py                 -> build_blob / parse_blob / _fixed_name (+ consts)

Locked public signature (matches faces.build_grid_static_blob):
    build_grid_static_blob(watts, *, max_w=12000, name="GRIDWATTS", preview_size=32) -> bytes

The static face is WATTS-ONLY (hard-edged number + WATTS label + bar gauge, no clock/live widgets).
At the HW-safe LZ4 cap=512 it lands ~9-10 KB — OVER the ~8 KB /local/ OOM ceiling — so the gauge
ships via the chunked D-plane path (facepush.push_face), NOT the /local/ url path. Byte-parity with
faces.build_grid_static_blob is guarded by the parity test.
"""
from __future__ import annotations

import io
import json
import os
import struct
import zlib

# LZ4 block compression seam: prefer python-lz4 (dev/the host — the byte-parity reference), fall back
# to cramjam on HAOS (the HA container is Alpine/musl; lz4 4.4.5 ships NO musllinux cp314 wheel and
# its sdist can't build there, while cramjam ships musllinux wheels). Equivalence (measured
# 2026-07-15, tests/test_gtx2cc_render_seam.py): byte-identical on the large BG_0565 block; the tiny
# 48x48 preview stream can differ by a few bytes (both valid LZ4 — different end-of-block match
# choices) but DECODES to identical pixels. Cross-decompression verified both directions.
try:
    import lz4.block as _lz4block

    def _lz4_compress(raw: bytes) -> bytes:
        return _lz4block.compress(raw, store_size=False)
except ImportError:                                    # HAOS: manifest installs cramjam instead
    import cramjam as _cramjam

    def _lz4_compress(raw: bytes) -> bytes:
        return bytes(_cramjam.lz4.compress_block(raw, store_size=False))

from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------------- geometry / assets
W = H = 466                      # GTX2 screen
PREVIEW = 256                    # default dial-picker thumbnail edge
_MANIFEST_NAME_MAX = 29          # dialfmt name field is 30 B, NUL-terminated

_TTF_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
_TTF_REG = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]

# --------------------------------------------------------------------------- grid-face palette
_GRID_BG = (5, 7, 10)
_GRID_TRACK = (28, 36, 48)
_GRID_IMPORT = (255, 59, 48)
_GRID_IDLE = (120, 130, 145)
_GRID_LABEL = (120, 132, 150)
_GRID_IDLE_W = 15
DEFAULT_MAX_W = 12000
_G_CX = 233                       # gauge/number centre x
_GRID_S_NUM = 84                  # watts glyph height (84 keeps every value < 8 KB with margin)


# ============================ VENDORED from gtx2_bridge.faces (static subset) ==================
def _load_font(size: int, *, bold: bool = False):
    for path in (_TTF_BOLD if bold else _TTF_REG):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    try:
        return ImageFont.load_default(size=size)   # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def _fmt_watts(watts: float) -> str:
    """934 -> '934', 1293 -> '1293', 12800 -> '12.8k'."""
    a = abs(int(round(watts)))
    return f"{a / 1000:.1f}k" if a >= 10000 else str(a)


def _hard_text(img: Image.Image, cx: int, cy: int, s: str, size: int, color, *,
               anchor: str = "mm", thresh: int = 96) -> None:
    """Paint hard-edged (no-AA) text: render to an L mask, threshold it, paste a solid fill.

    Anti-aliased glyph edges are the LZ4 entropy killer; a 2-value mask keeps the background in
    long runs so a full-canvas number still compresses into the < 8 KB budget."""
    layer = Image.new("L", (W, H), 0)
    ImageDraw.Draw(layer).text((cx, cy), s, font=_load_font(size, bold=True), fill=255, anchor=anchor)
    img.paste(Image.new("RGB", (W, H), color), (0, 0), layer.point(lambda p: 255 if p > thresh else 0))


def render_grid_static(watts: float, *, max_w: int = DEFAULT_MAX_W) -> Image.Image:
    """Render the FLAT < 8 KB grid-watts face: hard-edged watts number + WATTS label + bar gauge."""
    img = Image.new("RGB", (W, H), _GRID_BG)
    d = ImageDraw.Draw(img)
    mag = abs(int(round(watts)))
    col = _GRID_IDLE if mag < _GRID_IDLE_W else _GRID_IMPORT
    frac = max(0.0, min(1.0, mag / float(max_w or DEFAULT_MAX_W)))

    _hard_text(img, _G_CX, 224, _fmt_watts(watts), _GRID_S_NUM, col)
    _hard_text(img, _G_CX, 224 + _GRID_S_NUM // 2 + 22, "WATTS", 26, _GRID_LABEL)

    bx0, by0, bx1, by1 = 70, 338, W - 70, 366          # hard-edged bar gauge (LZ4-friendly rects)
    d.rectangle([bx0, by0, bx1, by1], fill=_GRID_TRACK)
    if frac > 0:
        d.rectangle([bx0, by0, bx0 + int((bx1 - bx0) * frac), by1], fill=col)
    return img


def _safe_name(name: str) -> str:
    ascii_name = "".join(c for c in name if 32 <= ord(c) < 127) or "NOTIFY"
    return ascii_name[:_MANIFEST_NAME_MAX]


def _manifest(name: str) -> dict:
    """A minimal, valid dial.json: one full-screen background item (no live fields)."""
    bg_item = {"widget": "icon", "type": "background", "x": 0, "y": 0,
               "w": W, "h": H, "picture": "BG_0565.bmp"}
    return {
        "frame_version": 1, "dial_version": 2, "name": name, "preview": "preview_0565.bmp",
        "dial_type": 1, "resolution_ratio": f"{W}x{H}", "platform": "ats3085s",
        "size": 256, "enable_pic_compress": 0, "app_preview": "app_preview.png",
        "item": [bg_item], "fade_item": [dict(bg_item)],
    }


def _bmp_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="BMP")
    return buf.getvalue()


# LZ4-block match-length cap — HW-CRITICAL for near-solid faces. (VENDORED verbatim from
# gtx2_bridge.faces @fbe5555 — the byte-parity test guards drift.)
# HW-PROVEN on the spare 2026-07-15: the GTX2's minimal LZ4 decoder GARBLES (renders black/corrupt)
# any image asset whose LZ4 has a match longer than ~this. A near-solid face (e.g. the grid gauge on
# a black bg) compresses to matches up to ~178 KB; the captured vendor dial's longest match is 858 B,
# so the watch is never tested past ~1 KB.
# ⚠️ cap=2048 was MARGINAL and is RETIRED: it rendered clean by luck (some layouts' 2048-length
# matches decode, others garble) — a verified-full CRC-valid v2 face STILL corrupted at 2048, and the
# live auto-refresh corrupted daily on a re-push at a new watts value (different match pattern). The
# watch's real limit is < 2048. cap=512 (well under the vendor-proven-good 858) is HW-CONFIRMED SAFE.
# cap_lz4_matches splits long matches into short ones, byte-identical on decode — and is COMPRESSOR-
# AGNOSTIC (operates on the finished block) so it applies equally after this module's lz4 OR cramjam seam.
GRID_STATIC_MAX_MATCH = 512


def cap_lz4_matches(block: bytes, cap: int) -> bytes:
    """Re-encode an LZ4 *block* so no match exceeds ``cap`` bytes (splitting long matches into several
    short ones). Decodes byte-identically; works on any standard LZ4 block regardless of the
    compressor that produced it (lz4.block, cramjam, …). See :data:`GRID_STATIC_MAX_MATCH`."""
    def _ext(x: int) -> bytes:
        out = bytearray()
        while x >= 255:
            out.append(255); x -= 255
        out.append(x)
        return bytes(out)

    out = bytearray()
    i, n = 0, len(block)
    while i < n:
        tok = block[i]; i += 1
        ll = tok >> 4
        if ll == 15:
            while True:
                b = block[i]; i += 1; ll += b
                if b != 255:
                    break
        lits = block[i:i + ll]; i += ll
        if i >= n:                                        # final sequence: literals only, no match
            out.append((15 if ll >= 15 else ll) << 4)
            if ll >= 15:
                out += _ext(ll - 15)
            out += lits
            break
        off = block[i] | (block[i + 1] << 8); i += 2
        ml = tok & 0xf
        if ml == 15:
            while True:
                b = block[i]; i += 1; ml += b
                if b != 255:
                    break
        mlen = ml + 4
        chunks, m = [], mlen                              # split into <=cap chunks, each >=4, same offset
        while m > cap:
            chunks.append(cap); m -= cap
        if 0 < m < 4 and chunks:
            chunks[-1] -= (4 - m); m = 4
        if m:
            chunks.append(m)
        for k, cl in enumerate(chunks):
            lk = len(lits) if k == 0 else 0
            mk = cl - 4
            out.append(((15 if lk >= 15 else lk) << 4) | (15 if mk >= 15 else mk))
            if lk >= 15:
                out += _ext(lk - 15)
            if k == 0:
                out += lits
            out += off.to_bytes(2, "little")
            if mk >= 15:
                out += _ext(mk - 15)
    return bytes(out)


def image_to_blob(img: Image.Image, *, name: str = "NOTIFY", preview_size: int = PREVIEW,
                  max_match: int | None = None) -> bytes:
    """Pack a rendered 466x466 image into the native dial container (RGB565 + LZ4 background).

    ``max_match`` (default None): if set, cap every image asset's LZ4 match length to it via
    :func:`cap_lz4_matches` — REQUIRED for near-solid faces or the watch renders black/garbled
    (see :data:`GRID_STATIC_MAX_MATCH`). None = no cap (fine for high-entropy notification art).
    """
    if img.size != (W, H):
        img = img.resize((W, H))
    name = _safe_name(name)

    def _enc(asset_name: str, bmp: bytes) -> bytes:
        a = _encode_image(asset_name, bmp)                # type(1) + dims(3) + lz4 block
        return a if max_match is None else a[:4] + cap_lz4_matches(a[4:], max_match)

    assets = [
        ("dial.json", json.dumps(_manifest(name), separators=(",", ":")).encode("utf-8")),
        ("file.json", json.dumps({"item": [], "fade_item": []},
                                 separators=(",", ":")).encode("utf-8")),
        ("BG_0565.bmp", _enc("BG_0565.bmp", _bmp_bytes(img))),
        ("preview_0565.bmp", _enc("preview_0565.bmp", _bmp_bytes(img.resize((preview_size, preview_size))))),
    ]
    blob = _build_blob(name, assets)
    _parse_blob(blob)   # fail fast: never hand a malformed container to the radio
    return blob


def build_grid_static_blob(watts: float, *, max_w: int = DEFAULT_MAX_W,
                           name: str = "GRIDWATTS", preview_size: int = 32) -> bytes:
    """Render the flat static grid-watts face + pack it as a native dial container.

    The LZ4 match length is capped to GRID_STATIC_MAX_MATCH (HW-CONFIRMED: uncapped/undercapped, the
    near-solid face's long matches render black/garbled on the watch's minimal decoder). At the safe
    cap=512 the full face is ~9-10 KB — OVER the ~8 KB /local/ HTTPS ceiling, so it ships via the
    chunked D-plane path (facepush.push_face), which has no such ceiling. preview_size 32 trims a few
    hundred bytes of the cap overhead.
    """
    return image_to_blob(render_grid_static(watts, max_w=max_w), name=name,
                         preview_size=preview_size, max_match=GRID_STATIC_MAX_MATCH)


# ============================ VENDORED from starmax_client.dialtranscode ======================
_ASSET_RGBA8888 = 0x18
_ASSET_RGB565 = 0x04


def _pack_field(width: int, height: int) -> bytes:
    """The 3-byte u24-LE resolution field: (height << 13) | (width << 2)."""
    if not (0 < width < 2048 and 0 < height < 2048):
        raise ValueError(f"dimensions out of range: {width}x{height}")
    return struct.pack("<I", (height << 13) | (width << 2))[:3]


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


def _asset_type_for(name: str) -> int:
    low = name.lower()
    if low.endswith("_8888.png"):
        return _ASSET_RGBA8888
    if low.endswith("_0565.bmp"):
        return _ASSET_RGB565
    raise ValueError(f"unrecognized image asset suffix: {name!r} (expected *_8888.png or *_0565.bmp)")


def _encode_image(name: str, source_bytes: bytes) -> bytes:
    """Encode one source PNG/BMP into a native image asset (header + lz4 block)."""
    atype = _asset_type_for(name)
    im = Image.open(io.BytesIO(source_bytes))
    w, h = im.size
    if atype == _ASSET_RGBA8888:
        raw = im.convert("RGBA").tobytes()  # R,G,B,A row-major
    else:
        raw = _rgb565_le(im)
    block = _lz4_compress(raw)
    return bytes([atype]) + _pack_field(w, h) + block


# ============================ VENDORED from starmax_client.dialfmt ============================
_MAGIC1 = 0x4321
_MAGIC2 = 0x5AA5
_CONST_A = b"\x06\x04"
_CONST_B = b"\x00\x04"
_NAME_LEN = 30
_ENTRY_LEN = 38
_HEADER_LEN = 0x2C


def _fixed_name(name: str) -> bytes:
    raw = name.encode("ascii")
    if len(raw) >= _NAME_LEN:
        raise ValueError(f"name {name!r} too long for {_NAME_LEN}-byte field")
    return raw + b"\x00" * (_NAME_LEN - len(raw))


def _build_blob(name: str, assets, *, const_a: bytes = _CONST_A, const_b: bytes = _CONST_B) -> bytes:
    """Serialize a native dial blob from (asset_name, payload) pairs (byte-exact container)."""
    count = len(assets)
    data_start = _HEADER_LEN + count * _ENTRY_LEN
    table = bytearray()
    data = bytearray()
    cursor = data_start
    for aname, payload in assets:
        table += _fixed_name(aname) + struct.pack("<II", cursor, len(payload))
        data += payload
        cursor += len(payload)
    body = bytes(table) + bytes(data)
    crc = zlib.crc32(body) & 0xFFFFFFFF
    header = (_fixed_name(name) + struct.pack("<HH", _MAGIC1, _MAGIC2)
              + bytes(const_a) + struct.pack(">H", count) + bytes(const_b)
              + struct.pack("<I", crc))
    return header + body


def _parse_blob(buf: bytes, *, verify_crc: bool = True) -> None:
    """Fail-fast validation only (raises on malformed container); mirrors dialfmt.parse_blob."""
    if len(buf) < _HEADER_LEN:
        raise ValueError(f"too short for a dial header: {len(buf)} < {_HEADER_LEN}")
    magic1, magic2 = struct.unpack_from("<HH", buf, _NAME_LEN)
    if magic1 != _MAGIC1 or magic2 != _MAGIC2:
        raise ValueError(f"bad magic: {magic1:#06x} {magic2:#06x}")
    count = struct.unpack_from(">H", buf, 0x24)[0]
    crc = struct.unpack_from("<I", buf, 0x28)[0]
    if verify_crc:
        calc = zlib.crc32(buf[_HEADER_LEN:]) & 0xFFFFFFFF
        if calc != crc:
            raise ValueError(f"header crc32 mismatch: stored {crc:#010x} != calc {calc:#010x}")
    table_end = _HEADER_LEN + count * _ENTRY_LEN
    if table_end > len(buf):
        raise ValueError(f"asset table ({count} entries) overruns buffer")
