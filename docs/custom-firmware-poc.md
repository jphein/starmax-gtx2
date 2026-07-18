# Custom-Firmware PoC — patch→repack pipeline + Zephyr build scoping

**Date:** 2026-07-11 · **Mode:** software-only, **nothing flashed**. Spare not in hand.
**Scope of writes:** `firmware/` (gitignored) + this doc. Gadgetbridge tree untouched.
**Companion:** `firmware-ota-byte-map.md` (container spec), `hardware-teardown-guide.md` (recovery/flash), `recovery-research notes` (boot chain).

> ⚠️ **DEFERRED / recovery-gated.** The patched images here are **software validity proofs**. Do **not** flash them. A real device flash needs (a) a proven unbrick path (teardown L4 — external programmer + golden.bin) and (b) explicit JP go/no-go. **Update 2026-07-13:** `word@0x2C` is **RESOLVED** — it's a fixed Actions sub-image **magic** (`0xA578875A`), *not* a checksum (byte-map §4), so it is **left byte-identical** on repack and is **not** a device-enforcement risk. The only remaining gate is the physical recovery path (#17).

---

## Part 1 — PATCH → REPACK PoC (pipeline proven in software)

**Goal:** prove that a modified app payload can be repacked into a **structurally valid** `FA EE EB DE` image — both CRC-32 gates pass and the diff vs stock is *only* the patched bytes + the two recomputed CRCs.

### 1.1 The patch (minimal, safe, length-preserving)
Target = a **git-describe build-provenance tag** inside the app payload (pure data, no `%` format specifiers, not referenced in control flow):

```
section0 offset 2083325 (unique):
  cws05_v1.0.0_24073101_updated_based_on_this_version-517-ge0346cbb7086
```
Change **one byte** — the last digit of the `24073101` build serial — `'1'(0x31) → '9'(0x39)`, i.e. `…_24073101_…` → `…_24073109_…`. Length-preserving (file stays 2,148,064 B), cosmetic, unmistakably ours.
- Patch site: `section0_firmware_zephyr.bin[2083345]` = **`.ota` offset `0x1FCA7D`**.

### 1.2 Result (verified)
| Field | Stock | Patched |
|---|---|---|
| file size | 2,148,064 | 2,148,064 (unchanged) |
| build tag | `…_24073101_…` | `…_24073109_…` |
| **inner** `data_crc32@0x38` | `0xE78749BE` | **`0xC36DA41F`** (recomputed) |
| **outer** `payload_crc32@0x04` | `0xE6E6BB23` | **`0xF2F3392F`** (recomputed) |
| `checksum_A@0x2C` | `0xA578875A` | `0xA578875A` (**untouched**) |

**Gate check** — re-unpacking the patched image verifies **both** stored CRC-32s recompute correctly:
```
outer crc @0x04 : stored 0xF2F3392F computed 0xF2F3392F  match=True
inner crc @0x38 : stored 0xC36DA41F computed 0xC36DA41F  match=True
```
**Diff vs stock = exactly 9 bytes**, all expected, nothing else:
```
off 0x04–0x07  23 BB E6 E6 -> 2F 39 F3 F2   (outer CRC-32, LE 0xF2F3392F)
off 0x38–0x3B  BE 49 87 E7 -> 1F A4 6D C3   (inner CRC-32, LE 0xC36DA41F)
off 0x1FCA7D   0x31 '1'    -> 0x39 '9'      (the one patched payload byte)
```
→ **Pipeline proven:** unpack → edit payload → repack → structurally valid image, CRC gates green, surgical diff.

### 1.3 Exact repeatable recipe
```bash
cd ~/Projects/smartwatch
OTA=firmware/cb05_yhzn01_v1.0.3_20241218_02.ota
mkdir -p firmware/_poc/extract

# 1) unpack + confirm stock CRCs verify (outer 0xE6E6BB23, inner 0xE78749BE)
python3 scripts/ota_unpack.py "$OTA" firmware/_poc/extract

# 2) length-preserving 1-byte patch to the build tag in the app payload
python3 - <<'PY'
src="firmware/_poc/extract/section0_firmware_zephyr.bin"
dst="firmware/_poc/section0_patched.bin"
d=bytearray(open(src,"rb").read())
i=d.find(b"cws05_v1.0.0_24073101"); assert i!=-1 and d.count(b"cws05_v1.0.0_24073101")==1
j=i+len(b"cws05_v1.0.0_24073101")-1          # final '1' of 24073101
assert chr(d[j])=='1'; d[j]=ord('9')          # -> 24073109, same length
open(dst,"wb").write(d)
print("patched section0[%d] (.ota 0x%X): '1'->'9'"%(j,0x6C+j))
PY

# 3) repack (recomputes inner @0x38 then outer @0x04; leaves @0x2C)
python3 scripts/ota_repack.py "$OTA" firmware/_poc/section0_patched.bin \
        firmware/cb05_yhzn01_v1.0.3_20241218_02_PATCHED.ota

# 4) verify: both gates green, diff = 9 bytes
python3 scripts/ota_unpack.py firmware/cb05_yhzn01_v1.0.3_20241218_02_PATCHED.ota firmware/_poc/verify
cmp -l "$OTA" firmware/cb05_yhzn01_v1.0.3_20241218_02_PATCHED.ota   # exactly 9 lines
```
Output: `firmware/cb05_yhzn01_v1.0.3_20241218_02_PATCHED.ota` (gitignored). Intermediates under `firmware/_poc/` are regenerable.

### 1.4 What it does and does NOT prove
- **Proves:** the container math + our unpack/repack tools are correct; a modified app repacks into a valid image with both CRC-32 gates satisfied and a clean, auditable diff. This is the software half of custom FW.
- **Does NOT prove device acceptance.** Two open gates remain, both **deferred**:
  1. `checksum_A@0x2C` (byte-map §4) is unresolved and **left stale** by the repacker. If the bootloader/DFU enforces it over the app region, this image would be rejected. Only a device-accept test (or a NOR dump / 2nd sample) settles it.
  2. Even if accepted, the *content* must be a valid bootable image for the hardware.
- **Safer route that sidesteps both:** flash the raw app partition **directly via the external programmer** (teardown-guide §3) — the on-NOR image doesn't go through the OTA container, so neither the `FA EE EB DE` wrapper nor `word@0x2C` is involved.

---

## Part 1b — VISIBLE-marker POC + flasher validation (2026-07-13)

**Goal (#17 offline prep):** produce a **ready-to-flash custom image** carrying a change we can recognise post-flash, and prove it passes the **merged** `otafmt` parser + `flash-firmware` dry-run — so we can flash it **first** (right after the byte-identical stock image) the instant #17 recovery is proven.

### 1b.1 The patch (minimal, safe, reversible, length-preserving)
Target = the free-form provenance text inside the git-describe **build string** (pure ASCII data, unique, not a format specifier, not control-flow):
```
section0 offset 2083347  (.ota offset 0x1FCA7F):
  before: cws05_v1.0.0_24073101_updated_based_on_this_version-517-ge0346cbb7086
  after:  cws05_v1.0.0_24073101_LUCID_customfw_poc_flashtest_-517-ge0346cbb7086
```
`updated_based_on_this_version` → `LUCID_customfw_poc_flashtest_` — **29 bytes, exact length-preserving**, unmistakably ours, trivially revertible (re-flash stock). Chose the build-identity string because it is the **safest** editable byte range (pure data) and the natural "is this our firmware?" marker.

### 1b.2 Repack + validation (all offline, verified)
Ran `ota_repack.py` (preserves `magic@0x2C`, recomputes inner `@0x38` then outer `@0x04`), then re-`ota_unpack.py`:
| Field | Stock | POC |
|---|---|---|
| inner `data_crc32@0x38` | `0xE78749BE` | **`0x8DCB824F`** (recomputed, re-verifies) |
| outer `payload_crc32@0x04` | `0xE6E6BB23` | **`0x97AD30C6`** (recomputed, re-verifies) |
| `magic@0x2C` | `0xA578875A` | `0xA578875A` (**byte-identical**) |
| file size | 2,148,064 | 2,148,064 (unchanged) |

**Byte-diff vs stock = exactly 37 bytes, all expected:** `0x04–0x07` (outer CRC) · `0x38–0x3B` (inner CRC) · `0x1FCA7F–0x1FCA9B` (the 29 marker bytes). Nothing else; `@0x2C` untouched.

**Validated against the merged #29 flasher (`starmax_client.otafmt` + `flash-firmware`):**
- `parse_ota_image(poc)` → **`valid=True`, `section_magic_ok=True`, integrity_errors=(none)** — identical verdict to stock.
- `flash-firmware <poc>` (default **dry-run**) → `integrity: VALID`; plan = `9183 frames / 2,148,064 B`; `D1 = d100e0c62000e0c620000f7265732e6f746100`; `D2 = 9180 chunks`; **`D4 = d400001b0d0000`** (whole-file CRC-16/XMODEM `0x0D1B`, correctly recomputed for the new content); `sect magic @0x2C 0xA578875A [never recomputed, OK]`; **"nothing transmitted"** (force-flash + `RECOVERY_PROVEN(#17)` gates hold).

**Ready payload:** `firmware/cb05_yhzn01_v1.0.3_LUCIDPOC.ota` (gitignored). Regenerate: `ota_unpack` stock → patch the 29 bytes → `ota_repack`.

### 1b.3 Honest note on "visible" (no overpromise)
This POC **proves the patch→repack→valid-container→flasher-accepts pipeline** and yields a ready payload. **Where the marker is *observable* post-flash is a hypothesis to confirm at flash time**, because the watch's on-screen version is **numeric-computed** (`##version V%d.%02d.%02d` from `fw_*_version` fields), *not* a rendered copy of this string — so the build string may **not** appear on the About screen. Confirmation channels, best-first:
1. **UART0 console** (available during the #17 teardown) — the boot/`os_ota` path prints build identity; most reliable "our fw is running" signal.
2. **Our BLE client** device-info readback — *if* the descriptor surfaces the build tag.
3. **About screen** — only if it shows the build string (uncertain; version there is numeric).

⚠️ **Do NOT** patch the numeric version fields to force an on-screen change — the OTA acceptance path includes a **device/version-compat check** (`os_ota "does not support this ota-firmware"`), so altering the version could get the image **rejected**. If a *guaranteed on-screen* marker is wanted, the safe escalation is a **rendered UI/resource string** (confirm it's in `app.bin` vs the resource partition first) — a follow-up, higher-RE-effort step. For proving custom firmware *runs*, the build-string marker + UART confirmation is the right first move.

---

## Part 2 — Zephyr build scoping (plan only; full build NOT run)

**Question:** can we build a minimal custom `app.bin` from the LVGL Actions SDK for the `ats3085s4_dev_watch_ext_nor` board, and how far is that from a GTX2-flashable image?

### 2.1 Toolchain & board target `[S]`
- **SDK:** `github.com/lvgl/lv_port_actions_technology`, vendoring `action_technology_sdk/` — **Zephyr 2.7.0** (`zephyr/VERSION`), **west** (`.west/config` → `zephyr/west.yml`), CMake/Ninja, Actions HAL + LVGL + VGLite GPU.
- **Board:** **`ats3085s4_dev_watch_ext_nor`** — matches the retail die's `S4` sub-mark and `soc_boot.h`'s `is_apm/3085s4` flag. Board dir exists under `application/app_demo/{lvgl_demo,noise_cancel_demo}/boards/ats3085s4_dev_watch_ext_nor/` (with its own `firmware.xml`). (The full `bt_watch` watch app ships `ats3089*` "leopard" boards; the `ats3085s4` target lives in the demos.)
- **Minimal app to build:** `application/app_demo/lvgl_demo` (or `gpu_demo`) — smallest LVGL-on-ATS3085S4 target that emits a real `zephyr.bin`.

### 2.2 Build steps (concrete)
1. **Env:** Zephyr **SDK 0.13.x** (the 2.7.0-era toolchain: `arm-zephyr-eabi` gcc), `west`, `cmake≥3.20`, `dtc`, `ninja`, python deps. (2.7.0 predates modern Zephyr SDK; pin the matching version or the DTS/Kconfig will fail.)
2. **Fetch:** clone the repo; `west init -l action_technology_sdk` then `west update` against the vendored manifest (modules are largely in-tree).
3. **Build:** `west build -b ats3085s4_dev_watch_ext_nor application/app_demo/lvgl_demo` → `build/zephyr/zephyr.bin`.
4. **Package (SDK-native):** the SDK post-build runs `build_boot_image.py` (mbrec) + `build_ota_image.py` → produces `app.bin` (the raw SYSTEM partition image) and an **`AOTA`** OTA `.fw`.
   - **Expected output = an `AOTA` (0x41544F41) container — NOT the retail `FA EE EB DE`.**

### 2.3 Gap to a GTX2-flashable image (the hard parts) `[S/I]`
1. **Container mismatch (transport only).** SDK emits `AOTA`; retail BLE-DFU consumes `FA EE EB DE`. *Bridging is already solved for the app payload:* take the SDK's `app.bin` as the section-0 payload and run `scripts/ota_repack.py <retail.ota> app.bin out.ota` — this splices it into a retail `FA EE EB DE` wrapper (same pipeline as Part 1). **Or** avoid the container entirely by programmer-flashing `app.bin` to the app offset (teardown §3.3). `[S]`
2. **Board bring-up (the real blocker).** The SDK `ats3085s4_dev_watch_ext_nor` is an **EVB**, not the GTX2 `CB05-MTL` board. Differences that must be ported before the app runs correctly: MIPI-DSI **panel init/model** (466×466), **touch controller** (I²C), **sensor stack** (PPG/HR, accel, baro), **button/crown GPIO**, **flash geometry/partition offsets**, and the **WTM2101** co-processor handshake. A dev-board build will boot the M33 but likely show a blank/garbled screen and dead sensors on retail hardware. `[I,H]`
3. **NOR dump needed first — YES.** To target the GTX2 you need the retail **partition layout** (app offset/size), the retail **`mbrec`/`param`/`nvram` (calibration)** and **resource partitions** to keep, and the board config the retail app actually uses. That all comes from a **teardown-L3 NOR dump**. Realistic recipe: dump → identify the app partition → build a custom app matching that layout/board → **programmer-flash just the app partition** (keep retail mbrec/res/nvram), with `golden.bin` as the restore net. `[S/I]`
4. **Secure boot.** If the mbrec-signing eFuse is burned (unknown), only *mbrec* is gated — the **app partition is CRC-only** (byte-map), so a custom **app** still flashes. Full-image replacement would need the signing question resolved. `[S/I,M]`

### 2.4 Step-by-step feasibility verdict
| Step | Feasible now? | Blocker |
|---|---|---|
| Build `zephyr.bin`/`app.bin` for `ats3085s4` EVB | **Yes** | just toolchain setup (Zephyr 2.7.0 + SDK 0.13.x) |
| Wrap it into a valid `FA EE EB DE` image | **Yes** (proven in Part 1 pipeline) | `word@0x2C` + device-accept still deferred for the BLE route |
| Boot it correctly on **retail GTX2** | **Feasible-with-major-gaps** | board bring-up (panel/touch/sensors/GPIO) + retail partition/calibration reuse → needs a NOR dump |
| Flash safely | **Yes, once teardown L3/L4 done** | external programmer + golden.bin restore (spare in hand) |

**Bottom line:** the software pipeline is **proven** (Part 1) and the SDK is a **real Zephyr 2.7 build** for an ATS3085S4 board. The distance to a GTX2-booting custom firmware is **not the container and not signing — it's board bring-up + retail partition/calibration reuse**, which is gated on a NOR dump (teardown L3) and a proven programmer recovery (L4). First sensible milestone once the spare is open: a minimal LVGL app that drives the retail panel, programmer-flashed to the app partition with `golden.bin` as the safety net.

---

## Part 3 — Raw-accel-over-BLE feasibility: binary-patch vs SDK rebuild (honest verdict, 2026-07-13)

**Question:** can an app-only mod expose the discrete **ST LIS2DH12** raw XYZ as a BLE characteristic/stream by **binary-patching** the Zephyr `app.bin`, or does it need an **SDK rebuild from source**?

**The sensor side is trivial; the plumbing is not.** Reading raw XYZ is datasheet-easy (LIS2DH12: OUT regs `0x28–0x2D`, 32-level FIFO, I²C/SPI; a mainline Zephyr `st,lis2dh` driver exists, and the app already reads it internally for steps/raise-wrist/sleep). The hard part is **getting those samples onto a BLE pipe** — and stock has **no** raw-accel path at all (capture-absence + architecture: there is no realtime/streaming channel — every live datum is a polled `0x0e` request/reply, and the only watch-originated pushes are the `0x10` control channel).

### 3.1 Binary-patching the app image — **NOT viable for this feature**
A binary patch can change constants/strings (Part 1b), NOP a check, or redirect an existing call. Exposing raw accel needs **new code + new state**: a periodic sampler (FIFO/OUT read or a hook into the step-counter's accel reads), a **new GATT characteristic** (attribute-table entry + CCCD + handle) *or* a new creek/bulk notify frame, notify buffers in RAM, and wiring into the BLE event loop / scheduler. That is hundreds of bytes of hand-assembled Thumb-2, free-space hunting, RAM allocation, and main-loop hooks — **beyond what patching can practically do**; fragile and unmaintainable even if attempted.
- **A lighter "hijack an existing stream" patch is also a dead end here:** the usual trick (redirect accel data into an existing realtime/notify frame) needs an existing stream to piggyback on — and captures confirm **there is none** (no realtime/streaming channel was ever observed on the wire). Nothing to repurpose.

### 3.2 SDK rebuild from source — **the realistic path**
Add the feature in C to the Actions LVGL Zephyr SDK (`lv_port_actions_technology`, Zephyr 2.7): a LIS2DH12 sampling work-item + a notify characteristic/frame + a periodic sender → build `app.bin` → repack into `FA EE EB DE` (proven, Part 1/1b) → flash (gated on #17).
- **The feature itself is small: ~1–3 engineer-days** for someone fluent in Zephyr + the BLE stack.
- **The real cost is producing a BOOTABLE image on the retail board** (per firmware-dfu.md §F): the SDK ships EVB/dev-watch targets, not the GTX2 `CB05-MTL`. You must port panel/touch/**sensor**/flash-geometry/GPIO and reuse the retail `mbrec`/`param`/`nvram`/resources — **weeks** of board bring-up.
- **Open unknown that can ~2× it:** whether the LIS2DH12 hangs off the **ATS3085** (app-only mod) or the **WTM2101** co-processor (its own firmware). If WTM2101-owned, you also touch that firmware or find the ATS3085↔WTM2101 IPC. Resolved only by the teardown/NOR-dump (#17).

### 3.3 Verdict (no overpromise)
**Feasible only via an SDK-rebuilt custom app — NOT by binary-patching.** No signing barrier (app path is CRC-only; keep stock `mbrec`). The cost is the **retail board port + the recovery gate**, not the accel feature: **days for the feature on a booting base, weeks for the board bring-up beneath it, +unknown for the WTM2101 question.** This is a real project, consistent with the earlier honest negatives (stock BLE exposes no raw accel; the platform is the cost — not the sensor). The pragmatic sequencing: prove recovery (#17) → stock re-flash → the Part-1b visible-marker POC (confirms a custom image *runs*) → *then* invest in the board port + accel feature. If raw motion is wanted sooner with zero risk, the stock proxies still stand (live HR `monitor` 0x0a; sport-session cadence/steps `sport` 0x15).

---

## Sources
- Tools: `scripts/ota_unpack.py`, `scripts/ota_repack.py` (implement `docs/firmware-ota-byte-map.md`).
- Stock image: `firmware/cb05_yhzn01_v1.0.3_20241218_02.ota`. Patched output + intermediates: `firmware/` (gitignored).
- SDK: <https://github.com/lvgl/lv_port_actions_technology> — `action_technology_sdk/{zephyr/VERSION,.west/config, application/app_demo/lvgl_demo, boards/…/ats3085s4_dev_watch_ext_nor, zephyr/tools/build_ota_image.py, build_boot_image.py}`.
- Container + recovery specifics: `docs/firmware-ota-byte-map.md`, `docs/hardware-teardown-guide.md`.
