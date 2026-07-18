# GTX2 / Actions ATS3085(S) watch-face (dial) format & delivery

Verified against **3 real GTX2 dials** pulled from the vendor CDN and unpacked locally
(2026-07-11). Correlated with our BLE capture (`decode-notes.md`) and the public ATS3085S
community tooling.

> **TL;DR**
> - A GTX2 dial `.bin` is an ordinary **ZIP** of a declarative **`dial.json`** manifest plus
>   loose image assets (**PNG = RGBA8888**, **BMP = RGB565**). It is *not* a compiled binary.
> - This is a **different format** from the well-documented community `dipcore/unpack_clock_res`
>   / `gen_clock` **`.res`** container (that one targets DT No.1 / DT Ultra / "DT Watch 11 Pro").
>   The dipcore unpacker **rejects GTX2 dials** (proven below). Both are ATS3085S — two
>   separate dial ecosystems.
> - **No special tool is needed for GTX2 dials — `unzip`/`zip`.** The manifest is human-readable JSON.
> - The CDN `<name>.png` preview is **byte-identical (md5) to `firmware/app_preview.png`** inside the ZIP.
> - **Building** a custom dial `.bin` is HIGHLY feasible. **Delivering** it over BLE is
>   **RESOLVED** — a later capture *did* record an install. See **`docs/watchface-install.md`**:
>   the install is a bulk-plane (`D1/D2/D3/D4`) push of a **transcoded native container** (not
>   the ZIP), it auto-activates, and it is implemented as the `dial-push` CLI command. The
>   "one missing capture" the sections below call for has since been obtained and decoded.

---

## Part 1 — The GTX2 dial container (VERIFIED)

### 1.1 It's a ZIP

```
$ file watchfaces/cw07630401.bin
Zip archive data
$ xxd -l4 watchfaces/cw07630401.bin
50 4b 03 04                                        # "PK\x03\x04"
```

Content-Type from the CDN is `application/zip`. Compression varies per dial (STORE or DEFLATE);
irrelevant to consumers — standard unzip handles both.

### 1.2 Directory layout

Two subtrees, split by consumer. A numeric **slot dir** (`2/`, `4/`) may prefix everything, or
may be absent (flat `firmware/…`). The slot prefix is **not required** — `act06120103` ships flat.

```
[<slot>/]
├── app/                     # PHONE-APP side only (not sent to watch)
│   ├── app.json             #   quick-launch shortcut config for the Runmefit app UI
│   ├── data/*_8888.png      #   80×80 app icons
│   └── image/bg.png         #   466×466 app background
└── firmware/                # WATCH side — this is the resource set the watch renders
    ├── dial.json            #   THE manifest (layers, positions, fonts, colors)
    ├── file.json            #   declares the glyph/array asset folders + their format
    ├── app_preview.png      #   466×466 RGBA — phone preview (== the CDN <name>.png)
    ├── preview_0565.bmp     #   256×256 24-bit BMP — on-watch dial-picker thumbnail
    ├── <Bg>_0565.bmp        #   466×466 background (RGB565 target; see 1.5)
    └── <Font>/<Font>_N_8888.png   # numbered glyph/state images per element
```

`firmware/` is the vendor's own label for "the on-device resource bundle" — the strongest
signal that the app sends (a packed form of) **`firmware/`**, not the whole ZIP, to the watch.

### 1.3 `dial.json` — the manifest (top-level fields)

| Field | Example | Meaning |
|---|---|---|
| `frame_version` | `1` | manifest schema version |
| `dial_version` | `1` / `2` | dial revision |
| `name` | `"CW07_6304_01"` | internal dial name (≈ the `.bin` basename, case-normalized) |
| `describe_name` | `"Heartbeat"` | optional friendly label |
| `dial_type` | `1` / `6` | vendor category id (not simply analog/digital) |
| `resolution_ratio` | `"466x466"` | canvas size — **all samples 466×466** (matches the AMOLED) |
| `platform` | `"ats3085s"` / `"ats3085c"` | **confirms the SoC in the asset itself** |
| `size` | `160` / `224` / `256` | budget/footprint hint (KB?); not linear w/ zip size — unconfirmed |
| `enable_pic_compress` | `0` | whether assets are (LZ4?) compressed when packed — samples all `0` |
| `preview` | `"preview_0565.bmp"` | on-watch preview asset |
| `app_preview` | `"app_preview.png"` | phone preview asset (= CDN `<name>.png`) |
| `item` | `[…]` | active-mode element list (see 1.4) |
| `fade_item` | `[…]` | always-on / dimmed (AOD) element list — same schema, `Fade_*` assets |

### 1.4 `dial.json` — element (widget) vocabulary

Each entry in `item[]`/`fade_item[]` is one rendered element. Common fields: `widget`, `type`,
`x`, `y`, `w`, `h`, optional `serial_number` (maps to a firmware element/data slot),
`enable_click`. Widget-specific fields observed across the 3 dials:

| `widget` | Purpose | Key fields |
|---|---|---|
| `icon` | static/stateless image | `picture` (asset filename) |
| `text` | data rendered from a digit font | `font` (glyph folder), `fontnum` (glyph count 10–13), `min_numwidth` (leading zeros), `align`, `color` (`#RRGGBB`), `enable_font`, `unit_count`/`unit_data` (unit glyph indices), `display_opa` |
| `array` | pick 1 of N state images | `array_img` (folder), `count` (states: week=7, battery=5, gradient=11), `color` |
| `curved` | text laid on an arc | `font`, `fontnum`, `centerx`, `centery`, `angle`, `angle_space`, `total_count`, `align` |
| `pointer` | rotating hand (analog) | `picture`, `rotation` (initial °), `centerx`/`centery` (pivot), `start_angle`/`end_angle`, `start_value`/`end_value` |

`type` values seen (the data the element is bound to): `background`, `other`, `hour`, `min`,
`hourhi`, `hourlo`, `minhi`, `minlo`, `second`, `week`, `day`, `date`, `month`, `heart`,
`battery`, `calorie`, `distance`, `step`, `calling`, `updategao`. Split hour/min into hi/lo
tens/units digits is common. `background` uses the big `_0565.bmp`.

### 1.5 Image encodings (VERIFIED)

The filename **suffix names the target pixel format**, but the shipped file is a normal image:

| Suffix | Target format | Shipped as | Verified detail |
|---|---|---|---|
| `_8888.png` | RGBA8888 | standard PNG w/ alpha | `PNG … 8-bit/color RGBA` — glyphs, icons, hands, overlays |
| `_0565.bmp` | RGB565 | **24-bit uncompressed Windows BMP** (BGR, 4-byte row pad) | header `BM … 466×466 … 0x18`=24bpp, `BI_RGB`. The `0565` is the *destination* format; the app/firmware down-samples 24-bit→RGB565 at pack/transfer time |

Preview assets: `preview_0565.bmp` = **256×256** 24-bit BMP (watch dial-picker);
`app_preview.png` = **466×466** RGBA PNG (phone app; = CDN `<name>.png`).

Glyph folders hold numbered frames: digit fonts `Name_0..9`, arrays like `Week_0..6`,
`battery_0..4`, `Gra_0..10` (gradient ring). Animation frames live under `Fade/…`.
`file.json` enumerates these folders and each folder's format (`png`/`bmp`):

```json
{ "item": [ {"name":"Hour","format":"png"}, {"name":"Week","format":"png"} ],
  "fade_item": [ {"name":"Fade/Time","format":"png"} ] }
```

RGB565 (2 bytes/pixel, **big-endian**) is corroborated by the firmware header (`decode-notes.md`).

### 1.6 The three sampled dials (all downloaded + unpacked)

| dial `.bin` | zip | style | manifest `name` / `dial_type` / `platform` | asset flavor |
|---|--:|---|---|---|
| `cw07630401` | 275 KB | digital + data ring | `CW07_6304_01` / 6 / ats3085s | PNG glyphs, BMP bg, `app/` present, Fade AOD |
| `act06120103` | 126 KB | digital big-clock | `Act06_1201_03` / 1 / ats3085c | PNG glyphs, BMP bg, flat (no slot dir) |
| `cwr01g21505` | 432 KB | **analog** (hour/min/sec hands) | `CWR01G_21505` / 1 / ats3085s | BMP glyphs, `pointer` widgets, Fade bg |

---

## Part 2 — Two distinct ATS3085S dial ecosystems

The community's documented format is **not** the GTX2 format. This is the key correlation result.

### 2.1 GTX2 / Runmefit (this device) — ZIP + JSON, *authoring* form
Human-readable manifest + loose PNG/BMP. No magic, no binary layer table, no LZ4. Described in Part 1.

### 2.2 DT No.1 / DT Ultra / "DT Watch 11 Pro" — `gen_clock` V3 `.res`, *compiled* form
This is what `dipcore/unpack_clock_res` + `gen_clock` handle. Single binary file:

```
0x00  8  Magic "Sb@*O2GG" or "II@*24dG"
0x08  4  clock_id  (BE; bit31 internal flag, bits16-23 resolution, bits0-15 base id)
0x0C  4  thumb_start   0x10 4 thumb_len   0x14 4 img_start
0x18  4  img_len       0x1C 4 layer_start
blocks: [thumbnail][images][z_images][layer_data]
image chunk (16B hdr): img_type, compressed, payload_len(24 LE), h(12b), w(12b), 8×0, payload(LZ4)
img_type: 3 gif · 9 jpg · 71 rgb8888(BGRA) · 72 rgb8565 · 73 rgb565 · 74 rgb1555 · 75 index8
layer_data: records of drawType/dataType/[interval]/[area_num]/alignType/x/y/num/entries
```

### 2.3 Proven: the dipcore unpacker cannot read GTX2 dials

```
$ tools/unpack_clock_res/.venv/bin/python unpack.py watchfaces/cw07630401.bin
ValueError: unexpected magic b'PK\x03\x04…'; expected one of {b'II@*24dG', b'Sb@*O2GG'}
```

So the two ecosystems are structurally incompatible at the container level. **Conceptual overlap
is real, though:** both are 466×466 ATS3085S faces, both ultimately RGB565/RGBA8888, both are
layer/element lists with x/y/w/h + digit-font glyph sets. The dipcore docs remain the best
public reference for the *pixel encodings* and the DT-family. Nice cross-check: dipcore's
`clock_id` resolution map lists `0x0D0000 → 466×466`, i.e. it already knows the GTX2-class
resolution even though it can't parse the GTX2 container.

---

## Part 3 — Delivery: how a dial reaches the watch

Three artifacts, three roles:

| Artifact | Where | Role | Status |
|---|---|---|---|
| `0x16` catalog | BLE command channel | watch reports **installed dial `.bin` names** | **VERIFIED in captures** |
| `<name>.png` | CDN | phone-app preview (= `app_preview.png`) | **VERIFIED (md5 match)** |
| `<name>.bin` | CDN | the dial ZIP bundle | **VERIFIED (downloaded + unpacked)** |

### 3.1 CDN scheme (VERIFIED)

```
https://download.runmefitserver.com/dial/gtx2/gtx2/<name>.bin   # ZIP bundle
https://download.runmefitserver.com/dial/gtx2/gtx2/<name>.png   # == firmware/app_preview.png
```

Not every `0x16` catalog name resolves on this CDN. Downloaded OK (200/zip): `cw07630401`,
`act06120103`, `cwr01g21505`. Returned empty/404: `CW07_6208_01`, `CW06G_187_03`,
`CW06G_187_04`, `num061109_10`, `YHZN_1021@LC` — these are almost certainly **firmware
built-in dials** (shipped inside the OTA image, not on the public dial CDN) or region/case
variants. The catalog namespace (mixed-case) and the CDN (lowercase for the market dials) overlap
but are not identical.

### 3.2 BLE transfer — the captured GTX2 dialect

**Our captured GTX2 dialect** (`decode-notes.md`): `0xC1`-framed protobuf + CRC-16/CCITT-FALSE;
**dial/resource catalog = opcode `0x16`**; bulk payloads on the **`0xD2`** channel
(`D2 | counter | 234 raw bytes`, same transport proven for firmware DFU).

`0x16` (C1) lists the installed dials. The **install/switch** path was later captured in full and
is documented byte-exact in [`watchface-install.md`](watchface-install.md): a dial install rides
the **`D1/D2/D3/D4` bulk plane** and **auto-activates** (it is *not* a renumbered C1
announce/stream). §3.3–§3.4 below are the original pre-capture census, retained for context.

### 3.3 Capture census — no dial install exists in our data (VERIFIED)

Full C1 command-channel opcode union across pairing/features1/features2/workout/notif-real:
`0x01 0x02 0x03 0x04 0x05 0x0e 0x10 0x11 0x12 0x13 0x16 0x22`. **`0x16` is the only
dial-related opcode present** — and it's the read-only catalog. No announce/stream/switch opcode
was exercised.

`0xD2` bulk volume per capture (app→watch), to rule out a dial-sized transfer:

| capture | 0xD2 frames | biggest contiguous run | verdict |
|---|--:|--:|---|
| `fw-complete` | 10,214 | ~2,098 KB | firmware OTA (2.1 MB image) |
| `call-012300` * | 10,235 | ~2,098 KB | firmware transfer (appeared mid-session) |
| `notif-real` | 1,034 | ~83 KB | too small for a dial; AGPS/notification-icon |
| `workout` / `features2` / `features1` / `pairing` | 306–456 | ~5 KB | periodic AGPS/health bulk |

\* `call-012300-btsnoop_hci.log` was created during this session (likely by the firmware-DFU
agent); it is a firmware transfer, not a dial install.

**Conclusion: a fresh "install a watch face from the Runmefit app" BLE capture is required.**
It must capture (with the app pushing a *market/custom* dial to the watch):
1. the C1 opcode that **announces** a dial file (the `0xEA`-equivalent + its info block);
2. whether the payload is the raw `firmware/` ZIP subtree or a **transcoded packed blob**
   (this is the single biggest unknown — see 3.4);
3. the C1 opcode + progress/commit handshake for the **data stream** (the `0xEB`/`0xD2` equivalent);
4. the **switch-active-dial** opcode (`0xED`-equivalent) and the **dial-Id** assigned to a custom face.

### 3.4 What we can and cannot yet say about the on-wire payload

- **VERIFIED:** the CDN distributes a ZIP; the watch reports dials by `.bin` name via `0x16`;
  bulk transport is `0xD2` 234-byte chunks.
- **INFERRED (needs the 3.3 capture):** the app almost certainly transcodes the `firmware/`
  subtree — converting `_0565.bmp` (24-bit) → packed RGB565 and possibly LZ4-compressing per
  `enable_pic_compress` — into the watch's native resource container before streaming, rather
  than shipping the raw ZIP. Rationale: (a) the `app/` vs `firmware/` split; (b) the on-device
  firmware stores resources in Actions `sdfs`/`other_res` LZ4 containers (see Part 4). Unproven
  until captured.

---

## Part 4 — Community tools: which apply to GTX2

Cloned into `tools/` (gitignored):

| Tool | Targets | Applies to GTX2 dial `.bin`? | Use for us |
|---|---|---|---|
| **`dipcore/unpack_clock_res`** (+ `gen_clock`) | DT No.1 / DT Ultra / DT Watch 11 Pro `.res` (magic `Sb@*O2GG`) | **No** — rejects the ZIP magic (proven §2.3) | Reference for RGB565/8888/1555/index8 encodings and the DT-family; not a GTX2 parser |
| **`Viper7000/ATS3085S_firmware_unpacker`** | ATS3085S **firmware images** (`.fw` → `sdfs` → `other_res`/`sec_res`/`video_res`, LZ4) | **No** (different layer) | Inspect **on-device** resource storage / built-in dials; informs the 3.4 transcode hypothesis |
| **`unzip` / `zip`** (system) | ZIP | **Yes — this is all a GTX2 dial needs** | Unpack + repack GTX2 dials directly |

The XDA "watchfaces for smartwatches with ATS3085S CPU" thread and the DT Watchfaces blog are
DT-family / `gen_clock` centric — useful community context, but they describe the §2.2 `.res`
format, not the GTX2 ZIP format documented here. (XDA is behind a Cloudflare/login wall; not
fetchable headless.)

---

## Part 5 — Custom watch-face feasibility

### 5.1 Build a valid GTX2 dial `.bin` — **HIGH feasibility, no vendor tooling needed**

Procedure (all steps exercised except the final on-watch validation):
1. Start from an unpacked sample (e.g. `cwr01g21505` for analog, `act06120103` for digital).
2. Replace `firmware/<Bg>_0565.bmp` with a 466×466 **24-bit BMP** (BGR, bottom-up, 4-byte row pad).
3. Replace digit/array glyph PNGs (`*_8888.png`, RGBA) and/or `_0565.bmp` glyphs, keeping the
   `Name_N` numbering and the folder set declared in `file.json`.
4. Edit `firmware/dial.json`: element `type`/`x`/`y`/`w`/`h`/`font`/`color`/`align`, plus
   `name`, `preview`, `app_preview`.
5. Regenerate `app_preview.png` (466×466) and `preview_0565.bmp` (256×256 24-bit BMP).
6. `zip` the tree back (slot dir optional). Keep `platform`/`resolution_ratio` matching the device.

**Open risks:** the meaning of `size`, `dial_type`, and `serial_number`; whether the app/watch
validates a signature or requires a registered dial-Id; and the exact RGB565 conversion the app
expects (our samples ship 24-bit BMP as input, so authoring in 24-bit BMP is the safe bet).

### 5.2 Deliver over BLE without the vendor app — **RESOLVED (see docs/watchface-install.md)**

> **UPDATE:** the capture called for below was obtained. The C1-dialect answer turned out to
> be simpler: a dial install is **not** a renumbered C1 announce/stream — it rides the existing
> **`D1/D2/D3/D4` bulk plane** (filename `custom_id_<id>.bin`) and **auto-activates**, and the
> payload is a **transcoded native container**, not the raw ZIP. Full protocol + byte evidence:
> `docs/watchface-install.md`. The remaining open piece is the ZIP→native **transcoder**
> (per-asset image re-encode), not the transport. The original (now-superseded) analysis:


Have (at the time): the proven `0xD2` bulk transport and the `0x16` catalog to verify installs.
Missing: the **C1-dialect opcode numbers** for announce/stream/switch, the **payload form** (raw
ZIP vs transcoded — §3.4), the **custom dial-Id** allocation, and any **commit/CRC** handshake.
(All since resolved — see [`watchface-install.md`](watchface-install.md).)

**One "install a watch face" capture (§3.3) resolves all of it.** After that, a Gadgetbridge
`installDial()` path is a moderate build: repack ZIP → (transcode if needed) → announce → stream
on `0xD2` → switch. Firmware-brick risk is low (dials are user resources, not the boot image),
but a bad transfer could leave a broken face until re-flashed from the app.

### 5.3 Recommended next steps
1. **Capture a dial install** (market dial, then ideally a custom one) — the critical unblock.
2. Decode the announce/stream/switch C1 opcodes; determine the payload form (§3.4).
3. If transcoded, use `Viper7000` to learn the on-device container and mirror it; if raw ZIP,
   trivial.
4. Prototype `installDial()` in the `starmax` coordinator; validate with a repacked known-good dial
   before trying a hand-built one.

---

## Appendix — artifacts produced by this task

**`tools/` (gitignored) — cloned community RE tools:**
- `tools/unpack_clock_res/` — `dipcore/unpack_clock_res` (gen_clock V3 `.res`; + `.venv` with lz4+Pillow)
- `tools/ATS3085S_firmware_unpacker/` — `Viper7000/ATS3085S_firmware_unpacker` (firmware `.fw`/sdfs/res)

**`watchfaces/` (gitignored) — real GTX2 dials from the CDN (`.bin` + `.png`):**
- `cw07630401.bin` (275 KB) / `.png` · `act06120103.bin` (126 KB) / `.png` · `cwr01g21505.bin` (432 KB) / `.png`

**`work/watchface-format/` — working artifacts:**
- `channel_census.py` — per-capture `0xD2` bulk census (reused in §3.3)
- `unpacked/<dial>/` — the three dials unzipped for inspection
- `net.log` — clone + download log

**Cross-references:** `decode-notes.md` (C1 framing, CRC, `0x16`, `0xD2` DFU),
`watchface-install.md` (the captured install protocol + native container),
`related-work-d6flasher.md` (community-tool pointers).
