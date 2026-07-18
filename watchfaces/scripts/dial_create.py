#!/usr/bin/env python3
"""Generate a VALID GTX2 (Actions ATS3085x / Runmefit) watch-face .bin from scratch.

Inputs: a background image (PNG/JPG) + a simple layout config (JSON). Produces a
dial .bin whose structure matches a stock dial (see docs/watchface-format.md):
  * dial.json manifest (466x466, platform "ats3085s", documented element vocabulary),
  * background converted to a 24-bit BMP (the *_0565.bmp target),
  * digit-glyph fonts rendered per text element (0-9 PNGs, RGBA8888),
  * app_preview.png (466x466 RGBA) + on-watch preview_0565.bmp (256x256 24-bit BMP),
  * packed with scripts/dial_pack.py (DEFLATE-9, slot dir).

BLE upload is hardware-gated; this is the CREATION half only. On-device rendering
of generated glyphs is not verified (no upload path yet).

Requires Pillow (image work): pip install pillow  (or use the repo venv).

    scripts/dial_create.py <config.json> <out.bin> [--workdir DIR] [--slot N]

Config schema (all coords in 466x466 screen space):
    {
      "name": "MyDial",              # <=30 chars
      "background": "bg.png",        # resized to 466x466
      "dial_type": 1,                # optional (default 1)
      "elements": [
        {"type":"time","x":116,"y":175,"color":"#FFFFFF","digit_w":76,"digit_h":116,"gap":24,"colon":true},
        {"type":"date","x":210,"y":66,"color":"#B0B0B0","digit_w":22,"digit_h":30},
        {"type":"steps","x":150,"y":360,"color":"#00E5FF","digit_w":22,"digit_h":30,"max_digits":5},
        {"type":"heart","x":250,"y":360,"color":"#FF4040","digit_w":22,"digit_h":30,"max_digits":3},
        {"type":"icon","x":40,"y":40,"image":"logo.png","w":48,"h":48}
      ]
    }
Element types -> GTX2 item types: time->hour+min(+colon icon), hour, min, date,
month, steps->step, heart, icon(static image).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("this tool needs Pillow: pip install pillow (or run with the repo venv python)")

W = H = 466
PREVIEW = 256
# Font resolution: $GTX2_FONT_DIR override (any .ttf inside it) -> common system paths.
# Install DejaVu/Liberation (`fonts-dejavu`, `fonts-liberation`) or point $GTX2_FONT_DIR at a dir.
_FONT_DIR = os.environ.get("GTX2_FONT_DIR")
TTF_CANDIDATES = [
    *([os.path.join(_FONT_DIR, f) for f in (
        "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "DejaVuSans.ttf")]
      if _FONT_DIR else []),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
# config type -> (GTX2 item "type", default font-dir name, default min_numwidth, default max_digits)
TEXT_TYPES = {
    "hour":  ("hour",  "Hour",  2, 2),
    "min":   ("min",   "Min",   2, 2),
    "date":  ("date",  "Date",  2, 2),
    "month": ("month", "Month", 2, 2),
    "steps": ("step",  "Step",  1, 5),
    "heart": ("heart", "Heart", 1, 3),
}


def load_font(px):
    for p in TTF_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, px)
            except OSError:
                pass
    try:
        return ImageFont.load_default(size=px)   # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def hexrgba(s):
    s = s.lstrip("#")
    if len(s) != 6:
        raise ValueError(f"color must be #RRGGBB, got {s!r}")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), 255)


def draw_centered(img, ch, color, font):
    d = ImageDraw.Draw(img)
    bb = d.textbbox((0, 0), ch, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    d.text(((img.width - tw) // 2 - bb[0], (img.height - th) // 2 - bb[1]), ch, fill=color, font=font)


def render_font_dir(fwdir, name, chars, w, h, color):
    """Render one glyph per char into <fwdir>/<name>/<name>_<i>_8888.png (RGBA8888)."""
    d = os.path.join(fwdir, name)
    os.makedirs(d, exist_ok=True)
    font = load_font(max(8, int(h * 0.82)))
    for i, ch in enumerate(chars):
        g = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw_centered(g, ch, color, font)
        g.save(os.path.join(d, f"{name}_{i}_8888.png"))


def text_item(gtx_type, x, y, w, h, font, min_numwidth, color):
    return {"widget": "text", "type": gtx_type, "x": int(x), "y": int(y), "w": int(w), "h": int(h),
            "fontnum": 10, "font": font, "enable_font": 0, "min_numwidth": min_numwidth,
            "align": "center", "color": color}


def build(cfg, cfgdir, fwdir):
    """Populate fwdir with assets; return (items, fade_items, font_dirs, preview_ops)."""
    items, fade_items, fonts, preview_ops = [], [], [], []

    # background -> BG_0565.bmp (24-bit)
    bg_path = cfg["background"] if os.path.isabs(cfg["background"]) else os.path.join(cfgdir, cfg["background"])
    bg = Image.open(bg_path).convert("RGB").resize((W, H))
    bg.save(os.path.join(fwdir, "BG_0565.bmp"))          # PIL writes 24-bit BI_RGB BMP
    bg_item = {"widget": "icon", "type": "background", "x": 0, "y": 0, "w": W, "h": H, "picture": "BG_0565.bmp"}
    items.append(bg_item)
    fade_items.append(dict(bg_item))

    def add_text(cfg_type, e, gtx_type=None, font=None):
        spec = TEXT_TYPES[cfg_type]
        gtx_type = gtx_type or spec[0]
        font = font or spec[1]
        min_nw = e.get("min_numwidth", spec[2])
        digits = e.get("max_digits", spec[3])
        dw, dh = e.get("digit_w", 22), e.get("digit_h", 30)
        color = e.get("color", "#FFFFFF")
        if font not in fonts:
            render_font_dir(fwdir, font, "0123456789", dw, dh, hexrgba(color))
            fonts.append(font)
        w = dw * digits
        items.append(text_item(gtx_type, e["x"], e["y"], w, dh, font, min_nw, color))
        preview_ops.append((e["x"], e["y"], {"hour": "12", "min": "34", "date": "15",
                            "month": "07", "step": "8888", "heart": "72"}.get(gtx_type, "0"),
                            color, dh))
        return dw, dh

    for e in cfg["elements"]:
        t = e["type"]
        if t == "time":
            dw, dh = e.get("digit_w", 76), e.get("digit_h", 116)
            gap, color = e.get("gap", 24), e.get("color", "#FFFFFF")
            font = "Time"
            if font not in fonts:
                render_font_dir(fwdir, font, "0123456789", dw, dh, hexrgba(color))
                fonts.append(font)
            hx, hy = e["x"], e["y"]
            items.append(text_item("hour", hx, hy, dw * 2, dh, font, 2, color))
            colon_w = 0
            if e.get("colon"):
                colon_w = max(dw // 3, 12)
                cimg = Image.new("RGBA", (colon_w, dh), (0, 0, 0, 0))
                draw_centered(cimg, ":", hexrgba(color), load_font(int(dh * 0.82)))
                cimg.save(os.path.join(fwdir, "Colon_8888.png"))
                cx = hx + dw * 2 + (gap - colon_w) // 2
                items.append({"widget": "icon", "type": "other", "x": int(cx), "y": int(hy),
                              "w": colon_w, "h": dh, "picture": "Colon_8888.png"})
            mx = hx + dw * 2 + gap
            items.append(text_item("min", mx, hy, dw * 2, dh, font, 2, color))
            preview_ops.append((hx, hy, "12", color, dh))
            if e.get("colon"):
                preview_ops.append((hx + dw * 2 + (gap - colon_w) // 2, hy, ":", color, dh))
            preview_ops.append((mx, hy, "34", color, dh))
        elif t in TEXT_TYPES:
            add_text(t, e)
        elif t == "icon":
            img_path = e["image"] if os.path.isabs(e["image"]) else os.path.join(cfgdir, e["image"])
            w, h = e.get("w", 48), e.get("h", 48)
            ic = Image.open(img_path).convert("RGBA").resize((w, h))
            fn = f"icon_{len([i for i in items if i.get('picture','').startswith('icon_')])}_8888.png"
            ic.save(os.path.join(fwdir, fn))
            items.append({"widget": "icon", "type": "other", "x": int(e["x"]), "y": int(e["y"]),
                          "w": w, "h": h, "picture": fn})
        else:
            sys.exit(f"unknown element type: {t!r}")
    return items, fade_items, fonts, preview_ops


def make_previews(fwdir, bg_path, preview_ops):
    base = Image.open(bg_path).convert("RGBA").resize((W, H))
    d = ImageDraw.Draw(base)
    for x, y, sample, color, dh in preview_ops:
        d.text((x, y), sample, fill=hexrgba(color), font=load_font(int(dh * 0.82)))
    base.save(os.path.join(fwdir, "app_preview.png"))                       # 466x466 RGBA
    base.convert("RGB").resize((PREVIEW, PREVIEW)).save(os.path.join(fwdir, "preview_0565.bmp"))  # 256 24-bit


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate a valid GTX2 dial .bin from a background image + layout config.",
        epilog="Creation half only — BLE upload is hardware-gated; on-device glyph rendering unverified.",
    )
    ap.add_argument("config", help="layout config JSON")
    ap.add_argument("out_bin", help="output dial .bin")
    ap.add_argument("--workdir", help="staging dir to keep (default: temp, removed)")
    ap.add_argument("--slot", default="1", help="numeric slot dir name (default: 1)")
    args = ap.parse_args()

    cfg = json.load(open(args.config))
    cfgdir = os.path.dirname(os.path.abspath(args.config))
    name = str(cfg.get("name", "CustomDial"))[:30]

    tmp = args.workdir or tempfile.mkdtemp(prefix="dialgen_")
    fwdir = os.path.join(tmp, args.slot, "firmware")
    if os.path.isdir(fwdir):
        shutil.rmtree(fwdir)
    os.makedirs(fwdir, exist_ok=True)

    items, fade_items, fonts, preview_ops = build(cfg, cfgdir, fwdir)
    make_previews(fwdir, os.path.join(fwdir, "BG_0565.bmp"), preview_ops)

    manifest = {
        "frame_version": 1, "dial_version": 2, "name": name, "preview": "preview_0565.bmp",
        "dial_type": int(cfg.get("dial_type", 1)), "resolution_ratio": f"{W}x{H}",
        "platform": "ats3085s", "size": 256, "enable_pic_compress": 0,
        "app_preview": "app_preview.png", "item": items, "fade_item": fade_items,
    }
    json.dump(manifest, open(os.path.join(fwdir, "dial.json"), "w"), indent=2)
    json.dump({"item": [{"name": f, "format": "png"} for f in fonts], "fade_item": []},
              open(os.path.join(fwdir, "file.json"), "w"), indent=2)

    # pack with dial_pack.py (DEFLATE-9, slot dir preserved)
    packer = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dial_pack.py")
    r = subprocess.run([sys.executable, packer, tmp, args.out_bin])
    if r.returncode != 0:
        sys.exit(f"dial_pack.py failed ({r.returncode})")

    print(f"generated dial '{name}' -> {args.out_bin}")
    print(f"  slot dir : {args.slot}/  ·  elements: {len(items)} items, {len(fonts)} rendered fonts {fonts}")
    print(f"  assets   : BG_0565.bmp (24-bit) + app_preview.png (466) + preview_0565.bmp (256) + glyph fonts")
    if not args.workdir:
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(f"  workdir  : {tmp} (kept)")


if __name__ == "__main__":
    main()
