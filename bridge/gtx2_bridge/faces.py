"""Notification-face renderer: arbitrary text/icon -> a minimal 466x466 round watch-face -> the
native dial container the watch installs.

This is the heart of the "notification system": instead of a classic-BT notification (blocked
on an LE-only host, docs/notifications.md), we bake the message straight into a custom
watch-face image and push it with ``dial-push``. The watch shows it as the active face.

Minimal-by-design for a FAST push
----------------------------------
A ``dial-push`` streams every byte over the D-plane write-with-response, so blob size == push
time. We keep the blob tiny:

  * everything (title/body/footer/icon) is **baked into a single background image** — no live
    digit-glyph fonts (those add ~10 PNG assets each);
  * the background is a **solid dark fill** (mostly one colour) encoded as **RGB565** (2 B/px,
    not RGBA's 4) and then **LZ4-compressed** — long runs of the fill colour crush to almost
    nothing. A typical notification face is ~10-15 KB vs a ~231 KB stock dial (≈17x smaller).

Reuse, don't reinvent: the image→native-asset codec (:func:`starmax_client.dialtranscode.encode_image`,
RGB565 + LZ4, byte-exact from a captured install) and the container assembler
(:func:`starmax_client.dialfmt.build_blob`) are the verified library primitives. This module only
renders pixels and lays out the manifest.

Requires Pillow (+ lz4, via the transcoder). Both ship in the starmax-client ``[transcode]`` extra.
"""
from __future__ import annotations

import io
import json
import math
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from starmax_client import dialfmt, dialtranscode

W = H = 466                      # GTX2 screen (docs/watchface-format.md)
PREVIEW = 256                    # on-watch dial-picker thumbnail
CENTER = (W // 2, H // 2)
SAFE_RADIUS = 210                # keep text inside the round bezel

# Bold + regular TTF candidates (same set dial_create.py uses).
_TTF_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
_TTF_REG = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]

_MANIFEST_NAME_MAX = 29          # dialfmt name field is 30 B, NUL-terminated


class FaceError(ValueError):
    """Raised when a notification face cannot be rendered/packed."""


# --------------------------------------------------------------------------- text helpers
def _load_font(size: int, *, bold: bool = False):
    for path in (_TTF_BOLD if bold else _TTF_REG):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    # last-resort: Pillow's bundled bitmap font (fixed size); tests still pass, just plainer.
    try:
        return ImageFont.load_default(size=size)   # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def _text_w(draw: ImageDraw.ImageDraw, s: str, font) -> float:
    return draw.textlength(s, font=font)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> List[str]:
    """Greedy word-wrap ``text`` to lines no wider than ``max_w`` px. Respects existing \\n."""
    lines: List[str] = []
    for para in text.replace("\r", "").split("\n"):
        if not para:
            lines.append("")
            continue
        words = para.split(" ")
        cur = ""
        for word in words:
            trial = word if not cur else f"{cur} {word}"
            if _text_w(draw, trial, font) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
    return lines


def _draw_block(draw, lines, font, cy, fill, line_gap=6) -> int:
    """Draw ``lines`` centred horizontally, vertically centred on ``cy``. Returns bottom y."""
    asc, desc = font.getmetrics()
    lh = asc + desc + line_gap
    total = lh * len(lines)
    y = cy - total // 2
    for ln in lines:
        draw.text((W // 2, y + lh // 2), ln, font=font, fill=fill, anchor="mm")
        y += lh
    return y


def _hex_rgb(s: str) -> Tuple[int, int, int]:
    s = s.lstrip("#")
    if len(s) != 6:
        raise FaceError(f"colour must be #RRGGBB, got {s!r}")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


# --------------------------------------------------------------------------- render
@dataclass
class NotificationFace:
    """A notification to render. All text fields optional except ``title``."""
    title: str
    body: str = ""
    footer: str = ""                     # small dim line at the bottom (e.g. a timestamp)
    icon: Optional[str] = None           # path to a PNG/JPG to composite at the top
    bg: str = "#000000"
    fg: str = "#FFFFFF"
    accent: str = "#00E5FF"              # title colour / divider
    name: str = "NOTIFY"                 # dial internal name (<=29 ASCII)


def render_image(face: NotificationFace) -> Image.Image:
    """Render ``face`` to a 466x466 RGB image (solid bg + baked text/icon)."""
    bg = _hex_rgb(face.bg)
    fg = _hex_rgb(face.fg)
    accent = _hex_rgb(face.accent)
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)

    max_w = int(SAFE_RADIUS * 1.9)
    cy = CENTER[1]
    top = 90

    if face.icon:
        try:
            ic = Image.open(face.icon).convert("RGBA").resize((96, 96))
            img.paste(ic, (W // 2 - 48, top), ic)
            top += 110
        except (OSError, ValueError) as e:  # bad path/format shouldn't kill the notification
            raise FaceError(f"cannot load icon {face.icon!r}: {e}") from e

    # Title (bold, accent) near the top; body centred; footer dim at the bottom.
    title_font = _load_font(46, bold=True)
    body_font = _load_font(30)
    footer_font = _load_font(22)

    title_lines = _wrap(d, face.title, title_font, max_w)
    title_bottom = _draw_block(d, title_lines, title_font,
                               top + 40 if not face.icon else top, accent)

    if face.body:
        body_lines = _wrap(d, face.body, body_font, max_w)
        # centre the body in the gap between the title and the footer
        body_cy = max(cy, title_bottom + 60)
        _draw_block(d, body_lines, body_font, body_cy, fg)

    if face.footer:
        footer_lines = _wrap(d, face.footer, footer_font, max_w)
        _draw_block(d, footer_lines, footer_font, H - 70, tuple(int(c * 0.6) for c in fg))

    return img


# --------------------------------------------------------------------------- pack -> native blob
def _safe_name(name: str) -> str:
    ascii_name = "".join(c for c in name if 32 <= ord(c) < 127) or "NOTIFY"
    return ascii_name[:_MANIFEST_NAME_MAX]


def _manifest(name: str) -> dict:
    """A minimal, valid dial.json: one full-screen background item (no live fields).

    Mirrors scripts/dial_create.py's vocabulary (466x466, ats3085s, dial_version 2). ``app_preview``
    is referenced but never shipped — the transcoder drops it (phone-only asset), matching the
    captured install.
    """
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


# LZ4-block match-length cap — HW-CRITICAL for near-solid faces.
# HW-PROVEN on the spare 2026-07-15: the GTX2's minimal LZ4 decoder GARBLES (renders black/corrupt)
# any image asset whose LZ4 has a match longer than ~this. A near-solid face (e.g. the grid gauge on
# a black bg) compresses to matches up to ~178 KB; the captured vendor dial's longest match is 858 B,
# so the watch is never tested past ~1 KB.
# ⚠️ cap=2048 was MARGINAL and is RETIRED: it rendered clean by luck (some layouts' 2048-length
# matches decode, others garble) — a verified-full CRC-valid v2 face STILL corrupted at 2048, and the
# live auto-refresh corrupted daily on a re-push at a new watts value (different match pattern). The
# watch's real limit is < 2048. cap=512 (well under the vendor-proven-good 858) is HW-CONFIRMED SAFE.
# cap_lz4_matches splits long matches into short ones, byte-identical on decode — and is COMPRESSOR-
# AGNOSTIC (operates on the finished block) so the HA custom_component applies the same cap after cramjam.
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
                  max_match: Optional[int] = None) -> bytes:
    """Pack a rendered 466x466 image into the native dial container (RGB565 + LZ4 background).

    Assets: ``dial.json`` + ``file.json`` (verbatim) + ``BG_0565.bmp`` + ``preview_0565.bmp``
    (both RGB565/LZ4 via the verified :func:`dialtranscode.encode_image`). Returns bytes that
    :func:`starmax_client.dialfmt.parse_blob` accepts and ``dial-push`` streams.

    ``preview_size`` is the dial-picker thumbnail edge (default 256). Shrinking it is a real byte
    lever for the /local/ HTTPS path: a 256px preview costs ~2 KB LZ4, a 48px one ~0.4 KB.

    ``max_match`` (default None): if set, cap every image asset's LZ4 match length to it via
    :func:`cap_lz4_matches` — REQUIRED for near-solid faces or the watch renders black/garbled
    (see :data:`GRID_STATIC_MAX_MATCH`). None = no cap (fine for high-entropy notification art).
    """
    if img.size != (W, H):
        img = img.resize((W, H))
    name = _safe_name(name)

    def _enc(asset_name: str, bmp: bytes) -> bytes:
        a = dialtranscode.encode_image(asset_name, bmp)   # type(1) + dims(3) + lz4 block
        return a if max_match is None else a[:4] + cap_lz4_matches(a[4:], max_match)

    assets = [
        ("dial.json", json.dumps(_manifest(name), separators=(",", ":")).encode("utf-8")),
        ("file.json", json.dumps({"item": [], "fade_item": []},
                                 separators=(",", ":")).encode("utf-8")),
        ("BG_0565.bmp", _enc("BG_0565.bmp", _bmp_bytes(img))),
        ("preview_0565.bmp", _enc("preview_0565.bmp", _bmp_bytes(img.resize((preview_size, preview_size))))),
    ]
    blob = dialfmt.build_blob(name, assets)
    dialfmt.parse_blob(blob)   # fail fast: never hand a malformed container to the radio
    return blob


def build_notification_blob(title: str, body: str = "", footer: str = "", *,
                            icon: Optional[str] = None, bg: str = "#000000",
                            fg: str = "#FFFFFF", accent: str = "#00E5FF",
                            name: str = "NOTIFY") -> bytes:
    """Render a notification and pack it into a native dial blob (one call)."""
    face = NotificationFace(title=title, body=body, footer=footer, icon=icon,
                            bg=bg, fg=fg, accent=accent, name=name)
    return image_to_blob(render_image(face), name=name)


# =============================================================================
# Grid-watts showcase face  (rendered watts gauge bg + live native widgets)
# =============================================================================
# The grid-power value has NO native dial.json binding (CDN weather-binding verdict = NO), so it is
# BAKED into the arc-gauge background and the face is re-rendered/re-pushed on a cadence (blobd
# render-on-fetch: GET /gauge.bin?w=<signed_watts>&max=<full_scale>). Clock / date / heart / step /
# battery ARE native firmware bindings, so they ride as live dial.json widgets (zero push, always
# current between watt re-pushes) — the payoff of the live-widget builder.
_GRID_BG = (5, 7, 10)            # near-black — long RGB565 runs crush under LZ4
_GRID_TRACK = (28, 36, 48)       # dim gauge track
_GRID_IMPORT = (255, 59, 48)     # red — pulling FROM grid (>=0)
_GRID_EXPORT = (52, 199, 89)     # green — reserved for a future signed/export sensor (v1 = import-only)
_GRID_IDLE = (120, 130, 145)     # |w| < IDLE_W
_GRID_LABEL = (120, 132, 150)    # dim labels
_GRID_STAT = (200, 208, 220)     # shared neutral stat digits (one glyph font, not three)
_GRID_WHITE = (255, 255, 255)
_GRID_IDLE_W = 15                # |watts| below this reads IDLE (neutral)
DEFAULT_MAX_W = 12000            # full-scale watts (JP-set 0-12 kW gauge scale). Override via ?max=

# geometry (466x466). Live-widget cells are kept DARK in the bg so the native text stays legible.
_CLK_Y, _CLK_H, _HR_X, _MIN_X, _DIG_W = 34, 64, 116, 266, 84
_DATE_X, _DATE_Y, _DATE_W, _DATE_H = 207, 112, 52, 30
_G_CX, _G_CY, _G_R, _G_ARC_W = 233, 250, 134, 20
_G_START, _G_SWEEP = 135, 270    # 270 deg, 90 deg gap centred on the bottom
_NUM_Y = 236
_STAT_Y, _STAT_H, _STAT_W = 392, 34, 84
_HEART_CX, _STEP_CX, _BATT_CX = 100, 233, 366


def _fmt_watts(watts: float) -> str:
    """934 -> '934', 1293 -> '1293', 12800 -> '12.8k'."""
    a = abs(int(round(watts)))
    return f"{a / 1000:.1f}k" if a >= 10000 else str(a)


def render_grid_gauge(watts: float, *, max_w: int = DEFAULT_MAX_W) -> Image.Image:
    """Render the 466x466 grid-power gauge background (art + baked watts; live-widget cells dark).

    ``watts`` signed: >=0 IMPORT (red, arrow into house), <0 EXPORT (green, arrow to grid),
    ``|watts| < _GRID_IDLE_W`` IDLE (neutral). ``max_w`` sets the arc fill fraction.
    """
    import datetime
    img = Image.new("RGB", (W, H), _GRID_BG)
    d = ImageDraw.Draw(img)

    mag = abs(float(watts))
    frac = max(0.0, min(1.0, mag / float(max_w or DEFAULT_MAX_W)))
    # magnitude-only v1 (this system never exports — no signed/net sensor exists): single import
    # colour, no direction word/arrow. The signed `watts` param is kept for a future export sensor.
    col = _GRID_IDLE if mag < _GRID_IDLE_W else _GRID_IMPORT

    bbox = [_G_CX - _G_R, _G_CY - _G_R, _G_CX + _G_R, _G_CY + _G_R]
    d.arc(bbox, _G_START, _G_START + _G_SWEEP, fill=_GRID_TRACK, width=_G_ARC_W)   # track
    if frac > 0:                                                                   # filled proportion
        d.arc(bbox, _G_START, _G_START + _G_SWEEP * frac, fill=col, width=_G_ARC_W)
        end = math.radians(_G_START + _G_SWEEP * frac)                             # end-cap dot
        ex, ey = _G_CX + _G_R * math.cos(end), _G_CY + _G_R * math.sin(end)
        r = _G_ARC_W // 2 + 2
        d.ellipse([ex - r, ey - r, ex + r, ey + r], fill=col)

    # big watts number (auto-shrink to fit) + unit. No direction word/arrow (magnitude-only).
    num = _fmt_watts(watts)
    size = 116
    while size > 60 and d.textlength(num, font=_load_font(size, bold=True)) > 250:
        size -= 6
    d.text((_G_CX, _NUM_Y), num, font=_load_font(size, bold=True), fill=col, anchor="mm")
    d.text((_G_CX, _NUM_Y + 62), "WATTS", font=_load_font(24, bold=True), fill=_GRID_LABEL, anchor="mm")

    # baked static overlays (the live widgets draw their numbers on top of these):
    d.text((_G_CX, _CLK_Y + _CLK_H // 2 - 4), ":", font=_load_font(60, bold=True),
           fill=_GRID_WHITE, anchor="mm")                                          # clock colon
    now = datetime.datetime.now()
    d.text((_DATE_X - 34, _DATE_Y + _DATE_H // 2), now.strftime("%a").upper(),      # weekday (baked)
           font=_load_font(22, bold=True), fill=_GRID_LABEL, anchor="mm")
    d.text((_DATE_X + _DATE_W + 34, _DATE_Y + _DATE_H // 2), now.strftime("%b").upper(),  # month (baked)
           font=_load_font(22, bold=True), fill=_GRID_LABEL, anchor="mm")
    lf = _load_font(18, bold=True)                                                  # stat labels (baked)
    for cx, lab in ((_HEART_CX, "HR"), (_STEP_CX, "STEPS"), (_BATT_CX, "BATT")):
        d.text((cx, _STAT_Y + _STAT_H + 12), lab, font=lf, fill=_GRID_LABEL, anchor="mm")
    return img


def _grid_widgets() -> List[dict]:
    """Live native dial.json widgets overlaid on the gauge: clock HH:MM + date + heart + step +
    battery. HR/step/battery share ONE neutral glyph font (``Stat``) to avoid three 10-glyph font
    folders (~10 KB saved) — colour is carried by the baked labels, not the digits."""
    def _txt(binding, cx, y, w, h, *, font, color, nw=1):
        return {"widget": "text", "type": binding, "x": cx - w // 2, "y": y, "w": w, "h": h,
                "color": color, "align": "center", "min_numwidth": nw, "font": font}
    white = "#FFFFFF"
    stat = "#C8D0DC"
    return [
        _txt("hour", (_HR_X + _DIG_W // 2), _CLK_Y, _DIG_W, _CLK_H, font="Clock", color=white, nw=2),
        _txt("min", (_MIN_X + _DIG_W // 2), _CLK_Y, _DIG_W, _CLK_H, font="Clock", color=white, nw=2),
        _txt("date", (_DATE_X + _DATE_W // 2), _DATE_Y, _DATE_W, _DATE_H, font="Date",
             color="#9AA4B2", nw=2),
        _txt("heart", _HEART_CX, _STAT_Y, _STAT_W, _STAT_H, font="Stat", color=stat),
        _txt("step", _STEP_CX, _STAT_Y, _STAT_W, _STAT_H, font="Stat", color=stat),
        _txt("battery", _BATT_CX, _STAT_Y, _STAT_W, _STAT_H, font="Stat", color=stat),
    ]


def build_grid_face_blob(watts: float, *, max_w: int = DEFAULT_MAX_W,
                         name: str = "GRIDWATTS", preview_size: int = 128) -> bytes:
    """Render the grid-watts gauge at ``watts`` + pack it with live clock/date/heart/step/battery
    widgets into the native dial container (one call). This is what ``GET /gauge.bin`` serves."""
    from starmax_client import dialface   # live-widget builder (bg + dial.json overlay)
    img = render_grid_gauge(watts, max_w=max_w)
    return dialface.build_dial_face(img, _grid_widgets(), name=_safe_name(name),
                                    dial_type=1, preview_size=preview_size)


# --- FLAT STATIC grid face  (compact, no glyph fonts — ships via Track B) ----------------------
# The full-art arc gauge above is ~36 KB live / ~21 KB static. This FLAT STATIC variant (solid bg +
# HARD-EDGED thresholded watts digits + a filled BAR gauge + a small preview, no glyph-font folders)
# is far smaller — but at the HW-SAFE LZ4 cap=512 (see GRID_STATIC_MAX_MATCH) it lands ~9-10 KB, OVER
# the ~8 KB /local/ HTTPS OOM ceiling, so it ships via the chunked D-plane path (TRACK B). (History:
# it was briefly on /local/ at cap=2048, but 2048 turned out MARGINAL and corrupted on-watch.) Fully
# static → "live" = a throttled RE-PUSH on watt change (2026-07-15). WATTS-ONLY (a baked
# clock adds fonts/size; a live clock is the separate date-fields face). Delivery = the chunked pusher.
_GRID_S_NUM = 84                 # watts glyph height — kept moderate so the capped face stays compact


def _hard_text(img: Image.Image, cx: int, cy: int, s: str, size: int, color, *,
               anchor: str = "mm", thresh: int = 96) -> None:
    """Paint hard-edged (no-AA) text: render to an L mask, threshold it, paste a solid fill.

    Anti-aliased glyph edges are the LZ4 entropy killer; a 2-value mask keeps the background in
    long runs so a full-canvas number still compresses into the < 8 KB budget."""
    layer = Image.new("L", (W, H), 0)
    ImageDraw.Draw(layer).text((cx, cy), s, font=_load_font(size, bold=True), fill=255, anchor=anchor)
    img.paste(Image.new("RGB", (W, H), color), (0, 0), layer.point(lambda p: 255 if p > thresh else 0))


def render_grid_static(watts: float, *, max_w: int = DEFAULT_MAX_W) -> Image.Image:
    """Render the FLAT < 8 KB grid-watts face: hard-edged watts number + WATTS label + bar gauge.

    ``watts`` magnitude drives the number + bar fill; colour is import-red (idle-grey < 15 W). No
    baked clock (doesn't fit the budget) and no live widgets — re-push on value change for updates.
    """
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


def build_grid_static_blob(watts: float, *, max_w: int = DEFAULT_MAX_W,
                           name: str = "GRIDWATTS", preview_size: int = 32) -> bytes:
    """Render the flat static grid-watts face + pack it as a native dial container.

    Static bg-only container (no live widgets) — re-push to update the value.

    The LZ4 match length is capped to GRID_STATIC_MAX_MATCH (HW-CONFIRMED: uncapped/undercapped, the
    near-solid face's long matches render black/garbled on the watch's minimal decoder). At the safe
    cap=512 the full face is ~9-10 KB — OVER the ~8 KB /local/ HTTPS ceiling, so it ships via the
    chunked D-plane path (TRACK B), which has no such ceiling. preview_size 32 trims a few hundred
    bytes of the cap overhead.
    """
    return image_to_blob(render_grid_static(watts, max_w=max_w), name=name,
                         preview_size=preview_size, max_match=GRID_STATIC_MAX_MATCH)


# --- LIVE-kW grid face  (real clock + "N kW" via set_time, NO image re-push) --------------------
# The flat static face above re-pushes the WHOLE image on every watt change. THIS variant pushes the
# image ONCE, then updates the value with a tiny set_time command: the digits are LIVE native text
# widgets, and the watts ride in an RTC field the face doesn't otherwise use — day = INTEGER kW —
# while hour/min stay the real clock. So it shows a real HH:MM clock AND a live "N kW" readout that
# refreshes for the cost of one 0x02 frame (see gtx2_client::set_time_custom + the encode_grid_live
# spec below + packages/gtx2_gridkw_live.yaml).
# Delivery is Track B (chunked): with the auto-generated 0-9 glyph fonts it is ~33 KB, over the
# /local/ ceiling.  ⚠️ INTEGER kW (JP design 2026-07-16): the old tenths-in-`second` decimal truncated
# on the narrow date-widget viewport — dropped for a big readable integer HERO (0-12, 2-digit cell).
# `second` is now unused. Floors at 1 kW because the RTC `day` field can't be 0. Match-cap = static face.
_GKL_HDR = (150, 160, 175)                              # dim "GRID" header text
_GKL_CLK_Y, _GKL_CLK_H, _GKL_CLK_DW = 118, 108, 136     # clock digit cell (hour/min): top-y, height, per-pair width.
#   DW widened 96->136: a 2-digit pair renders at glyph_w=int(h*0.6)=64 => 128 px, which CLIPPED the old
#   96 px cell bbox (JP on-glass). 136 fits 128 + padding.
_GKL_HR_X, _GKL_MIN_X = 81, 249                         # left x of hour / min pair — repositioned for DW=136 around the centre colon
_GKL_KW_Y, _GKL_KW_H, _GKL_KW_CELL_W = 296, 88, 118     # kW HERO row: top-y, height, 2-digit cell width
#   CELL_W widened 104->118: a 2-digit value renders glyph_w=int(88*0.6)=52 => 104, which exactly
#   filled the old 104 cell (edge-clip risk); 118 gives padding so nothing clips.
_GKL_BATT_RING = dict(x=8, y=8, w=450, h=450, start=225, end=135, color="#3FA35C", width=8)  # battery %% edge-arc (green, outer)
_GKL_KW_RING = dict(x=26, y=26, w=414, h=414, start=225, end=135, color="#FF3B30", width=8)   # kW gauge (red), concentric just INSIDE the battery ring.
#   Bound to `day` (the int-kW carrier) — matches the red kW hero number. ⚠️ Fill is firmware-scaled by the
#   binding's natural range (day → day/31), so 0-12 kW fills ~0-39% of the sweep (grows with load, won't top
#   out). If JP wants full-scale-at-max we'd need a different scaling; on-glass call.
_GKL_INT_X = 185                                        # centre x of the integer-kW digit cell (shifted left for unit clearance)
_GKL_UNIT_X = 290                                       # centre x of the baked "kW" unit — spaced clear of a 2-digit number (was 272, too close)
# HR complication (JP add 2026-07-16): SMALL live BPM + baked heart icon, tucked in the ring's lower
# gap BELOW the kW hero so it never competes with it. The number reuses the WHITE `Clk` glyph font
# (zero new assets — see build_dial_face's per-name font dedup) at a small cell; the heart is a red
# bg-baked icon. kW stays the sole big-red hero.
_GKL_HR_Y, _GKL_HR_H, _GKL_HR_CELL_W = 405, 32, 100     # HR row: top-y, cell height (small), BPM cell width.
#   HR uses a DEDICATED small glyph font `Hr` (NOT the reused Clk). On HW the fw renders each font's
#   glyphs at their NATIVE generated size and does NOT scale to the cell — Clk is generated at the hero
#   clock height (64px/digit), so reusing it rendered the BPM clock-sized ("much too big", JP on-glass
#   2026-07-16; widening the cell only exposed the full-size digits). A dedicated `Hr` font generates
#   small digits (glyph_w=int(h*0.6)=19 at h=32) → a true small complication. Cell w=100 fits a 3-digit
#   BPM (~57px) + margin; centre-aligned. Costs one small glyph font (~+3 KB), well within install envelope.
_GKL_HR_NUM_X = 258                                     # centre x of the live BPM number cell (right of the heart)
_GKL_HR_GLYPH = dict(cx=196, cy=419, color="#FF3B30")   # baked heart icon (small, red) left of the BPM


def render_grid_live() -> Image.Image:
    """Render the LIVE-kW face BACKGROUND (value- AND time-independent): the "GRID" header, the clock
    colon and the "kW" unit. The changing digits (HH:MM and the integer kW) are LIVE widgets the
    firmware draws on top — the value arrives via set_time_custom — so this art is built ONCE and never
    re-pushed (hence no ``now()`` / ``watts`` here: pure, deterministic output)."""
    img = Image.new("RGB", (W, H), _GRID_BG)
    d = ImageDraw.Draw(img)
    d.text((_G_CX, 74), "GRID", font=_load_font(26, bold=True), fill=_GKL_HDR, anchor="mm")   # header
    d.text((_G_CX, _GKL_CLK_Y + _GKL_CLK_H // 2 - 4), ":", font=_load_font(84, bold=True),
           fill=_GRID_WHITE, anchor="mm")                                              # clock colon
    d.text((_GKL_UNIT_X, _GKL_KW_Y + _GKL_KW_H // 2), "kW", font=_load_font(42, bold=True),
           fill=_GRID_LABEL, anchor="mm")                                              # unit (no decimal)
    hg = _GKL_HR_GLYPH                                                                  # baked heart icon (HR complication)
    hx, hy = hg["cx"], hg["cy"]                                                         # two top lobes + a bottom point
    d.ellipse([hx - 15, hy - 10, hx + 1, hy + 4], fill=hg["color"])                     #   left lobe
    d.ellipse([hx - 1, hy - 10, hx + 15, hy + 4], fill=hg["color"])                     #   right lobe
    d.polygon([(hx - 14, hy - 1), (hx + 14, hy - 1), (hx, hy + 14)], fill=hg["color"])  #   bottom point
    return img


def _grid_live_widgets() -> List[dict]:
    """SIX LIVE native widgets: HH + MM real clock (font ``Clk``, white) + the INTEGER-kW hero (bound
    to the ``day`` field, font ``Kw``, import-red, 2-digit cell) + a small HR BPM readout (bound to
    ``heart``, on its own small white ``Hr`` glyph font) + a battery %% edge-ring (``arc`` bound to
    ``battery``, green, outer) + a kW gauge-ring (``arc`` bound to ``day``, red, concentric inside the
    battery ring). No tenths widget — the value is whole kW (see :func:`encode_grid_live`); the RTC
    fields are read via set_time_custom, and heart/battery are the watch's own sensors (live, no push).
    ⚠️ HR needs its OWN font, NOT the reused ``Clk``: the firmware renders each font's glyphs at their
    native generated size (it does NOT scale to the cell), so reusing the hero-sized ``Clk`` rendered
    the BPM clock-sized on glass. A dedicated ``Hr`` font (generated at the small HR cell height) is the
    only way to get small digits."""
    def _txt(binding, cx, y, w, h, *, font, color, nw=1):
        return {"widget": "text", "type": binding, "x": cx - w // 2, "y": y, "w": w, "h": h,
                "color": color, "align": "center", "min_numwidth": nw, "font": font}
    white, kw = "#FFFFFF", "#FF3B30"
    b = _GKL_BATT_RING
    k = _GKL_KW_RING
    return [
        _txt("hour", _GKL_HR_X + _GKL_CLK_DW // 2, _GKL_CLK_Y, _GKL_CLK_DW, _GKL_CLK_H,
             font="Clk", color=white, nw=2),
        _txt("min", _GKL_MIN_X + _GKL_CLK_DW // 2, _GKL_CLK_Y, _GKL_CLK_DW, _GKL_CLK_H,
             font="Clk", color=white, nw=2),
        _txt("day", _GKL_INT_X, _GKL_KW_Y, _GKL_KW_CELL_W, _GKL_KW_H,
             font="Kw", color=kw, nw=1),    # integer kW hero on `day` (day-of-month 1-31; kW 0-12 fits).
        #                                     NOT `date` — the compound field that garbled to ~70.
        # HR complication: live BPM on the native `heart` sensor (read-only, no feed), on its OWN small
        # white `Hr` glyph font (the fw renders glyphs at native size — reusing the hero Clk font made
        # the BPM clock-sized on glass; a dedicated small font is the only way to get small digits).
        # Sits quiet next to the baked red heart icon so the big red kW stays the hero.
        _txt("heart", _GKL_HR_NUM_X, _GKL_HR_Y, _GKL_HR_CELL_W, _GKL_HR_H,
             font="Hr", color=white, nw=1),
        # battery %% edge-ring: native `arc` bound to `battery` (the watch's own charge sensor →
        # populates LIVE, no push/feed). Full-canvas inset, top arc w/ bottom gap, clear of clock + kW.
        {"widget": "arc", "type": "battery", "x": b["x"], "y": b["y"], "w": b["w"], "h": b["h"],
         "start_angle": b["start"], "end_angle": b["end"], "color": b["color"], "arc_width": b["width"]},
        # kW gauge-ring: red `arc` bound to `day` (the int-kW carrier), concentric just inside the battery
        # ring. Fills with load (firmware scales day/31 → see _GKL_KW_RING note). No extra feed — rides the
        # same day=kW that drives the hero number.
        {"widget": "arc", "type": "day", "x": k["x"], "y": k["y"], "w": k["w"], "h": k["h"],
         "start_angle": k["start"], "end_angle": k["end"], "color": k["color"], "arc_width": k["width"]},
    ]


def _cap_blob_assets(blob: bytes, cap: int) -> bytes:
    """Cap the LZ4 match length of EVERY image asset in a FINISHED dial container to ``cap``.

    :func:`image_to_blob` caps during encode, but faces built via
    :func:`starmax_client.dialface.build_dial_face` (background + auto-generated 0-9 glyph fonts) have
    no per-asset hook — so parse the finished container, re-cap each ``.bmp``/``.png`` asset's LZ4
    block, and rebuild. Decode is byte-identical (see :func:`cap_lz4_matches` / :data:`GRID_STATIC_MAX_MATCH`)."""
    parsed = dialfmt.parse_blob(blob)
    assets: List[Tuple[str, bytes]] = []
    for asset_name in parsed.asset_names:
        b = parsed.get(asset_name)
        b = b.encode() if isinstance(b, str) else b
        if asset_name.endswith((".bmp", ".png")):
            b = b[:4] + cap_lz4_matches(b[4:], cap)      # keep type(1)+dims(3) header, re-cap the block
        assets.append((asset_name, b))
    return dialfmt.build_blob(parsed.name, assets)


def encode_grid_live(watts: float, *, now=None) -> dict:
    """Map a watts reading to the set_time_custom fields the LIVE-kW face reads: real ``hour`` /
    ``minute`` (the clock) plus repurposed ``day`` = INTEGER kW. ``second`` is unused (always 0).

    ``day = max(1, int(|watts|/1000 + 0.5))`` — whole kW, HALF-UP, floored to 1 because the RTC
    ``day`` field can't be 0 (so <500 W reads "1 kW"). INTEGER not tenths (JP design 2026-07-16: the
    decimal truncated on the narrow viewport). ⚠️ Uses explicit ``int(x+0.5)`` (half-up), NOT Python
    ``round()`` (banker's) — they DIVERGE at exact .5-kW boundaries (2500 W: banker's→2 vs half-up→3),
    and HA's Jinja round doesn't match Python's across versions. All three surfaces (this, the yaml,
    the HA automation's feed) use the same explicit ``int(x+0.5)`` so the contract is environment-independent
    (packages/gtx2_gridkw_live.yaml → set_time_custom)."""
    import datetime
    now = now or datetime.datetime.now()
    day = max(1, int(abs(watts) / 1000.0 + 0.5))
    return {"hour": now.hour, "minute": now.minute, "second": 0, "day": day}


def build_grid_live_blob(*, name: str = "GRIDKW", preview_size: int = 48,
                         max_match: int = GRID_STATIC_MAX_MATCH) -> bytes:
    """Build the LIVE-kW face container: the :func:`render_grid_live` background + the four live text
    widgets (HH + MM clock + the integer-kW hero + the small HR BPM) + the battery arc, then the LZ4
    match cap applied to every image asset (bg + the glyph PNGs: the Clk clock font + Kw hero font +
    the small Hr BPM font — three digit fonts, ~30 glyph PNGs).

    Built ONCE — the displayed value then updates via ``set_time_custom`` (see :func:`encode_grid_live`),
    not by re-pushing this ~33 KB blob. Ships via the chunked D-plane (Track B); over the /local/ ceiling.
    """
    from starmax_client import dialface   # live-widget builder (bg + auto-generated glyph fonts)
    blob = dialface.build_dial_face(render_grid_live(), _grid_live_widgets(),
                                    name=_safe_name(name), dial_type=1, preview_size=preview_size)
    return _cap_blob_assets(blob, max_match)


# --- DECIMAL live-kW face  ("X.X kW" — JP decision 2026-07-16 "go 1.1 kW") -------------------------
# The integer face above shows whole kW on `day`. This variant adds ONE tenths digit on `month`, the
# SECOND raw value carrier proven clean on-glass by the digit test (2026-07-16: month=8 → "8"; `week`
# is NOT usable — the firmware derives weekday from the date). Layout: [int kW]·baked "."·[tenths]·"kW".
# The tenths reuse the existing red `Kw` hero font (same colour/size, ZERO new assets → still ~33 KB).
#   ⚠️ RTC `month` can't be 0, so tenths=0 (an exact "X.0 kW") is the one open case — encode_grid_live_
#   decimal pushes month=tenths and the .0 rendering is being confirmed on-glass; if month=0 clamps
#   instead of rendering "0", the fallback is an offset/array tenths (see build note). day still floors
#   at 1 kW (RTC day != 0), so sub-1 kW draw reads "1.x".
_GKL_DP_X = 205                                         # baked red decimal-point centre x
_GKL_INT_BOX = dict(x=84, w=116)                        # integer-kW cell — RIGHT-aligned, hugs the ".", fits 2 digits (0-12)
_GKL_TEN_BOX = dict(x=214, w=58)                        # tenths cell — LEFT-aligned, one digit
_GKL_DUNIT_X = 300                                      # "kW" unit centre x (clear right of the tenths)


def render_grid_live_decimal() -> Image.Image:
    """BACKGROUND for the DECIMAL live-kW face (value-/time-independent, built once): "GRID" header,
    clock colon, baked RED "." and "kW". The digits (HH:MM + integer kW + tenths) are LIVE widgets the
    firmware draws on top; the value arrives via set_time_multi (day=int kW, month=tenths)."""
    img = Image.new("RGB", (W, H), _GRID_BG)
    d = ImageDraw.Draw(img)
    d.text((_G_CX, 74), "GRID", font=_load_font(26, bold=True), fill=_GKL_HDR, anchor="mm")   # header
    d.text((_G_CX, _GKL_CLK_Y + _GKL_CLK_H // 2 - 4), ":", font=_load_font(84, bold=True),
           fill=_GRID_WHITE, anchor="mm")                                              # clock colon
    d.text((_GKL_DP_X, _GKL_KW_Y + _GKL_KW_H // 2 + 22), ".", font=_load_font(60, bold=True),
           fill="#FF3B30", anchor="mm")                                                # red decimal point
    d.text((_GKL_DUNIT_X, _GKL_KW_Y + _GKL_KW_H // 2), "kW", font=_load_font(42, bold=True),
           fill=_GRID_LABEL, anchor="mm")                                              # unit
    hg = _GKL_HR_GLYPH                                                                  # baked heart icon (HR complication)
    hx, hy = hg["cx"], hg["cy"]
    d.ellipse([hx - 15, hy - 10, hx + 1, hy + 4], fill=hg["color"])
    d.ellipse([hx - 1, hy - 10, hx + 15, hy + 4], fill=hg["color"])
    d.polygon([(hx - 14, hy - 1), (hx + 14, hy - 1), (hx, hy + 14)], fill=hg["color"])
    return img


def _grid_live_decimal_widgets() -> List[dict]:
    """Live widgets for the DECIMAL kW face: HH+MM real clock (Clk, white) + integer kW (``day``, red
    ``Kw`` font, RIGHT-aligned so it hugs the baked ".") + tenths (``month``, same ``Kw`` font,
    LEFT-aligned) + small HR BPM (``heart``, ``Hr``) + battery %% ring (``arc``/battery, green) + kW
    gauge-ring (``arc``/day, red). day+month are the two proven raw carriers; reusing ``Kw`` for both
    keeps the asset count (and byte size) identical to the integer face."""
    white, kw = "#FFFFFF", "#FF3B30"
    b, k = _GKL_BATT_RING, _GKL_KW_RING
    ib, tb = _GKL_INT_BOX, _GKL_TEN_BOX
    return [
        {"widget": "text", "type": "hour", "x": _GKL_HR_X, "y": _GKL_CLK_Y, "w": _GKL_CLK_DW,
         "h": _GKL_CLK_H, "color": white, "align": "center", "min_numwidth": 2, "font": "Clk"},
        {"widget": "text", "type": "min", "x": _GKL_MIN_X, "y": _GKL_CLK_Y, "w": _GKL_CLK_DW,
         "h": _GKL_CLK_H, "color": white, "align": "center", "min_numwidth": 2, "font": "Clk"},
        # integer kW — right-aligned against the "." ; tenths — left-aligned after it. Both red `Kw`.
        {"widget": "text", "type": "day", "x": ib["x"], "y": _GKL_KW_Y, "w": ib["w"], "h": _GKL_KW_H,
         "color": kw, "align": "right", "min_numwidth": 1, "font": "Kw"},
        {"widget": "text", "type": "month", "x": tb["x"], "y": _GKL_KW_Y, "w": tb["w"], "h": _GKL_KW_H,
         "color": kw, "align": "left", "min_numwidth": 1, "font": "Kw"},
        # HR complication (small white `Hr`), battery + kW arcs — identical to the integer face.
        {"widget": "text", "type": "heart", "x": _GKL_HR_NUM_X - _GKL_HR_CELL_W // 2, "y": _GKL_HR_Y,
         "w": _GKL_HR_CELL_W, "h": _GKL_HR_H, "color": white, "align": "center",
         "min_numwidth": 1, "font": "Hr"},
        {"widget": "arc", "type": "battery", "x": b["x"], "y": b["y"], "w": b["w"], "h": b["h"],
         "start_angle": b["start"], "end_angle": b["end"], "color": b["color"], "arc_width": b["width"]},
        {"widget": "arc", "type": "day", "x": k["x"], "y": k["y"], "w": k["w"], "h": k["h"],
         "start_angle": k["start"], "end_angle": k["end"], "color": k["color"], "arc_width": k["width"]},
    ]


def encode_grid_live_decimal(watts: float, *, now=None) -> dict:
    """Map watts to the set_time_multi fields the DECIMAL face reads: real ``hour``/``minute`` + ``day``
    = integer kW + ``month`` = tenths kW. Rounds to 0.1 kW HALF-UP via deci-kW ``int(kw*10 + 0.5)``
    (same explicit half-up contract as :func:`encode_grid_live`), then splits: ``day = max(1, dkw//10)``
    (floored — RTC day != 0), ``tenths = dkw % 10``. ``week`` is passed through as the real weekday
    (it's firmware-derived from the date anyway — not a carrier). NOTE the ``month`` == 0 (exact X.0 kW)
    open case — see the module build note."""
    import datetime
    now = now or datetime.datetime.now()
    dkw = int(abs(watts) / 1000.0 * 10 + 0.5)           # deci-kW, half-up (1149 W -> 11)
    day = max(1, dkw // 10)
    tenths = dkw % 10
    return {"hour": now.hour, "minute": now.minute, "second": 0, "day": day, "month": tenths,
            "week": now.weekday()}


def build_grid_live_decimal_blob(*, name: str = "GRIDKWD", preview_size: int = 48,
                                 max_match: int = GRID_STATIC_MAX_MATCH) -> bytes:
    """Build the DECIMAL live-kW container ("X.X kW"): :func:`render_grid_live_decimal` background +
    :func:`_grid_live_decimal_widgets` (clock + day int-kW + month tenths + HR + battery/kW arcs), then
    the LZ4 match cap on every asset. Same 3 glyph fonts as the integer face (Clk/Kw/Hr) — Kw is shared
    by the integer digit and the tenths, so ~33 KB / 24 assets, unchanged. Value updates via
    set_time_multi (:func:`encode_grid_live_decimal`); ships Track B (chunked, over the /local/ ceiling)."""
    from starmax_client import dialface
    blob = dialface.build_dial_face(render_grid_live_decimal(), _grid_live_decimal_widgets(),
                                    name=_safe_name(name), dial_type=1, preview_size=preview_size)
    return _cap_blob_assets(blob, max_match)


# --- COMPACT live-kW face  (single shared font — the guaranteed-installable "safe lander") ---------
# build_grid_live_blob above is ~33 KB / 24 assets (a WHITE 108px clock font + a separate RED 64px kW
# font) and was REJECTED on install (task #25 — leading theory: FREE-FLASH exhaustion; the watch
# refuses a valid 33 KB container when free < 33 KB, while the 12 KB minclock installs fine). This
# COMPACT variant collapses all four widgets onto ONE shared white glyph font of uniform height —
# exactly the minclock dedup that is HW-PROVEN to install — dropping a whole 10-glyph font (24 -> 14
# assets) to ~halve the container to ~14-16 KB. The red "." + "kW" accents stay BAKED in the bg so the
# grid identity survives; only the digits go uniform-white (glyph PNGs bake colour, and a font is
# rendered once at its first widget's cell height, so shared == one colour + one size).
# KEEP BOTH paths: if the #25 transport audit finds the real cause is a NO_RSP tail-drop (not flash),
# the congestion fix keeps the full 33 KB face and this trim is unneeded. digit_h + preview_size are
# exposed to dial the exact byte size down toward the ~12 KB minclock floor against the reader's f11/f12.
def _grid_live_compact_geom(digit_h: int) -> Tuple[int, int, int]:
    """Shared geometry for the compact face so render + widgets never drift: (glyph_w, clock_y, kw_y).
    glyph_w mirrors build_dial_face's own ``int(h*0.6)`` digit-cell width."""
    return int(digit_h * 0.6), 140, 140 + digit_h + 34


def render_grid_live_compact(digit_h: int = 52) -> Image.Image:
    """Background for the COMPACT live-kW face: "GRID" header + clock colon + RED decimal point +
    "kW" unit, laid out for a ``digit_h``-tall uniform digit row. Value-/time-independent (built once)."""
    gw, clk_y, kw_y = _grid_live_compact_geom(digit_h)
    img = Image.new("RGB", (W, H), _GRID_BG)
    d = ImageDraw.Draw(img)
    d.text((_G_CX, 96), "GRID", font=_load_font(22, bold=True), fill=_GKL_HDR, anchor="mm")   # header
    d.text((_G_CX, clk_y + digit_h // 2), ":", font=_load_font(digit_h, bold=True),
           fill=_GRID_WHITE, anchor="mm")                                          # clock colon
    d.text((_G_CX - 6, kw_y + digit_h // 2), ".", font=_load_font(digit_h, bold=True),
           fill=_GRID_IMPORT, anchor="mm")                                         # red decimal (accent kept)
    d.text((_G_CX + 2 * gw, kw_y + digit_h // 2), "kW",
           font=_load_font(int(digit_h * 0.55), bold=True), fill=_GRID_LABEL, anchor="mm")     # unit
    return img


def _grid_live_compact_widgets(digit_h: int = 52) -> List[dict]:
    """Four live text widgets ALL sharing ONE white font ``Clk`` (uniform ``digit_h``): hour/min real
    clock + date=integer-kW + second=tenths-kW. The single shared glyph array (14 assets total) is
    the byte lever AND the minclock-proven install path."""
    gw, clk_y, kw_y = _grid_live_compact_geom(digit_h)

    def _txt(binding, cx, y, w, h, *, nw):
        return {"widget": "text", "type": binding, "x": cx - w // 2, "y": y, "w": w, "h": h,
                "color": "#FFFFFF", "align": "center", "min_numwidth": nw, "font": "Clk"}
    return [
        _txt("hour", _G_CX - gw - 8, clk_y, 2 * gw, digit_h, nw=2),
        _txt("min", _G_CX + gw + 8, clk_y, 2 * gw, digit_h, nw=2),
        _txt("date", _G_CX - gw, kw_y, gw + 8, digit_h, nw=1),          # integer kW
        _txt("second", _G_CX + int(gw * 0.6), kw_y, gw, digit_h, nw=1),  # tenths kW
    ]


def build_grid_live_compact_blob(*, digit_h: int = 52, name: str = "GRIDKW",
                                 preview_size: int = 32,
                                 max_match: int = GRID_STATIC_MAX_MATCH) -> bytes:
    """Build the COMPACT (single shared white font) live-kW container — the guaranteed-installable
    "safe lander" (~14-16 KB / 14 assets vs the full face's ~33 KB / 24). Same set_time_custom value
    path as the full face (:func:`encode_grid_live` is unchanged). Lower ``digit_h`` / ``preview_size``
    to shrink further toward the ~12 KB minclock floor if the free-flash read is tight."""
    from starmax_client import dialface
    blob = dialface.build_dial_face(render_grid_live_compact(digit_h),
                                    _grid_live_compact_widgets(digit_h),
                                    name=_safe_name(name), dial_type=1, preview_size=preview_size)
    return _cap_blob_assets(blob, max_match)


# --- CFW Tier-2 EXAMPLE: multi-metric face over generic value slots (NOT for stock) ---------------
# ⚠️ [CFW Tier-2 ONLY] This face binds the arbitrary-data value slots (starmax_client.slots, opcode
# 0xA2) and renders ONLY under a CFW dial renderer (cfw_dialrender.c — design §3/§4.2). Pushed to a
# STOCK / allowlist watch the slot widgets show blanks, so this builder must NEVER go on the
# stock-targeted path — it is a contract-first example of what slots unlock, not a shipping face.
# What it proves: the RTC hijack is gone — real clock/calendar ride 0x02 while THREE live metrics +
# a text label ride slots, none of which the 2-carrier day/month hijack could do.
_GSL_CLK_Y, _GSL_DIGIT_H = 96, 84       # clock row
_GSL_HERO_Y, _GSL_ROW_Y = 210, 300      # slot0 hero row / slot1+slot2 row
_GSL_TXT_Y = 372                        # text0 label row


def render_grid_slots() -> Image.Image:
    """Background for the CFW multi-slot grid face (value-/time-independent, built once): "GRID"
    header, clock colon, and the baked "W" / "kW" / "%" unit accents the live slot digits sit beside.
    The digits themselves are live slot widgets drawn by the CFW renderer (see :func:`_grid_slots_widgets`)."""
    img = Image.new("RGB", (W, H), _GRID_BG)
    d = ImageDraw.Draw(img)
    d.text((_G_CX, 60), "GRID", font=_load_font(24, bold=True), fill=_GKL_HDR, anchor="mm")
    d.text((_G_CX, _GSL_CLK_Y + _GSL_DIGIT_H // 2 - 4), ":", font=_load_font(72, bold=True),
           fill=_GRID_WHITE, anchor="mm")                                   # clock colon
    d.text((_G_CX + 118, _GSL_HERO_Y + 20), "W", font=_load_font(30, bold=True),
           fill=_GRID_LABEL, anchor="mm")                                   # slot0 unit
    d.text((_G_CX - 70, _GSL_ROW_Y + 18), "kW", font=_load_font(24, bold=True),
           fill=_GRID_LABEL, anchor="mm")                                   # slot1 unit
    d.text((_G_CX + 96, _GSL_ROW_Y + 18), "%", font=_load_font(24, bold=True),
           fill=_GRID_LABEL, anchor="mm")                                   # slot2 unit
    return img


def _grid_slots_widgets() -> List[dict]:
    """Widgets for the CFW multi-slot face: real HH:MM clock (RTC via 0x02 — calendar intact) +
    slot0 (total W, hero) + slot1 (kW, dec=1 pushed) + slot2 (battery SOC) + text0 (status label).

    Slot ``type:`` strings match starmax_client.slots / dialface.DATA_BINDINGS; the CFW renderer
    resolves ``slotN`` -> slots.num[N] (fixed-point via the pushed ``dec``) and ``text0`` -> slots.txt[0]."""
    def _txt(binding, cx, y, w, h, *, font, color, nw=1):
        return {"widget": "text", "type": binding, "x": cx - w // 2, "y": y, "w": w, "h": h,
                "color": color, "align": "center", "min_numwidth": nw, "font": font}
    white, red, dim = "#FFFFFF", "#FF3B30", "#7884A0"
    return [
        _txt("hour", _G_CX - 60, _GSL_CLK_Y, 96, _GSL_DIGIT_H, font="Clk", color=white, nw=2),
        _txt("min", _G_CX + 60, _GSL_CLK_Y, 96, _GSL_DIGIT_H, font="Clk", color=white, nw=2),
        _txt("slot0", _G_CX - 20, _GSL_HERO_Y, 200, 72, font="Big", color=red, nw=1),   # total W
        _txt("slot1", _G_CX - 130, _GSL_ROW_Y, 96, 52, font="Kw", color=red, nw=1),     # kW (dec=1)
        _txt("slot2", _G_CX + 40, _GSL_ROW_Y, 96, 52, font="Stat", color=white, nw=1),  # SOC %
        # a pointer/arc widget can bind a slotN too (e.g. an SOC ring) — text shown here for clarity.
        {"widget": "text", "type": "text0", "x": _G_CX - 150, "y": _GSL_TXT_Y, "w": 300, "h": 40,
         "color": dim, "align": "center", "font": "Lbl"},                                # status label
    ]


def build_grid_slots_blob(*, name: str = "GRIDSLOT", preview_size: int = 48,
                          max_match: int = GRID_STATIC_MAX_MATCH) -> bytes:
    """[CFW Tier-2 ONLY — NOT for stock] Build the multi-metric slot face container: real clock +
    slot0 (total W) + slot1 (kW dec=1) + slot2 (SOC) + text0 (label). Values are pushed live via
    ``starmax_client.slots.build_set_slots`` / ``build_set_text_slot`` (opcode 0xA2) — this blob is
    built ONCE and never re-pushed for a value change. Renders only under the CFW dial renderer."""
    from starmax_client import dialface
    blob = dialface.build_dial_face(render_grid_slots(), _grid_slots_widgets(),
                                    name=_safe_name(name), dial_type=1, preview_size=preview_size)
    return _cap_blob_assets(blob, max_match)


# =============================================================================
# DIAGNOSTIC binding-map face  (task #28 — map every display binding on HW)
# =============================================================================
# A ONE-shot debug face that exercises EVERY dialface DATA_BINDING at once, each with a tiny baked
# LABEL, so JP reads on-glass which widget renders which field (definitive binding map). Values arrive
# at runtime via set_time_custom (v1: hour/min/sec/date distinct-known; month/week/weekday real). Ships
# to the SPARE. Legibility > beauty. Fonts are shared by COLOUR GROUP (white clock + cyan/amber/green
# small) so 17 value-widgets need only 4 glyph folders (~40 glyphs), not 17. Hypothesis under test:
# hourhi/lo + minhi/lo are clock-tied digit-renders (track the clock) while date/day/month/week are
# independent channels — the colour grouping (cyan = should track clock; amber = should not) makes it
# readable at a glance.
_BM_WHITE, _BM_CYAN, _BM_AMBER, _BM_GREEN = "#FFFFFF", "#46C8EB", "#FFB43C", "#50D278"
_BM_LABEL = (120, 132, 150)
_BM_COLS = (112, 233, 354)
_BM_ROWS = (134, 190, 246, 302, 358)
_BM_CLK_Y = 76
# DECENT, CONSISTENT value size — EVERY field (clock + all bindings) renders at the SAME big-ish size,
# so JP sees on-glass which FIELD VIEWPORTS CLIP a full-size number vs render it whole (JP's finding:
# `date` is a small FIXED complication slot that truncates a prominent number — which is why kW can't
# live there). Tiny values would fit any slot and hide the clipping; consistent big values expose it.
_BM_VAL_H, _BM_VAL_W = 40, 112
# grid cells (label, binding, font-name) in row-major order — the font NAME groups colour + digit size.
# LABELS carry the real firmware semantics (the authoritative firmware table 2026-07-16) so JP reads the map
# right: `day`=day-of-month 1-31 (a plain number — the integer-kW HOME); `date`=formatted/compound field
# (unit_count=11, GARBLES a bare digit-font number → the "~70" phantom); `week`=weekday(array);
# `month`=month(array). The binding (2nd tuple field) is still the raw widget `type`.
_BM_CELLS = (
    ("date(fmt)", "date", "Am"), ("day 1-31", "day", "Am"), ("month(arr)", "month", "Am"),
    ("week=wkdy", "week", "Am"), ("hourhi", "hourhi", "Cy"), ("hourlo", "hourlo", "Cy"),
    ("minhi", "minhi", "Cy"), ("minlo", "minlo", "Cy"), ("step", "step", "Gr"),
    ("heart", "heart", "Gr"), ("distance", "distance", "Gr"), ("calorie", "calorie", "Gr"),
    ("battery", "battery", "Gr"), ("steplist", "steplist", "Gr"),
)
_BM_FONT_COLOR = {"Clk": _BM_WHITE, "Cy": _BM_CYAN, "Am": _BM_AMBER, "Gr": _BM_GREEN}


def _bm_cell_positions() -> List[Tuple[int, int]]:
    """(cx, ry) centre of each grid cell, row-major over _BM_COLS x _BM_ROWS (bottom row = 2 cells so
    the round bezel never clips a cell)."""
    return [(_BM_COLS[i % 3], _BM_ROWS[i // 3]) for i in range(len(_BM_CELLS))]


def render_binding_map() -> Image.Image:
    """Background for the diagnostic binding-map face: title + baked binding LABELS + clock colons.
    Values are LIVE widgets (see :func:`_binding_map_widgets`), driven at runtime by set_time_custom."""
    img = Image.new("RGB", (W, H), _GRID_BG)
    d = ImageDraw.Draw(img)
    d.text((_G_CX, 22), "GTX2 BINDING MAP", font=_load_font(15, bold=True), fill=_BM_LABEL, anchor="mm")
    d.text((_G_CX, 46), "clock hour:min:sec", font=_load_font(12, bold=True), fill=_BM_LABEL, anchor="mm")
    d.text((191, _BM_CLK_Y), ":", font=_load_font(40, bold=True), fill=_BM_WHITE, anchor="mm")   # clock colons
    d.text((275, _BM_CLK_Y), ":", font=_load_font(40, bold=True), fill=_BM_WHITE, anchor="mm")
    lf = _load_font(13, bold=True)
    for (label, _b, _f), (cx, ry) in zip(_BM_CELLS, _bm_cell_positions()):
        d.text((cx, ry - 27), label, font=lf, fill=_BM_LABEL, anchor="mm")            # tiny binding tag
    return img


def _binding_map_widgets() -> List[dict]:
    """17 live widgets, ALL at the same _BM_VAL_H (consistent-size clip test): HH:MM:SS clock (font Clk,
    white) + the 14 labelled binding cells (colour-grouped fonts Cy/Am/Gr). All explicit
    ``widget:"text"`` (week + steplist aren't text-default). Shared fonts → ~40 glyphs (4 folders)."""
    def _txt(binding, cx, y, w, h, font, color, nw):
        return {"widget": "text", "type": binding, "x": cx - w // 2, "y": y, "w": w, "h": h,
                "color": color, "align": "center", "min_numwidth": nw, "font": font}
    cy = _BM_CLK_Y - _BM_VAL_H // 2
    ws = [
        _txt("hour", 150, cy, 64, _BM_VAL_H, "Clk", _BM_WHITE, 2),
        _txt("min", 233, cy, 64, _BM_VAL_H, "Clk", _BM_WHITE, 2),
        _txt("second", 316, cy, 64, _BM_VAL_H, "Clk", _BM_WHITE, 2),
    ]
    for (_label, binding, font), (cx, ry) in zip(_BM_CELLS, _bm_cell_positions()):
        ws.append(_txt(binding, cx, ry - _BM_VAL_H // 2 + 6, _BM_VAL_W, _BM_VAL_H,
                       font, _BM_FONT_COLOR[font], 1))
    return ws


def build_binding_map_blob(*, name: str = "BINDMAP", preview_size: int = 32,
                           max_match: int = GRID_STATIC_MAX_MATCH) -> bytes:
    """Build the diagnostic binding-map container (task #28): every DATA_BINDING as a labelled live
    widget, so a HW push + distinct set_time_custom values reveal which widget = which field. cap-512 +
    shared colour-fonts. Ships to the SPARE for the on-glass binding map."""
    from starmax_client import dialface
    blob = dialface.build_dial_face(render_binding_map(), _binding_map_widgets(),
                                    name=_safe_name(name), dial_type=1, preview_size=preview_size)
    return _cap_blob_assets(blob, max_match)
