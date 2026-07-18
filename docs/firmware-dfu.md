# GTX2 firmware: DFU-over-BLE protocol, image format, custom-build & recovery

**Status: DOCUMENT-ONLY.** Nothing here was flashed, written, or sent to any watch. This file
reverse-engineers the flashing protocol from a captured session + the retail image, and assesses
the feasibility and **risk** of custom firmware. No flashing is endorsed; the closing verdict is
deliberately conservative.

## Evidence base
- Full DFU session capture (`btsnoop`, 11,678 ATT frames — the complete 2.1 MB transfer + handshakes).
- Exact image flashed in that session: `cb05_yhzn01_v1.0.3_20241218_02.ota` (2,148,064 B, sha256 `5dac41…7cc0`).
- Re-runnable analyzers over the capture (`analyze*.py` + `att.tsv`).
- Web / FCC research (cited inline): recovery + boot chain + image format / signing. Every claim is tagged SOURCED / EMPIRICAL / INFERRED with confidence.

Every wire-level and image-level claim was reproduced against the capture or image bytes; the
verifying computation is named inline so it can be re-checked.

---

## Corrections to the original brief (verified)
- **Application SoC = Actions `ATS3085S4`, NOT `UC6228CI`.** The BLE bind descriptor's "chipset" field reports `UC6228CI`, but that is the **Unicore UC6228 GNSS/GPS L1 receiver** (a peripheral), per the istarmax vendor BOM (`"Maincontroller: ATS3085S4"` + `"GPS: UC6228CI (L1)"`, cross-checked against the FCC-ID filings). The flashable application processor is unambiguously the **ATS3085S4** (Zephyr/LVGL, `FA EE EB DE` OTA, creek BLE, BROM/ADFU, `ats3085s4`/`cb05` board all belong to it; `UC6228CI` has zero firmware footprint). **CFW targets the ATS3085S4.** (Detail + reconciliation: `docs/hardware-teardown-guide.md §1.2`.)
- **CPU core = ARM Cortex-M33 (ARMv8-M), NOT Cortex-M4F.** Source: SDK `soc/arm/actions/leopard/Kconfig.series` → `select CPU_CORTEX_M33` / `ARMV8_M_DSP` / `CPU_HAS_FPU` / `HAS_SWO` (`recovery-research notes`; corroborated by the PSA listing referencing TF-M v1.4.0, which only runs on ARMv8-M). An M33+FPU+DSP is functionally M4F-like — the likely source of the confusion; the audio "CEVA DSP" is a separate core.
- **SoC SDK codename = "leopard"** (the ATS3085 family); board ports are named `ats3085*`/`ats3089*`.
- **`dipcore/unpack_clock_res` is watch-face-only** (the `.res` resource format, 466×466 display) — **not** the firmware/OTA container. Do not treat it as the image-format reference.

---

## A. Transport & channels
- **GATT = custom command service `0x0FF0`** (`00000ff0-0000-1000-8000-00805f9b34fb`; **not** Nordic UART). App→watch on write char `0x0001` (`00000001-…`, props 0x0c) at ATT handle **0x0026** (Write Command, ATT op 0x52); watch→app on notify char `0x0002` (`00000002-…`, props 0x10) at **0x0028** (Notification, op 0x1b; CCCD 0x0029). The `0xD2` bulk/DFU plane rides this **same write char `0x0001` (handle 0x0026)** as C1 — verified: 10k+ `0xD2` writes on 0x0026 in the capture (see §A channel census). A secondary custom service `0xFFD0` (handles 0x001b–0x0023) appears in GATT discovery but carried **no observed traffic** — purpose undetermined.
- **Two logical planes, discriminated by the first payload byte:**
  - `0xC1`/`0xC3` = **control plane** — protobuf commands (device info, time, notifications, settings, weather, watch-face list); `0xC3` = continuation of a >255 B `0xC1`.
  - `0xD1`/`0xD2`/`0xD3`/`0xD4` = **bulk plane** — a generic file-transfer sub-protocol.
- **Firmware update rides entirely on the bulk plane; there is NO dedicated OTA opcode in the control plane.** Control opcodes seen across the whole session: `01,02,03,04,05,0e,11,12,13,16,22` (+`0x10` push) — none starts OTA. Firmware is just another bulk file, distinguished only by its **filename `res.ota`**.

Channel-byte census (`analyze.py`):

| dir | d2 | c1 | d1 | c3 | d4 | d3 |
|---|---|---|---|---|---|---|
| app→watch (0x0026) | 10214 | 342 | 36 | 13 | 3 | 1 |
| watch→app (0x0028) | 649 | 357 | 36 | 21 | 3 | 1 |

- **MTU:** chunk writes are 236-byte ATT values (§B) ⇒ the app negotiated **ATT_MTU ≥ ~239** (typically 247). A re-implementation must raise MTU before streaming.

---

## B. DFU-over-BLE protocol (the `res.ota` bulk transfer)

**Core finding: the "firmware DFU" is the ordinary bulk-file push — the same channel used to send GNSS assistance data and watch-face resources — invoked with the magic filename `res.ota`, whose bytes are the whole `FA EE EB DE` image.**

Proof (`analyze.py`): treating the app→watch `0xD2` stream as sequential 234-byte chunks, the run
**starting at chunk index 1034 reconstructs the 2,148,064-byte image byte-for-byte — 9180/9180
match, `firstbad=None`.** Chunk #0 of that run (frame 7435) = `d2 00 faeeebde23bbe6e60cc72000…` =
exactly `image[0:20]`. Chunks 0–1033 are *earlier* bulk files (`ephemeris.gnss` 4,764 B and
`offEphemeris.agnss` 85,059 B — GNSS AGPS almanac).

### B.1 Bulk-plane frame types (little-endian)

| Type | Direction / layout | Meaning |
|---|---|---|
| **D3** query | A→W `d3 00`; W→A `d3 00 00 <u32 staged_off> <u32 ?>` | resume/state probe (here `off=0` ⇒ fresh). Enables resume. |
| **D1** announce | A→W `d1 00 <u32 size> <u32 size> 0x0f <name>\0` | start-of-file: name + length. Watch acks `d1 00 00`. |
| **D2** data | A→W `d2 <ctr:1> <payload ≤234>` | raw file bytes. Full frame 236 B; final 2+178 B. |
| **D2** ack | W→A `d2 00 00 <u32 offset> <u32 crc>` | windowed progress every **15 chunks (3510 B)**: cumulative offset + running CRC-16/XMODEM. |
| **D4** end | A→W `d4 00 00 <u32 crc16>`; W→A `d4 00 00` | finalize: whole-file CRC-16/XMODEM; watch verifies, acks, then applies. |

Field notes:
- **D1**: both u32 = total file size here (`0x20C6E0`=2,148,064 for `res.ota`; `0x129C`=4,764 for `ephemeris.gnss`). The duplicate field is unresolved (candidate `start_offset`/`total_size` for resumed transfers — only a from-scratch push was captured). Byte `0x0f` after the sizes is a constant/flag (same for every file), **not** a name length; the name is NUL-terminated.
- **D3 is an OPTIONAL resume/state QUERY — the transfer is initiated by D1, not D3** (verified in the capture): the 30+ AGPS/`ephemeris`/`offEphemeris` pushes **all start with D1 and send no D3**; D3 appears **exactly once**, immediately before the *resumable* `res.ota` firmware transfer (frame 7408 `d300` → reply 7416 `d3 00 00 00000000 00000000` = `staged_off=0`,fresh → D1 at 7425, ~3 s later). ⇒ D3 reads staged state and announces nothing, so it is **effectively non-arming** — a stand-alone `d300` elicits a state reply and initiates no transfer (and even a worst-case "expecting transfer" flag can't commit anything without a full D1/D2/D4 + reboot clears it). This is what makes a **read-only `--probe`** (send D3, print the reply, disconnect) safe on a spare. The D3 reply is a **raw D-plane PDU** on the notify char (not a C1 frame) — a re-implementation must tap raw notifications whose first byte ∈ {D1..D4}, not route them through the C1 reassembler.
- **D2 counter**: 1 byte, **resets to `0x00` at each file start** (verified at the `res.ota` first chunk), increments, wraps at 256. It's a sanity aid, not the authoritative position — position comes from stream order, confirmed by the watch's D2-ack `offset`. (The AGPS phase shows counter regressions = retransmits; `res.ota` is one clean pass.)
- **D2 ack CRC = running CRC-16/XMODEM of `image[0:offset]`.** Verified (`analyze3.py`): `3510→0xD1F8`, `7020→0xB037`, `10530→0x59BE`, `2144610→0x9670`, `2148064→0xAA0D`. The watch integrity-checks continuously, one window behind.
- **D4 CRC = CRC-16/XMODEM** (poly `0x1021`, init `0x0000`, no reflection) over the **entire file**. Verified: `crc16xmodem(image)==0xAA0D` = the D4 frame `d4 00 00 0daa0000` = the final D2-ack CRC. This is the only whole-file transport check — a 16-bit **integrity** check, not authenticity.

### B.2 Captured start & finish (frame-cited, `att.tsv`)
```
f7408 t2870.31 A→W  d300                                  ; D3 resume query
f7416 t2870.42 W→A  d3 00 00 00000000 00000000            ; watch: 0 staged (fresh)
f7425 t2873.43 A→W  d1 00 e0c62000 e0c62000 0f "res.ota"  ; D1 announce, size 0x20C6E0 = 2,148,064
f7433 t2873.48 W→A  d1 0000                               ; announce ack
f7435 t2873.49 A→W  d2 00 faeeebde23bbe6e6…               ; chunk #0 = image[0:] header
   …9180 chunks, watch D2-acks every 15…
f20743 t2928.23 A→W  d2 ec …(178-byte tail)               ; last chunk
f20747 t2928.29 W→A  d2 00 00 e0c62000 0daa0000           ; final ack: off=2,148,064 crc=0xAA0D
f20748 t2928.30 A→W  d4 00 00 0daa0000                    ; end-of-file, whole-file CRC-16/XMODEM
f20750 t2928.38 W→A  d4 0000                              ; verify OK
   … ~28.5 s radio silence …                              ; watch APPLIES + REBOOTS (stage→verify→commit, §E)
f20992 t2957.00 A→W  c1 … op=0x01 device info             ; app reconnects, re-reads version, resyncs
```
**Apply/reboot is implicit:** after the D4 verify-OK the watch stages/commits and reboots on its
own; no explicit "reboot"/"activate" command is sent.

### B.3 DFU state machine (for a re-implementation / Gadgetbridge)
```
IDLE ─(have res.ota bytes; MTU≥~247)→ QUERY
QUERY:    → D3 (d3 00); ← D3 reply (staged_off)          # 0 ⇒ fresh; >0 ⇒ resume candidate
ANNOUNCE: → D1 (d1 00 <size> <size> 0f "res.ota\0"); ← D1 ack (d1 00 00)
STREAM (window = 15 chunks):
          for each 234-B chunk: → D2 (d2 <ctr> <payload>)
          every 15 chunks ← D2-ack(offset, runningCRC16X); verify offset advances &
              crc == CRC16X(image[0:offset]); pace writes to the watch's ack cadence
          (on mismatch/disconnect: reconnect → D3 query → resume from staged_off — inferred)
FINALIZE: → D4 (d4 00 00 <CRC16X(whole file)>); ← D4 ack (d4 00 00) == accepted
APPLY (implicit): watch stages→verifies→commits→reboots (~30 s), then reconnects on the command channel
DONE:     re-read device info (C1 op 0x01) to confirm the new version
```
Implementer notes: the transfer is **flow-controlled by the watch's D2-ack cadence** (15-chunk
windows) — pace writes to it, don't blast. The control plane is idle during the stream. Resume is
designed-in (D3 + staged offset) but only observed from-scratch.

---

## §29 — OTA FLASHER IMPLEMENTATION SPEC (offline RE; for the flasher)

> **HARD GATE:** everything here is buildable + testable **offline** (against captures + the OTA files). **No live watch flash** until #17's wired **backup + restore is proven** on the spare. The offline steps below need no watch.

**1. Transport (recap of §A — the flasher's channel).**
`0x0FF0` service · **write char `0x0001` @ ATT handle `0x0026`** (Write Command, ATT op `0x52`) · **notify char `0x0002` @ `0x0028`** (enable CCCD `0x0029`). **Negotiate ATT_MTU ≥ 247 before streaming** (chunk writes are 236-B ATT values). The `0xD2` bulk plane rides the **same write char** as `0xC1`. All bulk fields little-endian.

**2. Upload sequence (deliverable #1) — build to the §B state machine:**
`QUERY → ANNOUNCE → STREAM → FINALIZE → (implicit apply/reboot) → reconnect`
- **D3** `d3 00` → watch `d3 00 00 <u32 staged_off> <u32 ?>` (`staged_off=0` ⇒ fresh; `>0` ⇒ **resume** from there).
- **D1** `d1 00 <u32 size> <u32 size> 0f "<name>\0"` → ack `d1 00 00`. Firmware name = **`res.ota`**; `size` = whole `FA EE EB DE` file length.
- **D2** `d2 <ctr:1> <≤234 payload>`; `ctr` resets to 0 at file start, wraps at 256. Stream the file in 234-B chunks. **Pace to the watch's ack cadence** (don't blast).
- **D2-ack** (watch → app) every **15 chunks (3510 B)**: `d2 00 00 <u32 cum_offset> <u32 running_CRC16X>` — verify `cum_offset` advances and `running_CRC16X == CRC16/XMODEM(image[0:cum_offset])`.
- **D4** `d4 00 00 <u32 CRC16X(whole file)>` → ack `d4 00 00` = accepted. Then **~28 s silence** = watch stages→verifies→commits→reboots; reconnect and re-read `C1 op 0x01` to confirm version.
- **Resume-on-interrupt:** on disconnect/mismatch, reconnect → D3 → resume from `staged_off` (designed-in; only from-scratch was captured — exercise it on the spare).

**3. Validation gate (deliverable #3) — what the watch actually checks:**
| Layer | Check | Algorithm |
|---|---|---|
| Transport (per-chunk + final) | running + whole-file | **CRC-16/XMODEM** (poly 0x1021, init 0x0000) |
| Container outer | `payload_crc32@0x04` | zlib **CRC-32** over `file[0x2C:EOF]` |
| Container inner | `data_crc32@0x38` | zlib **CRC-32** over `file[0x6C:0x6C+data_len]` |
| Section magic | `magic@0x2C = 0xA578875A` | **fixed constant, NOT a checksum** (byte-map §4) |
| Compatibility | device/version accept | `os_ota` "does not support this ota-firmware(%d)" — a **device/version match**, *not* crypto |

- **No signature / secure-boot on the OTA/app path.** RSA/ECDSA/SHA absent; mbrec's optional BROM signature is off the app-reflash path (OTA never rewrites mbrec).
- **What makes stock OTA ACCEPT a modified app-only image:** a valid `FA EE EB DE` `res.ota` where (a) inner `data_crc32@0x38` and outer `payload_crc32@0x04` are recomputed, (b) **`magic@0x2C` left byte-identical** (it's a constant — resolved, byte-map §4, so **no device-accept test needed for it**), (c) the device/version-compat fields stay valid, (d) it streams with correct CRC-16, and (e) the app keeps stock `mbrec`/`param`/`recovery` (only `app.bin`+resources change). No signing key at any step.

**4. Safe first milestone (deliverable #4) — STOCK round-trip, zero image modification:**
The lowest-risk proof of the whole protocol is to push the **byte-identical stock image** and confirm a same-version reflash — no checksum work, no content change, and stock OTA is fail-safe (stage→verify→commit; can't touch mbrec/recovery). Validation ladder, **radio only at the last rung**:
1. **Offline A (done):** `ota_unpack`→`ota_repack` round-trip of the stock image is **byte-identical** (sha256 matches).
2. **Offline B (build this next):** the flasher emits its `D3/D1/D2*/D4` byte stream for the stock image and **asserts it against the captured `fw-complete` session** — `D1 == d1 00 e0c62000 e0c62000 0f "res.ota"\0`; `D2#0 == d2 00 faeeebde23bbe6e6…`; running-CRC checkpoints `3510→0xD1F8, 7020→0xB037, 10530→0x59BE`; `D4 == d4 00 00 0daa0000` (= `CRC16X(image)=0xAA0D`). This proves the flasher's wire output is byte-correct **with no watch**.
3. **LIVE (gated on #17 recovery proven + explicit JP go):** push the stock image to the **spare**, confirm same-version reflash + clean reconnect. Only after that: a modified app-only image (per §3).

---

## C. The `.ota` image format

Parsed directly from the bytes (`analyze` scripts + `xxd`), cross-checked against
`firmware-research notes`.

**Outer container = 44-byte (`0x2C`) header, then the payload to EOF:**

| off | value | meaning |
|---|---|---|
| 0x00 | `FA EE EB DE` | container magic |
| 0x04 | `0xE6E6BB23` | **CRC-32 of the payload — VERIFIED: `zlib.crc32(image[44:]) == 0xE6E6BB23`** (resolves the research doc's tentative `[INFERRED]` on this word) |
| 0x08 | `0x0020C70C` | size field (= file size + 0x2C) |
| 0x0C | `1` | section/entry count |
| 0x10 | `0x0FD9` | TOC/aux field |
| 0x14 | `"zephyr.bin\0"` | section name |
| 0x24 | `44` | payload offset |
| 0x28 | `0x20C6B4` = 2,148,020 | payload length |

`44 + 2,148,020 = 2,148,064` = exact file size (VERIFIED). The payload is **`firmware/zephyr.bin`**,
a **cleartext, unencrypted** Zephyr+LVGL build (strings: `*** Booting Zephyr OS build %s ***`,
`lvgl_res_loader_init`, `res_preload`, and tokens `mbrec`, `recovery`, `sdfs`, `app.bin`,
`fonts.bin`, `res.bin`). It carries an **inner Actions sub-header** (starts `0xA578875A`; inner
length `0x20A1F4`=2,138,612 appears twice; `0xCC` fill) — Actions nests its own checksummed
sub-image. This image has `count=1`.

**Integrity vs authenticity — decisive for custom firmware:**
- **Retail magic `FA EE EB DE` is a DISTINCT container from the open SDK's `AOTA`** (`0x41544F41`). The retail file contains zero `AOTA`/`ota.xml` strings. (`firmware-research notes` "format zoo": retail `FA EE EB DE`, SDK `AOTA`, `.fw` debug `ACTTEST0`, config-tool `upgrade.fw`, watch-face `.res` — **every Actions container in evidence is checksum/obfuscation only; no RSA/ECDSA/signed-hash anywhere**.)
- **Not mcuboot:** magic `0x96F3B83D` absent from the payload; no mcuboot TLV/signature area.
- **No trailing signature block:** the image tail is zero/`0xCC` padding (last 256 B have only 66 distinct byte values — padding, not the high-entropy blob a 64–256 B signature would be). The 44-byte outer header also has no room for one.
- On-device OTA framework verification (SDK `framework/ota/libota/{ota_image.c,ota_upgrade.c}`, SOURCED) is **CRC-32 only** — `utils_crc32(head+8)` vs header checksum, `utils_crc32(file)` vs per-file dir checksum, re-verify written partition vs checksum. **No SHA/RSA/ECDSA in the OTA path.** The `mbrec` boot image uses a plain 32-bit additive sum (not even a CRC); its MCUboot-*styled* TLV trailer carries only config TLVs (BOOTINI/NANDID), no crypto TLV.

⇒ **Integrity is CRC/checksum-only; the payload is cleartext; nothing observed is cryptographically
signed on the OTA/DFU surface.** A modified `zephyr.bin` re-wrapped with corrected checksums (outer
CRC-32 `0x04`, transport CRC-16/XMODEM D4) would pass the transport and OTA-framework checks. No key
is needed to repack. (First-stage BROM→mbrec *can* optionally check a signature — see §E — but OTA
never rewrites mbrec, so that gate is not on the app-reflash path.)

---

## D. Custom-firmware feasibility — **FEASIBLE, with major gaps**

Source: `firmware-research notes` §7 (SDK, SOURCED) + the image carve above.

- **The SDK is real and buildable.** `github.com/lvgl/lv_port_actions_technology` vendors a complete `action_technology_sdk`: **Zephyr v2.7.0**, `west`-based, `build.sh`; a full smartwatch app `application/bt_watch` (LVGL UI, BLE, OTA, sensors, watch-faces). ATS3085 board targets exist (`ats3085s_dev_watch_ext_nor`, `ats3085e…`, `ats3085s4…`; `bt_watch` ships `ats3089*` "leopard" boards). Display 466×466 MIPI-DSI ARGB8888, VGLite GPU, 8 MB NOR + 32 MB PSRAM. Build output = Zephyr `app.bin` → packaged by `build_ota_image.py` into an **`AOTA`** image; `mbrec` via `build_boot_image.py`.
- **Gaps that block a from-SDK image from booting on retail hardware:**
  1. **Container mismatch** — SDK emits **`AOTA`**; retail DFU consumes **`FA EE EB DE`**. The public SDK does not produce the retail container, so the vendor repacker must be reversed. **Tractable** (CRC-32 + cleartext, no key), but not provided. Offset/length carve is done (§C); the exact checksum ranges of the inner Actions sub-header still need confirming.
  2. **No retail board port** — the SDK ships EVB/dev-watch targets, not the GTX2/`cb05` board. Retail display panel init, touch controller, sensor stack (HR/SpO2/accel/baro/mag), button/vibra map, and flash geometry all differ. The SDK README labels itself **Beta** and says to build for real HW you need an Actions board.
  3. **Vendor blobs / calibration** — retail `mbrec.bin`, `param.bin`, `nvram_factory` (per-unit calibration), and `res.bin`/`fonts.bin`/`sdfs*` resource partitions are device-specific; a clean SDK build won't reproduce them.
- **Pragmatic path (app-only reflash):** keep the retail `mbrec`/`param`/`nvram`/resources; replace **only `app.bin` (the SYSTEM partition)** via the CRC-gated stock OTA path, after (a) reversing the `FA EE EB DE` repacker and (b) porting the retail board's drivers into a `bt_watch` build. **No signing key is required at any step.** This aligns with how stock OTA already works: it touches only `app.bin` + resources, never the bootloader.

---

## E. RECOVERY / UNBRICK + RISK — the gating section

Source: `recovery-research notes` (boot chain SOURCED from SDK; FCC exhibits visually
inspected).

### E.1 Boot chain is fail-safe by design
`BROM (on-chip mask ROM, immutable) → mbrec (boot record in NOR @0x0) → app / recovery-app.`
Partition map (`boards/arm/ats3089_dev_watch/firmware.xml`, SOURCED):

| addr | partition | file | `enable_ota` |
|---|---|---|---|
| 0x0 | BOOT | `mbrec.bin` | **false** |
| 0x1000 | SYS_PARAM | `param.bin` | **false** |
| 0x4000 | **RECOVERY** | `recovery.bin` | **false** |
| 0x44000 | **SYSTEM** | `app.bin` | **TRUE** (`ota_embed=TEMP`) |
| 0x1db000 | DATA | `sdfs.bin` (resources) | **TRUE** (`ota_embed=TEMP`) |

Implications (all SOURCED, HIGH):
1. **BLE OTA can only rewrite `app.bin` + resources.** `mbrec`, `param`, and `RECOVERY` are `enable_ota=false` — they **survive a bad BLE flash**. (They are `enable_dfu=true`, i.e. only the factory ADFU/production tool can rewrite them.)
2. **Stage → verify → commit.** OTA stages the new app to a **TEMP** region; on reboot the recovery app (`ota_recovery/recovery_main.c`) checks the `REC_OTA_FLAG`, verifies the staged image (`ota_upgrade_check` → `ota_upgrade_verify_along`, CRC-32), then commits and reboots to SYSTEM. An interrupted/failed flash leaves the old app or is re-applied. Boot/param have a `mirror_id 1` A/B duplicate.

### E.2 Silicon-level recovery exists — but is not user-reachable
- **Immutable BROM download modes.** `soc/arm/actions/leopard/brom_interface.h` (SOURCED) exposes ROM launchers at fixed address `0x188`: `p_adfu_launcher` (**USB/ADFU download**), `p_brom_uart_launcher` (**UART download**), plus `p_mbrc_brec_data_check(buf, digital_sign)` and `BOOT_TYPE_{USB,UART,EFUSE}`. These run even when NOR flash is blank/corrupt (mask-ROM). Entry: a hardware **key/GPIO at power-on** (`board.h`: `{KEY_ADFU, 0x05}`; `bootloader.ini [adfu config]`), or `dbg reboot adfu` over UART. Host tool = the Windows **Actions "USB Production Tool"/config-tool**; ADFU enumerates USB `10d6:10d6` (sibling-SoC evidence: `96boards-bubblegum/linaro-adfu-tool`).
- **UART_0 @ 2,000,000 baud** is the primary debug/console + the ROM UART-download transport. **SWD/SWO present** on the M33 (`HAS_SWO`); a `[jtag config]` block exists (likely disabled by default, `jtag_groud=0xff` — INFERRED).

### E.3 THE DECISIVE FINDING — no user recovery cable
- **The GTX2 charges via a 2-pin magnetic pogo cable = VBUS + GND, power only, NO USB data lines.** Verified in the FCC **External Photos** exhibit (2ASAU-GTX2, Fig. 9: exactly two gold pogo contacts; Fig. 1: 2-pin magnetic charging pucks). Firmware corroborates: `board_cfg.h` routes **no USB pins**. The ATS3085 *has* a USB-FS controller and the ROM supports USB ADFU, but **those USB lines are not brought out to the charger** — they exist only as internal die/PCB pads.
- **⇒ There is NO cable-based recovery for an end user.** The BROM ADFU (USB) / UART / SWD paths are reachable **only by opening the watch and soldering/probing internal test pads**, with the Actions Windows tool. FCC internal photos show battery pads (`B+/B-`, `A+/A-`) and assorted gold pads (possible test points) but **no labeled/broken-out SWD/UART debug header**; whether UART0/SWD/USB-D± are on solderable pads is **not determinable from the photos** (would require the physical board — off the main BLE task; spare-unit teardown is now tracked in the reopened CFW investigation, #17).

### E.4 Secure-boot posture (does not block app reflash)
- **PSA Certified = Level 1 only** (self-assessed questionnaire; cert `0632793519836-10100`, refs TF-M v1.4.0). **PSA L1 does NOT attest hardware-enforced secure boot or signed-image enforcement** (that is L2/L3). So it is *not* evidence of signing enforcement — if anything, consistent with its absence.
- BROM *can* verify a signature on `mbrec` (`digital_sign` flag; `BOOT_TYPE_EFUSE` exists), but whether that eFuse is provisioned on the retail GTX2 is **unknown from open sources**, the SDK ships an **unsigned** `mbrec`, and — decisively — **OTA never rewrites `mbrec`; it replaces only the CRC-32-checked `app.bin`/resources.** So even a fused secure-boot would not block an app-only custom reflash.

### E.5 Community reality
The ATS3085S hobbyist scene is **resource-level, app-mediated, BLE-only** (watch-faces via
`dipcore/unpack_clock_res`; offline maps via `purrrock/dtg1-map-tools`). **No public brick-and-recover
report for an ATS3085/ATS3085S watch exists** — there is **no community-proven unbrick playbook** to
lean on.

---

## F. Raw-accelerometer-over-BLE — the specific gap & effort estimate

> **Status (2026-07): CFW track REOPENED** as [issue #17](https://github.com/jphein/starmax-gtx2/issues/17)
> (feasibility + recovery-path + expose raw accelerometer), with spare-unit teardown (formerly
> issue #9) folded in as its hardware path. Still brick-risky and off the main BLE task, but no
> longer closed — this section is the investigation's starting point.

This is the objective the custom-firmware track exists to serve. Three layered facts:

### F.1 Stock BLE does NOT expose raw XYZ — confirmed by capture-absence + architecture `[CAP,H]`
- No raw-accelerometer frame ever appears on the wire in any capture — the watch never pushes sensor samples.
- The watch has **no realtime/streaming channel at all**: every live datum is a polled request/reply or a batch `healthSync` (`0x0e`); the only watch-originated pushes are the `0x10` control channel (media / find-phone), never sensor data.
- ⇒ The accel is used **internally** (steps, raise-wrist, sleep). No stock config flips a raw stream on. Exposing raw XYZ is a **firmware change**, full stop.

### F.2 The hardware side is nearly free — the accel is a standard, documented part `[S,H]`
The accelerometer is a **discrete ST `LIS2DH12`** (vendor BOM): 3-axis 12-bit nano-MEMS on I²C/SPI, **32-level FIFO**, raw output regs **`0x28–0x2D`** (OUT_X/Y/Z_L/H), ODR ≤5.376 kHz, watermark IRQ on INT1. A **mainline Zephyr `st,lis2dh` driver already reads it.** So "obtain raw XYZ on-device" is datasheet-trivial — read the FIFO/OUT registers.

### F.3 What the mod actually is, and what it costs
On the **ATS3085S4 `bt_watch` app** you would: (a) tap the LIS2DH12 FIFO/OUT read path (a driver likely already exists), (b) define a new transport for the samples — a new creek CmdId + protobuf message, or a dedicated bulk/notify frame, (c) push batches on the `0x0FF0` **notify char `0x0002`** (handle 0x0028), (d) add a matching decoder in the standalone client. **This is a rebuild, not a byte-patch** — you cannot graft a poll-loop + BLE notify path into a stripped Zephyr binary surgically.

| Work item | Effort | Gated on |
|---|---|---|
| Retail board bring-up (panel/touch/sensors/GPIO/flash geometry) in a `bt_watch` build | **weeks** | NOR dump (teardown L3) |
| Reverse `FA EE EB DE` repacker fully (or use direct-programmer flash) | days | `checksum_A@0x2C` (BLE route only) |
| Prove a recovery path (dump `golden.bin`, prove restore) | days–weeks | **teardown + spare (§E; CFW track #17)** |
| Determine which MCU masters the LIS2DH12 bus (ATS3085 vs WTM2101) | hours–days | NOR dump / on-device probe |
| **The raw-accel feature itself** (tap driver + notify frame + client decoder), on a *booting* custom base | **~1–3 engineer-days** (Zephyr+creek fluent); **×2 if WTM2101-owned** | a booting custom app on retail HW |

**Net:** the *feature* is small and the *sensor* is trivial; the cost and risk are entirely in the **platform beneath it** — board bring-up + a proven recovery path, which are gated on §E. The one genuine open unknown that swings feature effort ~2× is **whether the LIS2DH12 hangs off the ATS3085 (app-only mod) or the WTM2101 co-processor** (which has its own firmware) — resolved only by a NOR dump / on-device probe (teardown L1–L3).

### F.4 Non-CFW alternative (recommended first, zero brick risk)
If the real want is "live motion," stock BLE already exposes proxies: **live HR** (`monitor` 0x0a), **sport-session cadence/steps/pace** while a workout runs (`sport` 0x15 polled params), and daily step totals. Reserve CFW for the case where **raw XYZ specifically** is essential.

---

## VERDICT — is BLE flashing ever safe to attempt, and under what preconditions?

**Flashing STOCK vendor images over BLE (what the Runmefit app does): low risk / safe.** The stock
OTA framework is fail-safe: whole-file CRC-16/XMODEM at the transport (verified 0xAA0D), CRC-32 in
the container (verified 0xE6E6BB23), stage-to-TEMP → verify → recovery-commit, and a bootloader +
recovery partition that BLE OTA cannot touch. A dropped connection or bad transfer reverts.

**Flashing CUSTOM firmware over BLE: conditionally attemptable, but with a hard brick cliff.** It is
*technically* feasible because integrity is CRC-only, the payload is cleartext, and OTA replaces only
the CRC-gated `app.bin`/resources with no signing key required. It is safe to attempt **only if ALL**
of these hold:

1. **Delivered strictly through the stock Actions OTA framework** — a valid `FA EE EB DE` `res.ota` that touches only `app.bin` (SYSTEM) + resources, so the stage→verify→recovery-commit safety net and the untouched `mbrec`/`param`/`RECOVERY` remain in force. (Never bypass the framework to write the app partition directly.)
2. **The custom app MUST re-advertise BLE and re-accept OTA immediately after boot** — because BLE is the **only** recovery channel available to anyone who will not open the case. An app that boot-loops or fails to bring up BLE strands the device.
3. **The `FA EE EB DE` repacker is correctly reversed** (outer CRC-32 + inner Actions sub-header checksums) so the image passes verification, **and** the retail board's drivers (display/touch/sensors/flash) are ported so the app actually boots — a build that flashes cleanly but doesn't boot is still a brick.
4. **The flasher accepts that any mistake likely requires a hardware teardown to recover.** The charger is **2-pin power-only** — there is **no cable recovery**. If BLE never comes back, the only way back is opening the watch and probing internal test pads (UART0 @2 Mbaud / SWD / USB-D±) with the Actions Windows tool — invasive, uncertain (pad exposure unconfirmed; secure-boot-eFuse status unknown), with no community-proven playbook.

**Bottom line (document-only recommendation):** Do **not** attempt custom-firmware BLE flashing on
this unit until, at minimum, (i) a working `FA EE EB DE` repacker is proven, (ii) a test-pad recovery
path is validated on a **sacrificial** unit (teardown — now tracked as the reopened CFW investigation #17 on a spare unit, not the main BLE task),
and (iii) the candidate app guarantees BLE + OTA re-entry on boot. Absent (ii), treat every BLE
custom flash as **effectively one-way**. Stock re-flash via the vendor app remains the safe fallback
for a still-alive device.

---

## GO / NO-GO — the recovery gate (synthesis verdict)

**Is there a *proven* un-brick path today? NO — therefore custom-firmware flashing is NO-GO.**

- ✅ **Stock BLE OTA is fail-safe** (verified at source: `firmware.xml` → only `fw0_sys`/`fw0_sdfs` are `enable_ota=true`; `mbrec`/`param`/`recovery` are `enable_ota=false`; `recovery_main.c` → stage-to-TEMP → `REC_OTA_FLAG=="yes"` gate → `ota_upgrade_check` → reboot-to-system). A bad *stock* flash reverts. **GO for stock re-flash.**
- 🔴 **No user-recoverable path for a bad *custom* flash.** The charger is 2-pin power-only (no USB data), so the only non-invasive channel is BLE — which vanishes if a custom app fails to re-advertise. The recovery paths that *do* exist (external SPI programmer on the leadless WSON-8 NOR with a `golden.bin` restore; ADFU-over-UART on internal pads) are sound **in principle** but **UNPROVEN on this exact unit** (pad exposure, secure-boot eFuse state, and reflash acceptance all unverified) and require a **teardown (CFW track #17) + a spare**, with **no community playbook**.
- **Verdict:** the gate defined for this track — *a demonstrated recovery* — is **NOT met by research**. It can only be closed by physical work on a **spare** (teardown ladder L1→L4 in `hardware-teardown-guide.md`: teardown → pad-probe → NOR dump → **prove restore**), tracked in gadgetbridge issue #9. **Only after L4 is proven + explicit JP go/no-go should any L5 custom flash be considered.** Until then: **NO-GO on flashing; stay on stock; pursue the non-CFW live-motion proxies (§F.4) for the immediate want.**
