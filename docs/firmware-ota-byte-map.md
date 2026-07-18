# FA EE EB DE OTA container — byte-map & repack spec

**Date:** 2026-07-11 · **Mode:** read-only (file analysed with Python; nothing flashed/modified)
**Sample:** `firmware/cb05_yhzn01_v1.0.3_20241218_02.ota` — size **2,148,064 = 0x20C6E0**. **Only one firmware version is published for this device** (v1.0.3; see §8), so single-sample formulae below cannot yet be diff-hardened.
**Method:** `work/firmware-research/{crc_probe,inner_probe,word2c_probe}.py` (re-runnable). All multi-byte fields **little-endian**.
**Legend:** `[C]` CONFIRMED (CRC brute-reproduced / arithmetic-exact) · `[E]` empirical single-sample · `[?]` unresolved

## TL;DR for repack
- **Outer integrity gate:** `crc32@0x04 = zlib.crc32(file[0x2C:EOF])` → reproduces stored `0xE6E6BB23`. **[C]**
- **Inner image gate:** `crc32@0x38 = zlib.crc32(file[0x6C : 0x6C+0x20A1F4])` → reproduces stored `0xE78749BE`. **[C]**
- Both are stock **CRC-32/ISO-HDLC (zlib)**. **No signature, no hash, no encryption at any layer** — repack needs no key.
- `word@0x2C = 0xA578875A` is **RESOLVED (§4): a fixed Actions sub-image MAGIC, not a checksum** (it appears verbatim as a code literal at file `0x15DDF4`; no CRC/sum reproduces it). ⇒ a repack **leaves `@0x2C` byte-identical**; only `@0x38` (inner CRC) and `@0x04` (outer CRC) are recomputed. There is **no** residual unknown gating a deterministic repack, and **no** signature anywhere.

---

## 1. Layering overview
```
OUTER "FA EE EB DE" container (44-byte header @0x00, one TOC entry "zephyr.bin" = whole payload)
 └─ payload [0x2C .. EOF]  (opaque to outer; CRC32 @0x04 covers ALL of it)
     └─ INNER vendor multi-section image (its own mini-directory):
         ├─ section 0: "firmware/zephyr.bin"  desc@0x2C (64B) + data@0x6C..0x20A260  (the whole RTOS: mbrec+recovery+app+…)
         └─ section 1: "sdfs.bin"             dir-entry@0x20A260 (32B) + data (~9 KB tail)
```
Outer `file_count=1`; the multi-section structure lives one layer down. `app.bin` (the Zephyr app) is a sub-region **inside section 0's data** (token scan located `mbrec`/`app.bin`/`recovery` within `0x6C..0x20A260`).

## 2. OUTER container header — bytes `0x00–0x2B` (44 bytes)
| Off | Sz | Field | Value (this image) | Meaning / status |
|----|----|-------|--------------------|------------------|
| 0x00 | 4 | `magic` | `FA EE EB DE` (LE `0xDEEBEEFA`) | container magic **[C]** |
| 0x04 | 4 | `payload_crc32` | `0xE6E6BB23` | **zlib.crc32(file[0x2C:EOF])** — reproduced exactly **[C]** |
| 0x08 | 4 | `total_size` | `0x0020C70C` | `= filesize + 0x2C` (`= off@0x24 + len@0x28 + 0x2C`). Size field, not a CRC **[E,1-sample]** |
| 0x0C | 4 | `file_count` | `0x00000001` | # of outer TOC entries **[E]** |
| 0x10 | 4 | `version/flags` | `0x00000FD9` | not a CRC (4057); likely version/build/flags — indexes into code region, not a checksum **[E]** |
| 0x14 | 16 | `name[16]` | `"zephyr.bin"` (NUL-padded) | TOC entry-0 name **[C]** |
| 0x24 | 4 | `offset` | `0x0000002C` (44) | payload start = end of this header **[C]** |
| 0x28 | 4 | `length` | `0x0020C6B4` (2,148,020) | payload length; `off+len = 0x20C6E0 = EOF` **[C]** |

`crc32@0x04` covers the payload **including** its 56 trailing zero bytes (declared length runs to EOF). Content’s last non-zero byte is at `0x20C6A7`.

## 3. INNER section 0 header — "firmware/zephyr.bin", bytes `0x2C–0x6B` (64 bytes)
| Off | Sz | Field | Value | Meaning / status |
|----|----|-------|-------|------------------|
| 0x2C | 4 | `magic` (sub-image) | `0xA578875A` | **section/sub-image MAGIC constant** — fixed, not a checksum (§4). Leave untouched on repack. `[C]` |
| 0x30 | 8 | `reserved` | `0` | zero |
| 0x38 | 4 | `data_crc32` | `0xE78749BE` | **zlib.crc32(file[0x6C : 0x6C+0x20A1F4])** — reproduced exactly **[C]** |
| 0x3C | 4 | `data_len` | `0x0020A1F4` (2,138,612) | inner image length **[C]** |
| 0x40 | 4 | `data_len_2` | `0x0020A1F4` | duplicate length (orig/load size) **[E]** |
| 0x44 | ~8 | `pad` | `00 CC CC CC CC CC CC CC` | `0x00` then `0xCC` filler **[E]** |
| 0x4C | 20 | `name` | `"firmware/zephyr.bin"` + NUL | section name (NUL/zero-padded to 0x6C) **[C]** |
| 0x6C | — | **DATA** | — | inner image begins here **[C]** |

## 4. `word@0x2C = 0xA578875A` — RESOLVED: it's a MAGIC, not a checksum  `[C]`
**2026-07-13:** the year-old "unresolved checksum" is resolved — **`0x2C` is an Actions section/sub-image MAGIC constant (`0xA578875A`), not a checksum of anything.** Three independent lines of evidence:
1. **It recurs verbatim inside the payload's CODE.** `0xA578875A` (LE `5a 87 78 a5`) appears at **exactly two** file offsets: `0x2C` (the section-descriptor magic slot) and **`0x15DDF4`**, which sits in a stream of ARM Thumb-2 instructions (`48xx/49xx/4axx` LDR, `9ef7…` BL, `4605` MOV) — i.e. it's a **literal constant compiled into the Zephyr firmware** (the value the OTA code writes/compares). A checksum computed *over* the payload can never also appear verbatim *inside* that payload's code.
2. **Header position = magic slot.** The section-0 descriptor is `[0x2C: 0xA578875A][0x30: 8 reserved zero bytes][0x38: data_crc32][0x3C/0x40: len][0x4C: name]` — a textbook `MAGIC · reserved · CRC · length · name` layout. `0x2C` is the head-of-descriptor magic; the *real* integrity field is `data_crc32@0x38`.
3. **No checksum reproduces it.** Confirmed by two independent sweeps (prior byte-map sweep + a fresh broad sweep: CRC-32 ISO-HDLC/BZIP2/MPEG-2/POSIX + adler32 over inner-data/header/whole-file ranges) — **zero matches**, exactly as expected for a magic.

**⇒ Implication for repack (decisive):** `0x2C` is a **fixed constant** — a modified app-only image **keeps `0x2C` byte-identical**; it does **not** need recomputation and does **not** gate acceptance of a content change. The only fields a repack recomputes are inner `data_crc32@0x38` and outer `payload_crc32@0x04` (which `ota_repack.py` already does; leaving `@0x2C` untouched is now *confirmed correct*, not a guess). **No device-accept test is needed to clear this field** — the earlier "DEFERRED device-accept test for `@0x2C`" is obsolete.

<details><summary>Historical: algorithms ruled out before the magic was identified (kept for the record)</summary>

**Offline checksum resolution had been exhausted** — none of the following reproduce it:
- **zlib-CRC32 AND Actions additive-sum** (`sum(u32)`, `0xFFFFFFFF-sum`) over: inner data `[0x6C:IDE]`, `[0x6C:content_end]`, `[0x6C:EOF]`; 64-B descriptor `[0x2C:0x6C]`/`[0x30:0x6C]`; descriptor+data `[0x2C:IDE]`; data+descriptor; self-zeroed descriptor+data (0x2C and/or 0x38 zeroed).
- Transforms of `data_crc32`: `~`, `^0xFFFFFFFF`.
- **String/identity hashes** (crc32): `"cb05_yhzn01"`, `"cb05_yhzn01_v1.0.3_20241218_02"`, `"cb05_yhzn01_v1.0.3"`, `"zephyr.bin"`, `"firmware/zephyr.bin"`, `"v1.0.3"`, `"1.0.3"`, `"20241218"`, `"cb05"`, `"yhzn01"`.
- **Composite ranges**: name+data, data+name, len[8]+data, name-field(0x4C:0x6C)+data.
- **Range sweeps**: inner-data END swept `IDE-256 … IDE+12000` (step 4); START swept `0x2C…0x6C` (step 4) with end=IDE. No hit.

A 2nd firmware sample (to diff varies-with-content vs fixed) was never obtainable (§8) — but the **code-literal + magic-slot evidence in the RESOLVED banner above settled it without a 2nd sample**: it's a fixed magic (the "candidate remaining: non-derived value" branch), not any checksum.

</details>

## 5. INNER section 1 — "sdfs.bin", 32-byte dir-entry @`0x20A260`
`name[12]="sdfs.bin"` · `+0x0C=0x00000002` (type?) · `+0x10=0x00002480` (size≈9 KB tail) · `+0x14=0` · `+0x18=0x49DE21BB` · `+0x1C=0x99C13D1A` (checksum-ish). **Different descriptor shape than section 0** (name-first, 32 B) — vendor quirk; resembles Actions `ota_dir_entry`. Not app-relevant; documented for completeness.

## 6. Repack recipe (modify app.bin → valid FA EE EB DE image)
`app.bin` is inside section-0 data (`0x6C..0x20A260`). After editing those bytes:
1. **Keep the total size identical** if possible (patch in place / re-pad) so offsets/lengths don’t shift. If size changes, update `data_len@0x3C`+`@0x40`, section-1 offset, `length@0x28`, and `total_size@0x08`.
2. Recompute **inner** `data_crc32@0x38 = zlib.crc32(file[0x6C : 0x6C+data_len])`; write LE.
3. **Leave `magic@0x2C` (`0xA578875A`) untouched** — it's a fixed sub-image magic, not a checksum (§4); a content change never alters it.
4. Recompute **outer** `payload_crc32@0x04 = zlib.crc32(file[0x2C : EOF])`; write LE. **(do this LAST — it covers @0x38 and @0x2C.)**
5. If filesize changed, set `total_size@0x08 = filesize + 0x2C`.
6. BLE DFU transport recomputes per-chunk **CRC-16/XMODEM** on the fly (not stored in the file).

Order matters: inner CRC (@0x38) → checksum_A (@0x2C) → outer CRC (@0x04), because the outer CRC covers the entire payload incl. both inner fields.

## 7. Confidence
- Two image-gating CRC-32 ranges: **CONFIRMED** (brute-reproduced). Container is **checksum-only, unsigned, unencrypted** — reinforces the DFU verdict.
- Field semantics (`total_size`, `version/flags`, `data_len_2`, section-1 layout): single-sample inference. **Cannot be diff-hardened — only v1.0.3 is published (§8).** Treat formulae as best-effort until a 2nd build appears.
- `checksum_A@0x2C`: **unresolved**, offline avenues exhausted (§4); the one gap before a fully deterministic blind repack. Resolvable only by a (deferred) device-accept test.

## 8. Second-sample investigation (2026-07-11) — READ-ONLY, no flash
Goal was to obtain a 2nd `cb05_yhzn01` OTA and diff it to classify `word@0x2C` and harden §2/§3/§5 formulae. **Result: no 2nd sample obtainable.**
- **Firmware update-check API response** (captured in `bugreport-fw-complete` / `-final` logcat) offers exactly one build:
  `{"version":"1.0.3","force_update":true,"bin_url":"https://www.runmefit.cn/storage/20241219/706156b895d12202c13aefbb1ac0b632_cb05_yhzn01_v1.0.3_20241218_02.ota"}` (title `AYAYHKJ`).
- That URL is **byte-identical** to our local copy (HTTP 200, `content-length: 2148064`, `last-modified: Thu, 19 Dec 2024`, `etag "67638b75-20c6e0"`).
- **No other `.ota` URL or `v1.0.x` token** appears in any of the 9 bugreport captures. Storage paths are MD5-hash-prefixed (`706156…_`), so alternate versions aren't guessable.
- **Dial CDN** `download.runmefitserver.com/dial/gtx2/gtx2/*.bin` are **ZIP archives** (`PK\x03\x04`, watchface resources) — *not* `FA EE EB DE` containers, so they can't classify `word@0x2C`. `YHZN_1021` → 404 HTML.
- Conclusion: `word@0x2C` + single-sample formulae stay open pending either a future v1.0.4+ release or the deferred device-accept test.

### 8.1 Corroborating watch-side OTA strings (from logcat, reinforce §7)
The bugreport logcat contains the watch's own Zephyr/`os_ota` console strings — all consistent with **checksum-only, no crypto signature**:
- `os_ota: Error***: firmware partition signature is 0x%04x` — a **16-bit** partition *magic/signature word* (not an RSA/ECDSA signature; only 4 hex digits).
- `<ota_fstream>: Error***: OTA package does not match current firmware` and `... The device(%d) does not support this ota-firmware(%d)!` — acceptance is a **device/version compatibility** check, not a cryptographic verify.
- `Firmware Version: 0x%08x`, `FVER` magic, `os_ota: generate firmware package information`, `os_ota: open firmware partition: start(0x%x) size(%d)`.
- Partition names seen: `firmware_cur`, `usrdata`/`usrdata2`, `syslog`, `filesystem`, `star_calibration`, `app_list_sort`, plus **`wtm2101_ota`** — a **secondary sensor-MCU (WTM2101)** with its own OTA partition (a separate co-processor image, distinct from the ATS3085 Zephyr app).

---

## 9. Repacker CLIs (`scripts/`)

The byte-map above is implemented as two committed, **stdlib-only** CLIs (`zlib`, `struct`, `json`):

- **`scripts/ota_unpack.py <image.ota> <outdir>`** — verifies both stored CRC-32s and splits the
  container for inspection: `manifest.json` (all fields + CRC stored-vs-computed + `checksum_A`
  flagged UNRESOLVED), the outer header (`00_outer_header.bin`), the section-0 header
  (`01_section0_header.bin`), the editable **`section0_firmware_zephyr.bin`** (the inner
  `[0x6C : 0x6C+data_len]` region = the "app payload"), and the `section1_sdfs.bin` tail.
- **`scripts/ota_repack.py <orig.ota> <new-app-payload> <out.ota>`** — splices a new section-0
  payload into the stock container and fixes the fields **in the correct order**: length fields
  (`@0x3C`,`@0x40`,`@0x28`,`@0x08`) → inner `data_crc32 @0x38 = zlib.crc32(out[0x6C:0x6C+len])`
  → outer `payload_crc32 @0x04 = zlib.crc32(out[0x2C:EOF])` **last**. **`checksum_A @0x2C` is left
  untouched** (§4). Self-consistency of both CRCs is re-verified on the written file.

> **DEFERRED / recovery-gated:** `ota_repack.py` prints a warning banner and produces images for a
> **deferred, recovery-gated device flash test only — do not flash without a proven unbrick path.**
> Whether the watch enforces `checksum_A @0x2C` is unknown until that (go/no-go) test.

### Round-trip self-test (verified 2026-07-11, system Python 3.12, stdlib only)
- **Byte-identical round-trip:** `ota_unpack` the stock `cb05_yhzn01_v1.0.3` image, then
  `ota_repack` it with the **unchanged** section-0 payload → output is **BYTE-IDENTICAL** to the
  input (**sha256 `5dac…67cc0` matches exactly**). This proves the CRC recomputation, field
  handling, and the untouched `@0x2C` exactly reproduce the vendor packer.
- **Modify path:** flipping one payload byte and repacking changes **exactly three regions** —
  outer CRC (`0x04–0x07`), inner CRC (`0x38–0x3B`), and the edited payload byte — with both CRCs
  self-consistent, `@0x2C` preserved, and size unchanged. (Size-changing repacks additionally
  patch the single-sample-inferred `@0x3C/@0x40/@0x28/@0x08` fields and warn that they're
  unvalidated until a device-accept test.)
