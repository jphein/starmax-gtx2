# Custom GTX2 watch-face — build recipe (file-build PoC)

A **repeatable recipe** for producing a valid custom GTX2 dial `.bin`, verified by an actual
proof-of-concept built from a stock dial (2026-07-11). This is the **file-build half only** —
uploading the result over BLE is still blocked on decoding the install/switch opcodes (only the
read-only `0x16` catalog is captured; see `watchface-format.md` §3). The PoC here is structurally
valid and diffs cleanly against a stock dial; **on-watch acceptance is unverified** until the
upload path exists.

Format reference: `watchface-format.md`. In short, a GTX2 dial `.bin` is a **ZIP** of a
`dial.json` manifest + loose assets (**PNG = RGBA8888**, **BMP = RGB565 target**) under a
`firmware/` tree (optionally inside a numeric slot dir).

---

## PoC summary (what was built and proven)

Source: `watchfaces/cw07630401.bin` (stock digital dial). Output: **`watchfaces/custom/cw07_poc.bin`**.

Two visible, structure-preserving edits:
- **Asset path:** recolored the background `firmware/Bg_0565.bmp` (35% purple wash; mean pixel
  delta **21.1/255**), verified with before/after renders.
- **Manifest path:** edited `firmware/dial.json` **values only** — `name`, one element `color`
  (`#FFFFFF → #00E5FF` on the step data-ring), one element `x` (calling icon `58 → 98`).

Verified (commands in the Validation section):
- Output is a valid ZIP (`PK\x03\x04`), **270,633 B** vs stock 275,374 B (same ballpark).
- **Identical 132-entry file set** vs stock.
- **Only 2 files differ** from stock (the two we touched) — confirmed by full md5 tree diff.
- `dial.json`: top-level keys identical, 21/21 items, **every item key-set identical**, exactly
  **3 value diffs** (our edits), parses straight from the packed `.bin`.
- Asset formats intact: background = 24-bit BMP 466×466; on-watch preview = 256×256 BMP present;
  app preview = 466×466 RGBA PNG present; glyphs = RGBA PNG.

A runnable build script (`build_poc.py`) reproduces this PoC end to end.

---

## Prerequisites

- `unzip` / `zip` (system).
- Python + **Pillow** for the BMP recolor / preview regen. Reuse the tools venv:
  `tools/unpack_clock_res/.venv/bin/python` (has `lz4` + `pillow`).

---

## Recipe

### 1. Unpack a stock dial (template)
```bash
cd <repo-root>                                        # your starmax-gtx2 checkout
unzip -o watchfaces/cw07630401.bin -d build/          # yields build/2/{app,firmware}/...
```
Pick a template close to your target style (digital: `cw07630401`/`act06120103`; analog with
`pointer` hands: `cwr01g21505`). Note whether it has a numeric **slot dir** (`2/`, `4/`) — you
must preserve it (or its absence).

### 2. Edit assets (`build/<slot>/firmware/`)
- **Background** `*_0565.bmp`: must stay **466×466, 24-bit, uncompressed (BI_RGB), bottom-up BGR,
  4-byte row padding**. Pillow does this automatically when you save an `RGB`-mode image to `.bmp`:
  ```python
  from PIL import Image
  bg = Image.open("build/2/firmware/Bg_0565.bmp").convert("RGB")   # 466x466
  # ...recolor / paste / replace pixels...
  bg.save("build/2/firmware/Bg_0565.bmp")                          # stays 24-bit BMP
  ```
  To swap in a *new* background from a PNG/JPG: open it, `.convert("RGB").resize((466,466))`, save as `.bmp`.
- **Glyphs / icons** `*_8888.png`: keep as **RGBA PNG**. Preserve the numbered naming
  (`Font_0..9`, `Week_0..6`, …) and dimensions the manifest expects.

### 3. Edit the manifest (`firmware/dial.json`) — values only
- Change **values** (`x`,`y`,`w`,`h`,`color` `#RRGGBB`, `align`, `font`, `name`, …). **Do not add
  or remove keys**, and **do not rename** an asset a widget references (`picture`,`font`,`array_img`)
  unless you also rename the file/folder.
- `resolution_ratio` and `platform` must match the device (`466x466`, `ats3085s`).
- If you edit programmatically (`json.load`→edit→`json.dump`) the whitespace normalizes — fine,
  the watch parses JSON regardless. Assert the key set is unchanged (the PoC script does this).

### 4. Regenerate previews (do not skip)
The previews are **independent images** — they will NOT reflect your asset/manifest edits unless
you regenerate them:
- `firmware/app_preview.png` — **466×466 RGBA PNG** (phone-app preview; this is the file the CDN
  serves as `<name>.png`).
- `firmware/preview_0565.bmp` — **256×256 24-bit BMP** (on-watch dial-picker thumbnail).
Render your final face (or at least composite the new background) into both.

### 5. Repack — **DEFLATE, preserve the tree**
```bash
mkdir -p watchfaces/custom
( cd build && zip -r -X -9 -q ../watchfaces/custom/mydial.bin 2/ )   # zip the SLOT dir
```
Zip **from the parent of the slot dir** so internal paths stay `2/firmware/...`. Keep directory
entries (`zip -r` includes them). Flat dials (no slot dir) → `zip -r ... firmware/`.

---

## Validation checklist (all run for the PoC)
```bash
S=watchfaces/cw07630401.bin ; P=watchfaces/custom/cw07_poc.bin
# a) valid zip + same file set
xxd -l4 "$P"                                   # 50 4b 03 04  (PK)
diff <(zipinfo -1 "$S"|sort) <(zipinfo -1 "$P"|sort)     # empty = identical entry set
# b) minimal diff: only intended files changed
diff <(unzip -p "$S" ...) ...   # or md5 the two unpacked trees; expect only your edits
# c) manifest well-formed: keys preserved, only intended value diffs, parses from the .bin
python -c "import json,zipfile; json.loads(zipfile.ZipFile('$P').read('2/firmware/dial.json'))"
# d) asset formats
unzip -p "$P" 2/firmware/Bg_0565.bmp | file -    # PC bitmap ... 466 x 466 x 24
unzip -l "$P" | grep -E "app_preview.png|preview_0565.bmp"   # previews present
```

---

## Gotchas (learned building the PoC)

1. **Repack with DEFLATE, not store.** `zip -0` (store) bloated the bundle **~4×** (1.1 MB vs
   270 KB) because the 652 KB 24-bit BMPs don't compress. Stock uses deflate. Beware: `file`
   reports `compression method=store` for a stock dial — that only describes the **leading empty
   directory entry**; the real file entries are deflated (`unzip -v` shows it).
2. **Preserve the slot dir and exact internal paths.** Zip from the slot dir's parent. A dial with
   `2/firmware/...` must stay `2/firmware/...`; don't flatten or double-nest.
3. **`_0565.bmp` is authored as a 24-bit BMP**, despite the name. The `0565` is the *on-device
   target* (RGB565); the app/firmware down-samples at pack/transfer time. Do **not** hand it a real
   16-bit BMP — match the stock (24-bit BI_RGB).
4. **Pillow stamps 96 DPI** (`3780 px/m`) into the BMP DIB header; stock leaves it 0. Harmless —
   pixel data, dimensions, bit depth, and `bits offset 54` are unchanged; the watch reads pixels,
   not DPI.
5. **Regenerate both previews** (§4) — they're separate assets. Skipping this leaves the *old*
   artwork showing in the app and the watch picker even though the face changed.
6. **`enable_pic_compress` is asset-level, not ZIP-level.** Our samples are `0` (assets not
   pre-compressed); the ZIP still deflates independently.
7. **Manifest keys are load-bearing.** Change values, not the key set. Renaming a referenced asset
   without renaming the file will break rendering.

---

## Validated vs NOT validated

- **Validated:** the `.bin` is a well-formed GTX2 dial — correct container, identical structure to
  a stock dial, correct asset encodings, parseable manifest, minimal intended diff.
- **NOT validated (needs the BLE upload path / a real device install):**
  - on-watch **rendering** of the edited face;
  - semantics of `size`, `dial_type`, `serial_number`, and how a **custom dial-Id** is assigned
    (SDK says custom range 5001–25000);
  - whether the watch/app **validates a signature or registration** before accepting a dial;
  - whether the app ships the raw `firmware/` ZIP or a **transcoded packed blob** (`watchface-format.md`
    §3.4 — the open question a dial-install capture resolves).

---

## Artifacts
- **PoC dial:** `watchfaces/custom/cw07_poc.bin`.
- **Build script (repeatable):** `build_poc.py` (in your working dir).
- **Proof renders:** `bg_stock_preview.png` / `bg_mod_preview.png` (before/after).
- **Staged tree:** the unpacked `stage/2/` dial tree you build from.

---

## CLI usage (reusable tools)

The recipe above is generalized into two committed, **stdlib-only** CLIs (no Pillow required —
they never re-encode assets, they only move bytes):

- **`scripts/dial_unpack.py <dial.bin> <outdir>`** — extract a dial to a working dir, preserving
  the exact tree (incl. the slot dir), and print a manifest summary.
- **`scripts/dial_pack.py <workdir> <out.bin> [--no-verify]`** — repack a working dir into a
  valid GTX2 dial. **Always DEFLATE-9, never store.** Preserves the slot dir + directory entries,
  passes asset bytes through untouched, self-checks archive integrity, and (unless `--no-verify`)
  warns on the documented gotchas: missing `firmware/dial.json`, unparseable manifest, missing
  `app_preview.png`/`preview_0565.bmp`, and any `*_0565.bmp` that isn't authored as 24-bit.

`<workdir>` for `dial_pack.py` is the directory that **contains** the slot dir (i.e. exactly what
`dial_unpack.py` extracted into). Flat dials (no slot dir) work the same way.

### Typical workflow
```bash
python3 scripts/dial_unpack.py watchfaces/cw07630401.bin build/     # -> build/2/firmware/...
#  ...edit build/2/firmware/dial.json and assets; regenerate both previews...
python3 scripts/dial_pack.py   build/ watchfaces/custom/mydial.bin  # DEFLATE-9, verified
```

### Round-trip test result (verified 2026-07-11, system Python 3.12, no venv)
Unpack a stock dial then repack, for both a slot-dir dial and a flat dial:

| stock dial | layout | file set vs stock | assets | ZIP | dir markers |
|---|---|---|---|---|---|
| `cw07630401` | slot dir `2/` | **IDENTICAL** (116 files) | all byte-identical (md5) | valid, all DEFLATE | 16/16 exact |
| `act06120103` | flat `firmware/` | **IDENTICAL** (76 files) | all byte-identical (md5) | valid, all DEFLATE | 8 → 9* |

\* The packer emits standard directory entries, so the flat dial gains one benign `firmware/`
directory marker that stock `act06120103` happened to omit (it kept its *sub*-dir markers). This is
structure-equivalent — the extracted tree is byte-identical either way — because ZIP directory
markers are optional metadata implied by the file paths. The safety-check warnings were also
confirmed to fire (e.g. packing a tree with `app_preview.png` removed emits the missing-preview
warning) and to be suppressible with `--no-verify`.

---

## Dial generator — build a dial from scratch (`scripts/dial_create.py`)

Beyond editing a stock dial, `scripts/dial_create.py` generates a **complete, valid** GTX2 dial
from a background image + a small layout config. **Requires Pillow** (the only tool here that
does — it renders glyphs and converts images); packs via `dial_pack.py` (so DEFLATE-9 + slot dir
+ the safety checks all apply). BLE upload is hardware-gated — this is the **creation half**;
on-device rendering of the generated glyphs is unverified until an upload path exists.

What it does, handling every documented gotcha:
- writes `dial.json` from the documented vocabulary (466×466, `platform "ats3085s"`, `dial_version 2`);
- converts the background to a **24-bit** `BG_0565.bmp` (the `_0565` RGB565 target);
- renders a digit-glyph font per text element (`<Font>/<Font>_0..9_8888.png`, RGBA8888) from a TTF
  (DejaVuSans-Bold, falling back to Pillow's scalable default);
- generates **both** previews — `app_preview.png` (466×466 RGBA, with sample values composited) and
  `preview_0565.bmp` (256×256 24-bit BMP);
- stages under a numeric **slot dir** (default `1/`) and packs with `dial_pack.py`.

### Config schema (coords in 466×466 space)
```json
{
  "name": "NebulaClock",
  "background": "bg.png",
  "dial_type": 1,
  "elements": [
    {"type":"time","x":103,"y":175,"color":"#FFFFFF","digit_w":76,"digit_h":116,"gap":30,"colon":true},
    {"type":"date","x":210,"y":70,"color":"#8FB7FF","digit_w":24,"digit_h":32,"min_numwidth":2},
    {"type":"steps","x":120,"y":355,"color":"#00E5FF","digit_w":22,"digit_h":30,"max_digits":5},
    {"type":"heart","x":300,"y":355,"color":"#FF5252","digit_w":22,"digit_h":30,"max_digits":3}
  ]
}
```
Element `type`s → GTX2 item `type`s: `time` → `hour`+`min` (+ optional `:` colon icon), plus
`hour`, `min`, `date`, `month`, `steps`→`step`, `heart`, and `icon` (a static user image).

### Usage
```bash
python3 scripts/dial_create.py <config.json> <out.bin> [--workdir DIR] [--slot N]
```

### Worked example (generated + validated 2026-07-11)
Inputs (`config.json` + a generated `bg.png`) →
output **`watchfaces/custom/nebula_clock.bin`** (139 KB): a functional digital face — big white
`HH:MM` with colon, blue date, cyan steps, red heart, over a radial-glow background.

Validation (`dial_unpack.py` + structural diff vs stock `cw07630401`), all **OK**:

| check | result |
|---|---|
| parses via `dial_unpack.py` | name `NebulaClock`, 466×466, `ats3085s`, 7 items |
| top-level manifest keys ⊇ stock | OK (none missing) |
| widgets / types | `icon`,`text` / `background`,`hour`,`min`,`date`,`step`,`heart`,`other` |
| all file entries DEFLATE | OK |
| both previews present | OK |
| `BG_0565.bmp` / `preview_0565.bmp` are 24-bit BMP | OK (466×466×24 / 256×256×24) |
| `app_preview.png` is 466×466 RGBA PNG | OK |
| every referenced glyph (0–9) + picture present | OK (none missing) |

Not verified (hardware-gated): on-watch rendering of the generated glyphs, and device acceptance —
same upload gap as the rest of the watch-face track (`watchface-format.md` §3).
