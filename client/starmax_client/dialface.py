"""Author a GTX2 dial *face* — custom background + LIVE widgets — as a native container.

This is the offline builder behind a **live** custom watch face (time/steps/date/HR that the
watch firmware updates), as opposed to :mod:`starmax_client.dialtranscode`, which only re-packs
an existing dial ZIP. It emits the **native container** the watch consumes
(:mod:`starmax_client.dialfmt`) **directly** — no intermediate ZIP — by synthesizing the
``dial.json`` render list (``item[]`` + optional ``fade_item[]`` AOD) and rendering/encoding the
assets each widget needs.

Why this works — the live-widget schema is ``dial.json``  [CAP]
--------------------------------------------------------------
There is **no** separate binary "overlay" section. On the GTX2 (Actions ATS3085S) the live
complications ARE the ``dial.json`` ``item[]`` array, embedded verbatim inside the container.
This was decoded byte-exact from our own captured **vendor** install (``custom_id_25022.bin`` /
``CWR05G_23687``, parsed by :mod:`starmax_client.dialfmt`), whose ``item[]`` declares live
``pointer`` hands, ``arc`` progress rings and ``text`` step/calorie fields. The vocabulary below
is that decode plus the freely-redistributable CDN dials documented in ``docs/watchface-format.md``.

Each widget = ``{widget: <renderer>, type: <data-binding>, x, y, w, h, ...}``. ``widget`` is HOW
it renders; ``type`` is WHICH live source the firmware binds. The firmware owns the clock / step
/ HR / date / battery sources per ``type``; the descriptor only says where, how, and which source.

Provenance labels used in comments:
  * [CAP]      byte-verified from our own capture / captured container.
  * [SCHEMA]   from the in-repo CDN dials + docs/watchface-format.md.
  * [INFERRED] reasoned convention, not independently confirmed on-device (flagged for the
               hardware test / for capture re-derivation before any Gadgetbridge upstream).

Clean-room: the container/codec/transport and ``dial.json``-as-observed are capture-derived and
PORTABLE. This module deliberately bakes in **no** APK/SDK-traced detail (the vendor's
``ZhjPlugin``/``X04DialProvider`` "photo" path is a different — SiFli — chip family and is not
used here). Any pointer-geometry convention taken from the captured analog dial is marked
[CAP]/[INFERRED] at its use site.

Requires **Pillow + lz4** (the optional ``transcode`` extra), imported lazily.
"""
from __future__ import annotations

import io
import json
from typing import Dict, List, Optional, Sequence, Tuple, Union

from starmax_client import dialfmt, dialtranscode

# --- decoded schema ----------------------------------------------------------------------
# `widget` = renderer kind. [SCHEMA] doc §1.4 + [CAP] `arc` (captured CWR05G_23687).
WIDGET_KINDS = frozenset({"icon", "text", "array", "curved", "pointer", "arc"})

# `type` = live data binding the firmware drives. [SCHEMA]/[CAP] (see module docstring).
DATA_BINDINGS = frozenset({
    # time / date
    "hour", "min", "second", "hourhi", "hourlo", "minhi", "minlo",
    "date", "day", "month", "week", "updategao",
    # health
    "step", "steplist", "heart", "calorie", "distance", "battery", "sport",
    # structural / static
    "background", "other", "calling",
    # [CFW Tier-2 ONLY] generic value slots — arbitrary-data channel (opcode 0xA2,
    # starmax_client.slots). These render ONLY under a CFW dial renderer (cfw_dialrender.c,
    # design §3/§4.1); a STOCK renderer never sees them and shows blanks. Keep them out of any
    # stock-targeted builder. slotN = signed int32 (fixed-point via the pushed `dec`); textN = short
    # ASCII string. Numbers/lengths mirror slots.CFW_NUM_SLOTS / CFW_TXT_SLOTS.
    *(f"slot{i}" for i in range(8)),      # slot0..slot7  (numeric, fixed-point aware)
    "text0", "text1",                      # short text labels
})

# When `widget` is omitted, a text-y binding renders as `text`; `background` as `icon`.
# Analog hands (`pointer`) / rings (`arc`) / state images (`array`) must be requested explicitly
# because a binding like `hour` is legitimately either a text field or a pointer hand.
_TEXT_DEFAULT_BINDINGS = frozenset({
    "hour", "min", "second", "hourhi", "hourlo", "minhi", "minlo",
    "date", "day", "month", "step", "heart", "calorie", "distance", "battery", "updategao",
    # [CFW Tier-2 ONLY] slots default to text widgets (auto 0-9 glyph font, min_numwidth, align) —
    # exactly like a `day`/`month` text field. A pointer/arc bound to a slot must still be explicit.
    *(f"slot{i}" for i in range(8)), "text0", "text1",
})

CANVAS = 466  # all sampled GTX2 dials are 466x466 (the AMOLED). [SCHEMA] doc §1.3
CENTER = CANVAS // 2

ImageLike = Union[str, bytes, "object"]  # path / raw bytes / PIL.Image.Image


class DialFaceError(ValueError):
    """Raised when a face spec (widgets / geometry / assets) is invalid."""


# =============================================================================
# Validation
# =============================================================================
def _is_hex_color(v) -> bool:
    return isinstance(v, str) and len(v) == 7 and v[0] == "#" and all(
        c in "0123456789abcdefABCDEF" for c in v[1:])


def _resolve_widget_kind(w: dict) -> str:
    kind = w.get("widget")
    if kind is None:
        t = w.get("type")
        if t == "background":
            return "icon"
        if t in _TEXT_DEFAULT_BINDINGS:
            return "text"
        raise DialFaceError(
            f"widget for type {t!r} is ambiguous — set 'widget' explicitly "
            f"(one of {sorted(WIDGET_KINDS)})")
    return kind


def validate_widgets(widgets: Sequence[dict], *, canvas: int = CANVAS) -> None:
    """Reject unknown widget kinds / data bindings / out-of-bounds geometry / bad fields.

    Raises :class:`DialFaceError` on the first problem. Pure — no I/O, no rendering.
    """
    if not isinstance(widgets, (list, tuple)) or not widgets:
        raise DialFaceError("widgets must be a non-empty list")
    for i, w in enumerate(widgets):
        where = f"widget[{i}]"
        if not isinstance(w, dict):
            raise DialFaceError(f"{where} must be a dict, got {type(w).__name__}")
        t = w.get("type")
        if t not in DATA_BINDINGS:
            raise DialFaceError(f"{where}: unknown type {t!r} (allowed: {sorted(DATA_BINDINGS)})")
        kind = _resolve_widget_kind(w)
        if kind not in WIDGET_KINDS:
            raise DialFaceError(f"{where}: unknown widget {kind!r} (allowed: {sorted(WIDGET_KINDS)})")

        # geometry — integral + in-bounds. Required for every kind EXCEPT `pointer`, whose
        # placement/size the builder auto-derives from the hand pivot (a full-canvas render box
        # centred on the dial); any geometry a pointer *does* supply is still bounds-checked.
        # Pointer/curved legitimately use a full-canvas w/h with an offset x/y, so x+w may exceed
        # the canvas — bound each field independently rather than the sum.
        geom_required = kind != "pointer"
        for f in ("x", "y", "w", "h"):
            if f not in w:
                if geom_required:
                    raise DialFaceError(f"{where}: missing geometry field {f!r}")
                continue
            if not isinstance(w[f], int) or isinstance(w[f], bool):
                raise DialFaceError(f"{where}: {f!r} must be an int, got {w[f]!r}")
        if "x" in w and "y" in w and not (0 <= w["x"] <= canvas and 0 <= w["y"] <= canvas):
            raise DialFaceError(f"{where}: x/y out of bounds 0..{canvas}: ({w['x']},{w['y']})")
        if "w" in w and "h" in w and not (1 <= w["w"] <= canvas and 1 <= w["h"] <= canvas):
            raise DialFaceError(f"{where}: w/h out of bounds 1..{canvas}: ({w['w']},{w['h']})")

        for cf in ("color", "fgcolor"):
            if cf in w and not _is_hex_color(w[cf]):
                raise DialFaceError(f"{where}: {cf!r} must be #RRGGBB, got {w[cf]!r}")

        if kind == "icon" and "picture" not in w:
            raise DialFaceError(f"{where}: icon widget requires 'picture'")
        if kind == "array" and "frames" not in w and "array_img" not in w:
            raise DialFaceError(f"{where}: array widget requires 'frames' (or a prebuilt 'array_img')")


# =============================================================================
# Asset helpers (lazy Pillow)
# =============================================================================
def _pil():
    Image, _ = dialtranscode._require_deps()  # raises TranscodeError w/ install hint if missing
    return Image


def _open_image(src: ImageLike):
    Image = _pil()
    if isinstance(src, str):
        return Image.open(src)
    if isinstance(src, (bytes, bytearray)):
        return Image.open(io.BytesIO(bytes(src)))
    if isinstance(src, Image.Image):
        return src
    raise DialFaceError(f"unsupported image source: {type(src).__name__}")


def _png_bytes(img) -> bytes:
    b = io.BytesIO()
    img.convert("RGBA").save(b, "PNG")
    return b.getvalue()


def _bmp_bytes(img) -> bytes:
    b = io.BytesIO()
    img.convert("RGB").save(b, "BMP")  # PIL writes 24-bit BI_RGB; encode_image downsamples to 565
    return b.getvalue()


def _load_font(px: int):
    """A deterministic-within-an-environment font for digit glyphs. Tries bundled DejaVu/
    Liberation, else Pillow's built-in default. Byte-parity of the *container assembly* does
    not depend on the exact glyph pixels (see tests)."""
    from PIL import ImageFont
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(p, px)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=px)  # Pillow >= 10.1
    except TypeError:  # pragma: no cover - very old Pillow
        return ImageFont.load_default()


def _rgba(hex_color: str) -> Tuple[int, int, int, int]:
    s = hex_color.lstrip("#")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), 255


def _render_digit_glyphs(font_name: str, w: int, h: int, color: str) -> List[Tuple[str, bytes]]:
    """Render digits 0-9 for a text font into ``<font>_<i>_8888.png`` native assets.

    The container flattens folders into the filename prefix (``Number_0_8888.png``) — matches the
    captured install. [CAP]
    """
    from PIL import Image, ImageDraw
    fnt = _load_font(max(8, int(h * 0.82)))
    out: List[Tuple[str, bytes]] = []
    for d in range(10):
        g = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(g)
        ch = str(d)
        bb = draw.textbbox((0, 0), ch, font=fnt)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        draw.text(((w - tw) // 2 - bb[0], (h - th) // 2 - bb[1]), ch, fill=_rgba(color), font=fnt)
        out.append((f"{font_name}_{d}_8888.png", dialtranscode.encode_image(
            f"{font_name}_{d}_8888.png", _png_bytes(g))))
    return out


# --- default analog-hand sprite ----------------------------------------------------------
# Pivot / placement convention derived from the captured analog dial CWR05G_23687 [CAP]:
# a `pointer` item places its picture so the pivot lands at (x+centerx, y+centery) in canvas
# space; for a centered clock that is (233,233). The sprite geometry itself is [INFERRED]
# (the vendor ships bespoke hand art). The hardware test confirms firmware honours it.
_HAND_SPEC = {  # binding -> (width, length, default_color)
    "hour":   (16, 150, "#FFFFFF"),
    "min":    (10, 205, "#FFFFFF"),
    "second": (6, 215, "#FF3B30"),
}


def _make_hand_sprite(binding: str, color: Optional[str]):
    from PIL import Image, ImageDraw
    w, length, dflt = _HAND_SPEC.get(binding, _HAND_SPEC["min"])
    col = _rgba(color or dflt)
    tail = 20  # stub below the pivot
    img = Image.new("RGBA", (w, length + tail), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, w - 1, length + tail - 1], radius=w // 2, fill=col)
    pivot = (w // 2, length)  # centerx, centery within the sprite
    return img, pivot


# =============================================================================
# dial.json item synthesis
# =============================================================================
def _text_item(w: dict, font_name: str) -> dict:
    it = {"widget": "text", "type": w["type"],
          "x": w["x"], "y": w["y"], "w": w["w"], "h": w["h"],
          "fontnum": int(w.get("fontnum", 10)), "font": font_name,
          "enable_font": int(w.get("enable_font", 0)),
          "min_numwidth": int(w.get("min_numwidth", 1)),
          "align": w.get("align", "center")}
    if "color" in w:
        it["color"] = w["color"]
    return it


def _passthrough(keys: Sequence[str], w: dict, extra: dict) -> dict:
    it = dict(extra)
    for k in keys:
        if k in w:
            it[k] = w[k]
    return it


# =============================================================================
# Builder
# =============================================================================
def build_dial_face(background: ImageLike,
                    widgets: Sequence[dict],
                    *,
                    name: str = "CustomFace",
                    aod: Optional[Sequence[dict]] = None,
                    dial_type: int = 1,
                    dial_version: int = 4,
                    canvas: int = CANVAS,
                    preview_size: int = 256,
                    describe_name: Optional[str] = None) -> bytes:
    """Build a native dial container from a custom ``background`` + live ``widgets``.

    Returns the container bytes to stream with :func:`starmax_client.commands.dials.push_dial`
    (or ``push_dial_face``). Offline; no ZIP round-trip. The container is validated on the way
    out — the returned bytes always parse under :func:`dialfmt.parse_blob` with the header
    CRC-32 the watch checks.

    * ``background`` — path / bytes / PIL image; resized to ``canvas`` and stored as the
      ``type:"background"`` ``_0565.bmp`` asset.
    * ``widgets`` — list of widget descriptors (see module docstring / :data:`WIDGET_KINDS`,
      :data:`DATA_BINDINGS`). ``text`` widgets auto-render a 0-9 digit font; ``pointer`` widgets
      auto-generate a hand sprite when no ``picture`` is given; ``arc`` needs no asset;
      ``icon`` requires ``picture``.
    * ``aod`` — optional widgets for the always-on / dimmed ``fade_item[]`` list (same schema).
    * ``preview_size`` — edge length of the on-watch dial-picker thumbnail (``preview_0565.bmp``).
      Vendor dials ship 256; a smaller value trades picker sharpness for a markedly smaller
      container (the 256px preview is ~9 KB of an RGB565+LZ4 blob — 128px is ~4 KB), which matters
      on the memory-constrained ESPHome node push path. [SCHEMA] (256 is the observed vendor size).
    """
    if not isinstance(preview_size, int) or not (16 <= preview_size <= canvas):
        raise DialFaceError(f"preview_size must be an int in 16..{canvas}, got {preview_size!r}")
    if not isinstance(name, str) or not name or len(name) >= dialfmt.NAME_LEN:
        raise DialFaceError(f"name must be 1..{dialfmt.NAME_LEN - 1} ASCII chars, got {name!r}")
    try:
        name.encode("ascii")
    except UnicodeEncodeError as e:
        raise DialFaceError(f"name must be ASCII: {name!r}") from e

    validate_widgets(widgets, canvas=canvas)
    if aod is not None:
        validate_widgets(aod, canvas=canvas)

    assets: List[Tuple[str, bytes]] = []        # (asset_name, native_bytes) in container order
    seen: Dict[str, bytes] = {}                 # dedup by asset name
    font_folders: Dict[str, str] = {}           # font_name -> "png"/"bmp" for file.json

    def _emit(asset_name: str, native: bytes) -> None:
        if asset_name not in seen:
            seen[asset_name] = native
            assets.append((asset_name, native))

    # --- background asset ---------------------------------------------------------------
    bg_img = _open_image(background).convert("RGB").resize((canvas, canvas))
    _emit("BG_0565.bmp", dialtranscode.encode_image("BG_0565.bmp", _bmp_bytes(bg_img)))

    def _build_items(specs: Sequence[dict]) -> List[dict]:
        items: List[dict] = [
            {"widget": "icon", "type": "background", "x": 0, "y": 0,
             "w": canvas, "h": canvas, "picture": "BG_0565.bmp"}
        ]
        icon_seq = 0
        for w in specs:
            kind = _resolve_widget_kind(w)
            t = w["type"]
            if kind == "text":
                font_name = w.get("font") or t.capitalize()
                color = w.get("color", "#FFFFFF")
                if font_name not in font_folders:
                    glyph_w = max(8, int(w["h"] * 0.6))  # proportional digit cell
                    for an, nb in _render_digit_glyphs(font_name, glyph_w, w["h"], color):
                        _emit(an, nb)
                    font_folders[font_name] = "png"
                items.append(_text_item(w, font_name))
            elif kind == "pointer":
                pic = f"{t}_hand_8888.png"
                if "picture" in w:
                    src = _open_image(w["picture"])
                    _emit(pic, dialtranscode.encode_image(pic, _png_bytes(src)))
                    iw, ih = src.size
                    cx, cy = int(w.get("centerx", iw // 2)), int(w.get("centery", ih // 2))
                else:
                    sprite, (cx, cy) = _make_hand_sprite(t, w.get("color"))
                    _emit(pic, dialtranscode.encode_image(pic, _png_bytes(sprite)))
                # pivot -> canvas centre unless the caller pins x/y explicitly. [CAP]-derived pivot
                # convention: pivot_canvas == (x+centerx, y+centery); centre it at (233,233).
                x = int(w.get("x", CENTER - cx))
                y = int(w.get("y", CENTER - cy))
                items.append(
                    {"widget": "pointer", "type": t, "x": x, "y": y,
                     "w": int(w.get("w", canvas)), "h": int(w.get("h", canvas)),
                     "picture": pic, "centerx": cx, "centery": cy,
                     "rotation": int(w.get("rotation", 0)),
                     "start_angle": int(w.get("start_angle", 0)),
                     "end_angle": int(w.get("end_angle", 360)),
                     "start_value": int(w.get("start_value", 0)),
                     "end_value": int(w.get("end_value", 360))})
            elif kind == "arc":
                items.append(_passthrough(
                    ("serial_number", "radius"), w,
                    {"widget": "arc", "type": t, "x": w["x"], "y": w["y"], "w": w["w"], "h": w["h"],
                     "start_angle": w.get("start_angle", 225), "end_angle": w.get("end_angle", 135),
                     "fgcolor": w.get("fgcolor", w.get("color", "#FFA34C")),
                     "color": w.get("color", "#FFA34C"), "arc_width": int(w.get("arc_width", 8))}))
            elif kind == "icon":
                pic = w.get("picture")
                if isinstance(pic, str) and (pic.lower().endswith("_8888.png")
                                             or pic.lower().endswith("_0565.bmp")):
                    asset_name = pic.split("/")[-1]
                    src = _open_image(pic)
                else:
                    asset_name = f"icon_{icon_seq}_8888.png"
                    icon_seq += 1
                    src = _open_image(pic)
                nb = (dialtranscode.encode_image(asset_name, _png_bytes(src))
                      if asset_name.lower().endswith(".png")
                      else dialtranscode.encode_image(asset_name, _bmp_bytes(src)))
                _emit(asset_name, nb)
                items.append({"widget": "icon", "type": t, "x": w["x"], "y": w["y"],
                              "w": w["w"], "h": w["h"], "picture": asset_name})
            elif kind == "array":
                base = w.get("array_img", t.capitalize())
                frames = w.get("frames") or []
                for j, fr in enumerate(frames):
                    an = f"{base}_{j}_8888.png"
                    _emit(an, dialtranscode.encode_image(an, _png_bytes(_open_image(fr))))
                if frames:
                    font_folders[base] = "png"
                items.append(_passthrough(
                    ("count", "color"), w,
                    {"widget": "array", "type": t, "x": w["x"], "y": w["y"], "w": w["w"],
                     "h": w["h"], "array_img": base, "count": int(w.get("count", len(frames)))}))
            elif kind == "curved":
                items.append(_passthrough(
                    ("centerx", "centery", "angle", "angle_space", "total_count", "align",
                     "fontnum", "font", "min_numwidth"), w,
                    {"widget": "curved", "type": t, "x": w["x"], "y": w["y"],
                     "w": w["w"], "h": w["h"]}))
        return items

    item_list = _build_items(widgets)
    # Every known-good container ships a NON-EMPTY fade_item[] (the captured vendor install does); an
    # empty one is vendor-divergent, so default the AOD/dimmed list to the background mirror (item[0])
    # rather than []. [preserved from dial-face-builder 619adec, bg-only form.]
    fade_list = _build_items(aod) if aod is not None else [item_list[0]]

    manifest = {
        "frame_version": 1, "dial_version": int(dial_version), "name": name,
    }
    if describe_name:  # vendor dials carry it right after `name` (dial-picker label, e.g. "Vibrant Metal")
        manifest["describe_name"] = describe_name
    manifest.update({
        "preview": "preview_0565.bmp", "dial_type": int(dial_type),
        "resolution_ratio": f"{canvas}x{canvas}", "platform": "ats3085s", "size": 256,
        "enable_pic_compress": 0, "app_preview": "app_preview.png",
        "item": item_list, "fade_item": fade_list,
    })
    file_json = {"item": [{"name": n, "format": fmt} for n, fmt in font_folders.items()],
                 "fade_item": []}

    # small on-watch preview thumbnail (24-bit BMP), like every vendor dial. [SCHEMA]
    prev = bg_img.resize((preview_size, preview_size))
    _emit("preview_0565.bmp", dialtranscode.encode_image("preview_0565.bmp", _bmp_bytes(prev)))

    # dial.json + file.json go FIRST and verbatim (UTF-8), matching the captured container. [CAP]
    dial_json_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    file_json_bytes = json.dumps(file_json, separators=(",", ":")).encode("utf-8")
    ordered = [("dial.json", dial_json_bytes), ("file.json", file_json_bytes)] + assets

    # const_a/const_b left at their defaults — the ONLY observed values (b"\x06\x04" / b"\x00\x04",
    # byte-identical to the proven CWR05G_23687 install). The dial.json dial_version is authored
    # separately; whether the firmware requires the opaque const_b to track it is unverified, so we
    # do NOT synthesize an unobserved const_b. [CAP] defaults / [INFERRED] independence.
    blob = dialfmt.build_blob(name, ordered)
    # self-check: the bytes we hand back must parse + pass the header CRC-32 the watch verifies.
    dialfmt.parse_blob(blob, verify_crc=True)
    return blob


# =============================================================================
# Config-file convenience (for the CLI / demo staging)
# =============================================================================
def build_from_config(config: dict, base_dir: str = ".") -> bytes:
    """Build a face from a JSON-ish config dict.

    ``{"name","background","dial_type"?,"widgets":[...],"aod"?:[...]}``. ``background`` and any
    widget/frame ``picture`` given as a relative path are resolved against ``base_dir``.
    """
    import os
    bg = config.get("background")
    if bg is None:
        raise DialFaceError("config has no 'background'")

    def _resolve(p):
        return p if (not isinstance(p, str) or os.path.isabs(p)) else os.path.join(base_dir, p)

    def _fix(specs):
        out = []
        for w in specs or []:
            w = dict(w)
            if isinstance(w.get("picture"), str):
                w["picture"] = _resolve(w["picture"])
            if isinstance(w.get("frames"), list):
                w["frames"] = [_resolve(f) for f in w["frames"]]
            out.append(w)
        return out

    return build_dial_face(
        _resolve(bg), _fix(config.get("widgets")),
        name=str(config.get("name", "CustomFace"))[:dialfmt.NAME_LEN - 1],
        aod=_fix(config["aod"]) if config.get("aod") else None,
        dial_type=int(config.get("dial_type", 1)),
        dial_version=int(config.get("dial_version", 2)))
