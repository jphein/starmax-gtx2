# GTX2 Custom-Firmware RECOVERY-GATE Runbook (R1 → R2 → R3)

**Purpose:** the exact, ordered, execute-live sequence to establish and *prove* a
recovery path on the **spare** GTX2 before any custom flash. This is the gate from
issue #31. **`RECOVERY_PROVEN` flips to TRUE only after R3 boots the spare from a
written-back `golden.bin`.** No custom flash (R4) until then.

**Prepared:** software-side, **no hardware attached yet** (JP is
tearing down). · **Basis:** `docs/hardware-teardown-guide.md` §0.5.2 (R1–R4) + §3;
SDK `board_cfg.h`/`firmware.xml`; XT25F128F datasheet; FCC 2ASAU-GTX2.
**The SWD configs referenced below ship in this repo at `tools/swd/`** (run the commands from there, or copy them out).

> **Safety.** 3.3 V board — **never feed 5 V to any pad or the NOR.** Opening voids
> water resistance. Plastic tools near the LiPo/FPCs; Kapton over the battery for any
> hot-air. Custom firmware on the **spare** only; daily driver stays stock.

---

## 0. Hardware & tooling — in hand vs. gaps

| Item | Status | Note |
|---|---|---|
| SWD probe (ST-Link and/or CMSIS-DAP) | ✅ JP has | CMSIS-DAP/J-Link preferred for a *generic* M33; ST-Link needs the dapdirect driver (below) |
| OpenOCD 0.12.0 | ✅ installed | generic `cortex_m` target via raw DAP — configs written |
| pyOCD 0.44.1 | ✅ installed (pipx: `~/.local/bin/pyocd`) | builtin `cortex_m` target, pack-less |
| CH341A programmer | ✅ JP has | **3.3 V I/O caveat — see R3 / Appendix B** |
| WSON-8 socket / hot-air | ⚠️ uncertain | needed for the CH341A path (flash is **leadless WSON-8**, a SOIC clip can't grip it) |
| **flashrom** | ❌ **NOT installed** | **install in R0** — required for the CH341A read/write |

### THE load-bearing constraint (read before planning the session)
- **SWD gives you READ, not WRITE, of the NOR.** The external NOR is memory-mapped
  for execute-in-place (XIP) at **`0x12000000`** (SDK `CONFIG_SPI_XIP_VADDR`), so a
  16 MiB `dump_image` over SWD is a real, easy `golden.bin` READ path (R2).
  **But** OpenOCD/pyOCD ship **no flash driver for the Actions SPIC controller**, and
  the XIP window is a read-only cache — **you cannot program the NOR through SWD out
  of the box.** A golden.bin *restore write* (R3) needs the **CH341A** (raw die
  write) or **ADFU** (or a to-be-built custom SWD flash-loader, not ready).
- **Consequence:** even if SWD is NOT fused, the **R3 restore-write path is the
  CH341A** (or ADFU). SWD's job is R1 (prove access) + a fast, non-invasive R2 read
  and cross-check. Plan the session so the WSON is accessible for the write regardless.

---

## R0 — PRE-FLIGHT (do now, no hardware needed)

1. **Install flashrom** (CH341A read/write):
   ```
   sudo apt-get install -y flashrom     # or: mise/brew — check flashrom --version >= 1.2
   ```
   Confirm CH341A support: `flashrom -p ch341a_spi 2>&1 | head` should NOT say "unknown
   programmer".
2. **The SWD configs ship in `tools/swd/`**:
   `openocd-common.cfg`, `openocd-cmsis-dap.cfg`, `openocd-stlink.cfg`, `pyocd.yaml`.
   Both OpenOCD configs parse-check clean on 0.12.0.
3. **Pick the off-device backup home** for `golden.bin`: at least **two** locations
   (e.g. `~/gtx2-golden/` + a second disk / `disks:`). 16 MiB × several copies — trivial.
4. **Print the pad map** (Appendix A) and the XT25F128F pinout (Appendix C).
5. **Know the expected values** so a live read is unambiguous:
   - M33 SCB CPUID `@0xE000ED00` → part id **`0xD21`** (e.g. `0x410FD213`/`0x411FD210`).
   - Arm M33 DP DPIDR ≈ **`0x6ba02477`** (exact value is fine as long as it *reads*).
   - NOR size **exactly 16 MiB = `0x1000000` = 16 777 216 bytes**.

---

## R1 — ESTABLISH WIRED ACCESS (prove SWD present & not fused)

**Goal:** read DPIDR/IDCODE + core CPUID and **halt** the M33. That proves SWD is
reachable and not fused off. Do this **connect-under-reset** so firmware can't
interfere. Pads: the Fig-13 red-arrow cluster NW of the SoC (SWDIO+SWCLK min, +GND,
+VREF/3V3, +nRESET if present) — see teardown guide §2.2.

**Wiring:** probe GND↔board GND, SWDIO↔SWDIO, SWCLK↔SWCLK, VREF/target-power sense↔3V3,
nRESET↔nRESET (if a reset pad exists). Power the board from battery/bench 3.3 V — the
probe does **not** power the watch.

### CMSIS-DAP (preferred)
```
cd tools/swd
openocd -s . -f openocd-cmsis-dap.cfg -c "init; r1_probe; shutdown"
```

### ST-Link (needs V2 fw ≥ V2J24, or V2-1/V3 — uses dapdirect, not HLA)
```
cd tools/swd
openocd -s . -f openocd-stlink.cfg -c "init; r1_probe; shutdown"
```

### pyOCD (either probe family)
```
cd tools/swd
pyocd list                                   # confirm the probe is seen
pyocd cmd -t cortex_m -M under-reset -f 1000000 \
    -c "status" -c "read32 0xE000ED00" -c "reg pc" -c "reg xpsr"
```

**Expected (PASS):**
- OpenOCD `init` prints a **DPIDR** line; `r1_probe` prints CPUID with **part `0xD21`**
  and halts (`target halted due to debug-request`).
- pyOCD logs `DP IDR = 0x...` on connect; `read32 0xE000ED00` shows part `0xD21`.

### ✅/❌ GO/NO-GO CHECKPOINT R1
- **PASS** → proceed to R2. You have a live debug link (also: console/RAM/flash-read).
- **FAIL** (no DPIDR / `No ACK` / DAP not found): SWD is fused or pads wrong.
  - Re-verify pad identity (SWDIO pull-up, SWCLK is a clock in) and try
    `reset_config none` + attach (edit `openocd-common.cfg`).
  - If still dead → **SWD is fused. Skip to R2 via the CH341A** (works regardless of
    any SoC lock — the NOR is external). SWD being fused does **not** block recovery.

---

## R2 — READ + BACK UP THE FULL STOCK FLASH → `golden.bin`

**Goal:** a trustworthy 16 MiB image of the stock NOR, off-device, multiple copies.
**Nothing else in this project matters if this image doesn't exist.**

Two independent read methods. Do **at least one**; do **both** if you can — the
cross-check is free brick-insurance.

### Path A — SWD via the XIP window (fast, non-invasive; needs R1 PASS)
The core must run **past mbrec** so the SPI0 XIP cache is configured — so **do NOT
hold reset** here. Read twice.
```
cd tools/swd
# read #1
openocd -s . -f openocd-cmsis-dap.cfg \
    -c "init; reset run" \
    -c "sleep 2000; dump_nor_xip golden_swd_1.bin; shutdown"
# read #2
openocd -s . -f openocd-cmsis-dap.cfg \
    -c "init; reset run" \
    -c "sleep 2000; dump_nor_xip golden_swd_2.bin; shutdown"
```
Sanity: `dump_nor_xip` prints the first 16 bytes at `0x12000000` — expect a real
mbrec header (NOT all `0xFF`/`0x00`).

### Path B — CH341A raw die read (authoritative; needs WSON access, §3.1)
Access the leadless WSON-8 by **chip-off + WSON-8→DIP socket** (primary) or
**flying-lead micro-solder** (alt) — a SOIC-8 clip **cannot** grip this part.
```
flashrom -p ch341a_spi -c XT25F128F -r golden_ch341a_1.bin
flashrom -p ch341a_spi -c XT25F128F -r golden_ch341a_2.bin
# if flashrom doesn't know XT25F128F, force a W25Q128-class 16 MiB profile:
#   flashrom -p ch341a_spi -c "W25Q128.V" -r golden_ch341a_1.bin
```
See **Appendix B** for the CH341A 3.3 V hazard — do not skip it.

### Verify discipline (BOTH paths)
```
cmp golden_swd_1.bin  golden_swd_2.bin      # must be identical
cmp golden_ch341a_1.bin golden_ch341a_2.bin # must be identical
stat -c %s golden_swd_1.bin                 # must be 16777216
sha256sum golden_*.bin
# If you have BOTH A and B: cmp golden_swd_1.bin golden_ch341a_1.bin
#   - identical  => no on-die encryption; XIP dump == raw. Either is golden.
#   - DIFFER     => SoC scrambles/encrypts the XIP view. The CH341A raw read is
#                   the ONLY valid golden.bin (the SWD-XIP image is decrypted and
#                   would NOT restore correctly). Use golden_ch341a_*.bin.
```
Then: **copy the verified image to `golden.bin` in ≥2 off-device locations.**

### ✅/❌ GO/NO-GO CHECKPOINT R2
- **PASS** iff: two identical reads (per method), size **exactly 16 777 216 bytes**,
  a plausible mbrec header at offset 0, and multiple off-device copies saved.
- **NO-GO**: reads differ run-to-run (bus noise / contention — slow the clock, hold
  the SoC in reset for flying-lead, reseat), or size ≠ 16 MiB. **Do not proceed.**

---

## R3 — PROVE RESTORE (the hinge: brick → re-flash)

**Goal:** write `golden.bin` back and confirm the spare **boots normally**. This is
what converts *"brick = dead spare"* into *"brick = re-flash golden."*

**Write path = CH341A (raw die write).** (SWD cannot program the NOR — see §0. ADFU is
the no-desolder alternative if the Actions tool works; the CH341A is the primary net.)

To make R3 a *real* test, first perturb the flash so a successful restore is
unambiguous — e.g. read-modify a benign byte, or erase+rewrite. Simplest honest test:
**erase then write golden.bin** (proves both erase and program, and that a
freshly-programmed die boots):
```
# (chip on the CH341A / WSON socket, 3.3 V — Appendix B)
flashrom -p ch341a_spi -c XT25F128F -E                 # full-chip erase
flashrom -p ch341a_spi -c XT25F128F -r blank.bin
python3 - <<'PY'                                       # confirm erase = all 0xFF
d=open("blank.bin","rb").read(); assert len(d)==16777216 and set(d)=={0xff}; print("ERASED OK")
PY
flashrom -p ch341a_spi -c XT25F128F -w golden.bin      # write-back (verifies by default)
flashrom -p ch341a_spi -c XT25F128F -v golden.bin      # explicit re-verify
```
Re-solder the WSON (if chip-off), reassemble enough to power, and **boot the spare.**

### ✅/❌ GO/NO-GO CHECKPOINT R3  → sets `RECOVERY_PROVEN`
- **PASS** iff: erase read back all-`0xFF`, write verified, and the spare **boots to a
  normal stock UI** (or, via UART console §2.1, prints the normal Zephyr boot banner
  and reaches app). → **`RECOVERY_PROVEN = TRUE`.** Recovery is proven; R4 is unlocked
  (still gated on explicit JP go/no-go).
- **NO-GO**: write won't verify (retry: slower clock, reseat, re-flux, fresh socket),
  or the spare does **not** boot after a verified write. **STOP.** Do not attempt R4 —
  investigate (partial write? wrong image? secure-boot mismatch?) before any custom flash.

---

## R4 — CUSTOM FLASH (OUT OF SCOPE HERE — gated)
Only after `RECOVERY_PROVEN = TRUE` **and** explicit JP go/no-go. Primary: write the
modified app region with the CH341A (app/SYSTEM @ `0x44000`, size `0x197000`; see
`firmware-ota-byte-map.md` for CRCs). On any failure: `flashrom -w golden.bin`. Not
executed by this runbook.

---

## Decision flow
```
R1 SWD probe ──PASS──► R2 read golden.bin (SWD XIP + / or CH341A) ──PASS──► R3 restore-write (CH341A) ──PASS──► RECOVERY_PROVEN ─(JP go)─► R4
     │                        │                                                    │
   FAIL (fused)          NO-GO (reads                                         NO-GO (no boot /
     │                    differ/≠16MiB)                                       no verify)
     ▼                        ▼                                                    ▼
 R2 via CH341A only        STOP + diagnose                                     STOP + diagnose
 (external NOR bypasses                                                        (never proceed to R4)
  any SoC lock)
```

---

## Appendix A — SWD pad hunt (teardown guide §2.2)
Tight cluster of 2–6 gold pads near the SoC / under-beside the display FPC: **SWDIO
(has pull-up), SWCLK (clock in)**, often + GND, VREF/3V3, nRESET, SWO. Start at the
**Fig-13 red-arrow cluster NW of the ATS3085.** Confirm SWDIO by its pull-up; SWCLK is
a driven clock. If a JTAGulator/SWD-scan is handy, scan the cluster first.

## Appendix B — CH341A 3.3 V hazard (do not skip)
The XT25F128F is **3.3 V (2.7–3.6 V)**. Many cheap **CH341A "black" boards drive the
SPI I/O lines at ~5 V even with the VCC jumper at 3.3 V** (the CH341 chip is 5 V) —
this can stress/kill a 3.3 V NOR. Use a **3.3 V-modded CH341A** (well-documented mod:
lift pin 9 / add a 3.3 V regulator feed to the I/O rail) **or** an external level
clamp/shifter, and set VCC to **3.3 V**. **Never 5 V to this chip.** Green v1.4-type
boards / dedicated 1.8–3.3 V programmers avoid the issue.

## Appendix C — XT25F128F pinout (WSON-8; logical = standard 25-series / W25Q128)
```
 1 CS#        2 SO/IO1      3 WP#/IO2    4 GND
 8 VCC 3.3V   7 HOLD#/IO3   6 SCLK       5 SI/IO0
```
16 MiB = `0x1000000`. flashrom: `-c XT25F128F` (or force `-c "W25Q128.V"`).

## Appendix D — NOR facts & partition map
- **XIP window:** `0x12000000` (SDK `CONFIG_SPI_XIP_VADDR`, `CONFIG_SPI_XIP_READ`).
  PSRAM is separate at `0x38000000` — don't confuse them.
- **Encryption:** reference `firmware.xml` = `enable_encryption=false`,
  `enable_crc=false` on **all** partitions ⇒ on a matching build, XIP read == raw NOR.
  Shipping-unit status unknown → the R2 A-vs-B cross-check settles it.
- **Partition map** (offsets within the 16 MiB image; recovery-research notes / SDK `firmware.xml`):
  | addr | size | partition |
  |---|---|---|
  | `0x0` | `0x1000` | BOOT (`mbrec.bin`) — not OTA-writable |
  | `0x1000` | `0x1000` | SYS_PARAM (`param.bin`) |
  | `0x4000` | `0x30000` | RECOVERY (`recovery.bin`) — not OTA-writable |
  | `0x34000` | `0x10000` | coredump |
  | **`0x44000`** | **`0x197000`** | **SYSTEM (`app.bin`)** — the custom-flash target (R4) |
  | `0x1db000` | `0x20000` | `sdfs.bin` (resources) |
  | `0x1fb000+` | … | nvram (factory/rw/user) |
