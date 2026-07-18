# GTX2 Hardware Teardown & Graduated Recovery Guide

**Date:** 2026-07-11 · **Mode:** built from PUBLIC sources only — no device opened. Intended for use on the **spare** GTX2.
**Legend:** `[S]` SOURCED (FCC exhibit I inspected / SDK source / datasheet) · `[I]` INFERRED · confidence H/M/L
**Companion docs:** `firmware-dfu.md`, `firmware-ota-byte-map.md`, `recovery-research notes` (recovery verdict + boot chain).

> **Safety framing.** This is a *reference*, not an authorization to flash. Opening the watch **voids water resistance** (gasket + adhesive) and risks the LiPo. The custom-FW flash (L5) and any device-accept write test stay **gated on an explicit JP go/no-go** and on a proven recovery path (L4). Work on the **spare** only.

---

## 0. TL;DR
- Confirmed silicon (FCC + datasheets + vendor BOM): **ATS3085(S4)** main SoC ("Maincontroller" per istarmax), **XT25F128F-W** 16 MB SPI-NOR, **WTM2101** RISC-V AI co-processor (2nd firmware!), **UC6228CI (Unicore) GNSS/GPS L1 receiver** (⚠ **NOT** a charger PMIC — corrected §1.2; this is the chip the BLE bind descriptor reports as "chipset"), discrete sensor stack (**ST LIS2DH12** accel, **HX3918** HR/SpO₂, **QMC6308** mag, **SPL07-003** baro), **BP2A579000** + **3304J** charge/load-switch cluster, **RYX 432429** 3.8 V/390 mAh LiPo, 466×466 MIPI-DSI panel + I²C touch on 2 FPCs. Board silk **`CB05-MTL MB V1.2`** (= the `cb05` in the OTA name).
- **PRIMARY recovery + flash = an external SPI programmer on the NOR** (§3): read a **golden.bin** first → custom-flash by writing the modified NOR → **unbrick by writing golden.bin back**. Guaranteed while the flash is reprogrammable, and it **sidesteps BLE-DFU/ADFU and the unresolved `word@0x2C`** (a direct partition write doesn't go through the OTA container).
- ⚠️ **Package/voltage verdict (§1.1, from FCC Fig 12):** the flash is a **leadless WSON-8 @ 3.3 V**. CH341A (3.3 V) is directly compatible (no level-shifter) — **but a standard SOIC-8 clip CANNOT grip a leadless WSON** (no gull-wing leads to clamp). Access = **chip-off + WSON-8 socket** (primary) or **flying-lead micro-solder** (alt), *not* an in-circuit clip.
- **No external USB data** (2-pin magnetic charger = power only) → ADFU/SWD transports live on **internal PCB pads**; kept as **fallback**. ADFU usefully runs **over UART too** (`adfu_txrx=1`), not only USB.
- Graduated plan **L1→L5** below: teardown → pad-probe → NOR dump → restore-proof → custom-FW. Each stage lists tooling, what it unlocks, and risk to the spare.

---

## 0.5 REOPENED 2026-07-12 — sacrificial SPARE available → the gate MOVES (not vanishes)

**Context:** JP now has **spare GTX2 units** to sacrifice. The prior custom-firmware **NO-GO** was gated on *one* unit with no proven recovery (a bad flash = dead daily driver). A spare changes the **blast radius**: worst case is a **dead spare, never JP's daily watch**. That lifts the veto and, crucially, lets us **empirically establish + prove the recovery path** (which was the missing gate). This is JP's own hardware / hobby RE — legitimate.

**New verdict: CONDITIONAL GO — on the SPARE only, gated on a proven backup+restore.** No flash on the daily driver. No custom flash until steps R1–R3 below are demonstrably done.

### 0.5.1 JP FIELD CHECKLIST — teardown photo shot-list (do this first; JP executes, then routes photos to the team)
> Setup: watch **off**; clean anti-static mat; strong light; a **macro-capable camera** (phone macro or a cheap USB microscope is ideal). Opening the case **voids water resistance** (gasket + adhesive) and exposes the LiPo — go slow near the battery and the FPC ribbons. Warm (~50 °C heat pad) to soften adhesive; plastic spudger/picks, not metal, near the LiPo.

**OPEN METHOD — INSPECT FIRST; direction not yet settled (2026-07-13):**
- **The FCC set does NOT settle the entry direction.** The lab photos show the watch *fully disassembled* (back puck off in Fig 1, a front module off in Fig 2), so they reveal which components sit on which face but **not the intended service seam** — every seam was already broken. Neither a front nor a back read is conclusive from these photos alone.
- **Evidence LEANS FRONT (inference, not proof):** the **component side** (ATS3085 + XT25F128F flash + debug-pad cluster) carries the **two display/touch FPC connectors** (Figs 12/13), so that face most likely sits **under the display**; the opposite face is bare solder (Figs 8/9/10) and the back cover is the PPG puck (Fig 1) + battery (Figs 5–7). That *suggests* front access — but the FPC routing is an inference and doesn't prove the service direction.
- **THE DECIDER = inspecting a real CLOSED unit:**
  1. **Back:** removable plate? Look for **screws** (under HR ring / label / rubber plugs) or a **pry seam/gasket line**. Back screws or a clean back seam ⇒ back-service.
  2. **Front:** a **bonded display with no seam** ⇒ front-service (heat + pick the display; it's tethered by the 2 FPCs).
  3. Photograph **closed** back/front/edge; report **screws (count/head) vs glued seam; which face has the removable cover.** Then finalize this section + teardown.html step 01.
- **Safety — holds EITHER direction:** **no heat gun** (heat pad/hair-dryer ≤70 °C only); **never force the bonded AMOLED glass or the LiPo**; plastic tools only; the **2 display/touch FPCs** release by flipping the latch, never pull the ribbon.
- **Once in (either way):** target the component side; macro the pad cluster **NW of the SoC** (Fig-13 red arrow) — the SWD/TP candidate.

Take these shots (label each; **in focus, edge-to-edge**). Reference: FCC internal photos (Figs 11–13) + §1 inventory below.

| # | Shot | Why it matters |
|---|---|---|
| 1 | **Whole watch + front face, in situ** (before opening) | baseline; bezel/glass seam; confirms front-entry starting state |
| 2 | **Whole component-side PCB, straight down** | master overview — SoC / flash / FPCs |
| 3 | **Whole skin-side PCB, straight down** | the other half of the pad hunt |
| 4 | **Macro: main SoC laser marking** | confirm **eagle logo · `S4` · `ATS3085` · `ZR62AM0W`** ⇒ Actions **ATS3085S4** |
| 5 | **Macro: flash chip** (upper-left of SoC) | confirm **`XT25F128F-W`**; count pins; **leadless WSON vs leaded SOP** (decides clip vs chip-off, §1.1) |
| 6 | **Macro: small QFN/LGA near SoC** | the **WTM2101** co-proc candidate (2nd firmware) |
| 7 | **Macro: RF/GNSS corner** | `UC6228CI` (Unicore GPS) + antenna feed |
| 8 | **Macro: charge/PMIC cluster** | `BP2A579000` + `3304J` FETs |
| 9 | **★ Macro of EVERY pad cluster / test-point (TP) group, both sides, with a scale ref** | **the recovery gate** — this is what identifies SWD/UART. Start with the **Fig-13 red-arrow upper-left cluster.** |
| 10 | **Macro: any silk text near pads** | look for `DIO`/`CLK`/`RST`/`RX`/`TX`/`SWD`/`SWC`/`TP1..n` |
| 11 | **Macro: battery pads** (`B+/B-`, `A+/A-`) | orientation + 2nd feed ID |
| 12 | **Board silk + rev/date codes** | confirm `CB05-MTL MB V1.2` |

**What to LOOK FOR in the pad clusters (§2 has the probe method):**
- **SWD** — a tight cluster of **2–6 gold pads** near the SoC (or under/beside the display FPC): **SWDIO, SWCLK** (minimum), often + **GND, VCC/VREF, nRESET, SWO**. This is the prize — it enables halt/read/write/backup.
- **UART0** — **2–3 pads** (TX, RX, GND). TX **idles high (3.3 V) and bursts** at power-on. Actions UART0 = 2 Mbaud console + a recovery transport (ADFU-over-UART).
- **Any isolated gold pad/via** not tied to an obvious part = candidate TP (debug or flash-net).
- **Flash-net pads** near the `XT25F128F` (CLK/CS/IO0–3) — the programmer fallback if SWD is locked.

### 0.5.2 RECOVERY-PATH ASSESSMENT — the safe sequence (the gate; do NOT skip)
**R1 → R2 → R3 must all pass BEFORE any custom flash (R4):**
- **R1 — Establish wired access.** Probe the candidate SWD pads with a debug probe (ST-Link / J-Link / any **CMSIS-DAP**) + OpenOCD/pyOCD **"connect under reset"**; read **IDCODE** → an Arm M33 DP confirms SWD is present and *not* fused off. If SWD is dead → fall back to the **external SPI-NOR programmer** (works regardless of SoC lock — see §0.5.3).
- **R2 — READ + BACK UP the full stock flash FIRST → `golden.bin`.** Via SWD (dump the 16 MB NOR through the SoC) **or** the SPI programmer (read the `XT25F128F` directly). **Read twice, `cmp`, verify 16 MiB, keep multiple off-device copies.** This is the un-brick image — nothing else matters if this doesn't exist.
- **R3 — PROVE RESTORE.** Write `golden.bin` back and confirm the spare **boots normally.** This is the hinge that converts *"brick = dead"* → *"brick = re-flash golden."* Until R3 passes on the spare, treat every custom flash as one-way.
- **R4 — Custom flash** (only now): flash the modified app; on any failure, restore `golden.bin`.

### 0.5.3 Secure-boot / read-protection — the real remaining risks `[assess on the spare]`
- **SWD fused off?** Then no SWD backup — but the **flash is EXTERNAL SPI-NOR (`XT25F128F`)**, so an external programmer reads/writes the die **directly, bypassing the SoC and any SWD lock.** This is the ultimate safety net and why a permanent brick is *unlikely* with a programmer in hand.
- **Flash read-protection on the SoC controller?** Blocks SWD-side reads, but again the external programmer reads the raw NOR unaffected.
- **Secure-boot eFuse requires a signed `mbrec`?** Status unknown. Matters **only** for replacing the bootloader — the **app-only raw-accel mod keeps the stock `mbrec`** (only `app.bin` is CRC-checked, never signed), so this does **not** block the mod.
- **The only no-escape scenario** — internal flash + SWD locked + no programmer access — **does not apply here** (flash is confirmed external). Residual real risks: the WSON-8 is **leadless** (chip-off + socket or flying-lead, invasive; pad-lift/LiPo-heat risk, §3.1), and any teardown loses water resistance.

### 0.5.4 FLASHING APPROACH — wired first, BLE-OTA second
- **✅ FIRST attempt = WIRED (SWD, else external SPI-NOR programmer).** It uniquely gives **read-back → backup → write → restore** (the full recovery net) and **sidesteps both BLE-OTA unknowns** (the unresolved `checksum_A@0x2C` and whether a custom app re-advertises BLE). A direct partition write never touches the OTA container. This is the realistic first move on the spare.
- **⏸ SECOND = BLE-OTA (Actions `0xD1–D4` bulk plane, `res.ota`).** The stock path is fail-safe (stage→verify→commit; can't touch `mbrec`/`recovery`), so a bad *transfer* reverts — but a custom app that **fails to re-advertise BLE** strands the unit, and `checksum_A@0x2C` **may** reject a repacked image (untested). BLE-OTA shines **after** wired recovery is proven: fast custom-image iteration with the wired `golden.bin` as the net.
- **⚠️ D6Flasher does NOT apply to this watch.** It is a **Nordic nRF52832 Secure/Legacy-DFU** flasher (atc1441/ATCwatch ecosystem) — wrong SoC, wrong RTOS, wrong DFU. The GTX2 is **Actions ATS3085 / Zephyr**; its OTA is the reverse-engineered **`0xD2` bulk plane** (`firmware-dfu.md`), not Nordic DFU. Do not use D6Flasher here. (See `related-work-d6flasher.md` — relevance verdict LOW.)

### 0.5.5 Reconciliation with the prior NO-GO
| | Prior (single unit) | Now (sacrificial spare) |
|---|---|---|
| Blast radius of a brick | **daily driver dies** → veto | **a spare dies** → acceptable |
| Recovery path | unproven, unprovable without risking the only unit | **can be established + proven destructively on the spare** (R1–R3) |
| Verdict | **NO-GO** | **CONDITIONAL GO on the spare**, gated on R1–R3 |

**What the spare changes:** the blast radius, and the ability to *prove* recovery. **What still holds:** recovery must actually be proven (R3) before trusting it; teardown is invasive and irreversible for water resistance; SWD may be fused (→ programmer route); secure-boot status is still unknown (mbrec-only). **Keep custom firmware on the spare; leave the daily driver on stock.**

---

## 1. Confirmed hardware inventory
All chip IDs below are read from the **FCC 2ASAU-GTX2 Internal Photos** (Figs 11–13, which I inspected) unless noted.

| Component | Marking (as seen) | Identity | Package | Source |
|---|---|---|---|---|
| **Main SoC** | eagle logo · `S4` · `ATS3085` · `ZR62AM0W 37K` | Actions **ATS3085** "Leopard", Cortex-**M33** (Armv8-M). `S4` sub-mark ⇒ likely **ATS3085S4** (matches SDK `ats3085s4_dev_watch_ext_nor` + `soc_boot.h` `is_apm/3085s4`). **App core ≤ 202 MHz** (Actions brands it &ldquo;MStar&rdquo;) + **CEVA TL420** audio DSP @ 202 MHz; **1168 KB SRAM + 4/8 MB DDR OSPI PSRAM**; 2D GPU + JPEG dec; 16 µA/MHz — Actions published spec `[S]` (actionstech.com; 240 MHz is a family &ldquo;up-to&rdquo;, the S4 variant is 202) | QFN, ~ center of PCB | FCC Fig 12/13 `[S,H]`; SKU `[I,M]` |
| **NOR flash** | `XT25F128F-W` · `2336ASC` | **XTX XT25F128F**, 128 Mbit = **16 MB** SPI-NOR, QuadSPI, 3.3 V, W25Q128 command-compatible | **leadless WSON-8 (~6×5 mm)** — see §1.1; upper-left of SoC | FCC Fig 12 `[S,H]`; datasheet `[S]` |
| **AI co-proc** | *(not clearly labelled in photos)* | **WTM2101** (Witmem/WITINMEM) computing-in-memory AI SoC: 32-bit **RISC-V** + CIM-NPU + Fbank + 320 KB SRAM; always-on voice/health. **Has its own firmware** (`wtm2101_ota` partition). Min pkg 2.6×3.2 mm | small QFN/LGA near SoC | firmware partition `[S]`; part ID `[I,M]` — not visually confirmed |
| **GNSS / GPS** | `UC6228CI` | **Unicore UC6228 GNSS (GPS L1) receiver** — ⚠ **corrected**: prior revs called this "charger PMIC" from its FCC-photo neighbours; the istarmax vendor BOM lists it as **"GPS: UC6228CI (L1)"**. This is also the string the BLE bind descriptor reports as "chipset" — a **peripheral**, not the app SoC. | Figs 11 (near RF/charge cluster) | vendor BOM `[S,H]`; function-corrected |
| **Charger / load-switch** | `BP2A579000`, `3304J`×3, `12CA` | Li-ion charger + load switches (`3304J` FETs) | Figs 11 | FCC `[S,H]` |
| **Accelerometer** | *(discrete, skin/edge)* | **ST LIS2DH12** — 3-axis nano MEMS, 12-bit, **32-level FIFO**, raw XYZ regs `0x28–0x2D`, I²C/SPI, ODR ≤5.376 kHz; mainline Zephyr `st,lis2dh` driver exists. **The chip we'd read for raw-accel-over-BLE.** | not in top-side photos | vendor BOM `[S,H]` + ST datasheet |
| **HR / SpO₂ · Mag · Baro** | *(discrete)* | **HX3918** PPG HR/SpO₂ · **QMC6308** magnetometer · **SPL07-003** barometer | skin-side / edges | vendor BOM `[S,H]` |
| **Battery** | `RYX 432429` `+3.8V 390mAh` `202410 1.482Wh` | LiPo pouch, ~43×24×29, made 2024-10 | Figs 5–7 | FCC `[S,H]` |
| **Display + Touch** | 2× black FPC connectors | 466×466 round MIPI-DSI panel + I²C cap-touch | bottom edge, Fig 12/13 | FCC `[S,H]` + SDK `[S]` |
| **Board** | `CB05-MTL MB V1.2`, `102108A 0.7`, `ZBX 2420` | main PCB rev; `CB05` ⇒ the `cb05` in `cb05_yhzn01_v1.0.3…ota` | — | FCC `[S,H]` |
| **Other** | `MIC`, red side crown/button, `B+/B-`, `A+/A-` pads | mic; physical key; battery pads (`B±`); `A±` = 2nd feed (antenna/speaker) `[I]` | edges | FCC `[S]` |

**Board topology (component/top side, Fig 12/13):** ATS3085 center → **XT25F128F immediately upper-left of it** → two display/touch FPCs along the bottom → PMIC/charge cluster + coil on one edge (Fig 11) → `MIC` top → crown/button right → `B+/B-` bottom-left, `A+/A-` left. **A red FCC reviewer arrow on Fig 13 points to an upper-left component/pad cluster** (candidate antenna feed or test points — worth close inspection first). Health/PPG optical sensor sits on the **skin-side (opposite) face**, not shown in these top-side shots. `[S]`

## 1.1 NOR package & voltage verdict — DECISIVE for programmer tooling `[S,H]`
I zoomed the FCC Fig-12 flash region (600 DPI render) to settle the two questions that decide whether JP's SOIC-8 clip works.

- **Voltage = 3.3 V (2.7–3.6 V).** XTX's product page lists XT25F128F at **2.7–3.6 V**, and the datasheet is titled *"XT25F128F-**W 3.3V** Quad I/O Serial Flash"*. The board marking's `-W` = the **3.3 V family** (a voltage designator, **not** a package code). ⇒ **CH341A @3.3 V is directly compatible — NO level-shifter.** A 1.8 V variant would carry a different designation and is not indicated. `[S,H]`
- **Package = leadless WSON-8 (~6×5 mm), NOT leaded SOP-8.** In the zoom the `XT25F128F-W / 2336ASC` body shows **no outward gull-wing leads** — contacts terminate flush at the package edge (side-wettable flanks). XTX ships this die as **WSON8 6×5 mm** (order code `…WO…`, e.g. `XT25F128FWOIGT-W`) or SOP-8 (`…SS…`); the photo is unambiguously the **leadless WSON**. `[S,H]`
  - ⇒ **A standard SOIC-8/SOP-8 clip (Pomona 5250 & clones) CANNOT clamp this chip in-circuit** — there are no protruding leads to grip. This is the key tooling consequence; the dump/restore path (§3) is built around it.

## 1.2 SoC-label reconciliation — `ATS3085S4` is the app SoC; `UC6228CI` is the GNSS chip `[S,H]`
The BLE bind descriptor reports `chipset = "UC6228CI"` (protocol-spec §3.1 f18), which earlier notes mistook for the application processor. **It is not.** Authoritative resolution:
- **istarmax (the vendor's own spec page) lists "Maincontroller: ATS3085S4"** and, separately, **"GPS: UC6228CI (L1)"** (BOM captured in `work/fccid-research/ats3085-fccids.md`). "Maincontroller" is the vendor's term for the application SoC.
- **`UC6228CI` = Unicore Communications UC6228 GNSS/GPS L1 receiver** (Unicore's UCxxxx family is all GNSS SoCs). It has **zero** firmware/toolchain footprint — a repo-wide grep finds it only in the bind string and this hardware table. The entire flashable stack (Zephyr/LVGL, Actions OTA `FA EE EB DE`, creek BLE, BROM/ADFU, the `ats3085s4` board, the `cb05` silk) belongs to the **ATS3085S4**.
- **Why the bind field says `UC6228CI`:** the vendor populated the "chipset" string with a peripheral/module part number (the GNSS chip) rather than the app SoC — a mislabel from an RE standpoint. **Custom-firmware work targets the ATS3085S4 (Cortex-M33), never `UC6228CI`.** Confusing them would send the effort hunting for a non-existent GNSS-chip firmware toolchain.
- (Corollary: the FCC "Fig 11" reading that grouped `UC6228CI` with `BP2A579000`/`3304J` as a "charger PMIC" was a proximity guess; the vendor BOM overrides it — `UC6228CI` is GNSS, the charge/load-switch cluster is `BP2A579000` + `3304J`.)

---

## 2. Test-pad access (the recovery gate)
Actions does **not** publish ATS3085 ball/pin maps (NDA), so physical pads must be found by **continuity from known chip pins**, not from a pinout. General method, then per-signal.

**Baseline setup:** multimeter (continuity/diode), USB **3.3 V** logic analyzer (≥24 MHz; the debug UART is 2 Mbaud so sample ≥8×), 3.3 V USB-UART adapter, fine tip probes, microscope/loupe, and a bench supply or the battery for power. **Never feed 5 V to any pad** — this is a 3.3 V/1.8 V board.

### 2.1 UART0 — primary console + a recovery transport `[S for existence]`
- SDK: `UART_0` @ **2,000,000 baud, 8N1**, `uart_mfp=1` (`bootloader.ini [serial config]`, `board_cfg.h`). This is the boot/console UART **and** the ROM UART-download transport. `[S,H]`
- **Find it:** at power-on, **TX idles high (3.3 V) and bursts** as the bootloader prints. Scope/logic-probe candidate gold pads/vias near the SoC while booting; the one showing 2 Mbaud 8N1 framing is **TX**. **RX** is the adjacent input pad (high-Z / weak pull-up, no activity). Confirm by connecting a 3.3 V USB-UART (TX↔RX crossed) at 2 000 000 baud and watching for the Actions/Zephyr banner (`*** Booting Zephyr OS build …`).
- Value: read-only console = crash logs, boot state, `os_ota` messages — and the door to **ADFU-over-UART** (§4).

### 2.2 SWD / SWO — debug + potential flash access `[S present; I exposure]`
- M33 has SWD+SWO (`HAS_SWO`). `bootloader.ini [jtag config] jtag_groud=0xff` most likely = **JTAG/SWD pin-group not muxed out by the bootloader** (i.e. off by default) — unconfirmed. `[I,M]`
- **Find it:** SWDIO carries a pull-up, SWCLK is a clock input. Either (a) use a J-Link/ST-Link "**connect under reset**" while touching candidate pad pairs, or (b) a **JTAGulator / SWD-scan** (e.g. `pyswd`/`openocd` swd scan) across the small pad cluster (the Fig-13 red-arrow area is the first candidate). If SWD responds you can read `IDCODE` (should show an Arm M33 DP). If it's fused off, no response.

### 2.3 USB D+/D- — likely NOT broken out `[S,H]`
- The charger is a **2-pin magnetic pogo (VBUS+GND only)** — confirmed in External Photos Fig 9 and `board_cfg.h` defines **no USB pins**. So USB device mode, if used at all, would be on **internal-only pads**. `[S,H]`
- **Find it (if present):** a **90 Ω differential pair** from the SoC to unpopulated pads; on a FS device **D+ has a 1.5 kΩ pull-up to 3.3 V** when connected. Look for a closely-routed pad pair; verify with continuity to the SoC and the pull-up on one line. **Expect this to be absent/unpopulated** — which is exactly why **UART ADFU (§4) is the realistic recovery transport.**

### 2.4 SPI-NOR pads — for the dump (§3)
Easiest signals to find: probe directly from the **XT25F128F** pins (§3 pinout). CLK/CS/IO0-3 often have nearby test vias.

---

## 3. External SPI-programmer dump / restore / flash — PRIMARY recovery + flash path `[S]`
An external programmer on the XT25F128F is the **guaranteed unbrick and the safest flash route**: read a **golden.bin** before any write; custom-flash by writing the modified NOR; if anything goes bad, write golden.bin back. It bypasses BLE-DFU/ADFU **and** the unresolved `word@0x2C` — a direct partition write reads back the raw image the bootloader expects, not the OTA container.

**Voltage (§1.1): 3.3 V** → CH341A / FT2232H / Raspberry-Pi SPI at **3.3 V**, driven by `flashrom`. **No level-shifter. Never 5 V.**
**`flashrom` id:** XT25F128F is recognised by recent flashrom (else force a `W25Q128.V`-class 16 MiB profile with `-c`). Expect **16 MiB = 0x1000000**.

**XT25F128F pin map (die is WSON-8; logical pinout = standard 25-series / W25Q128):** `[S — XT25F128F datasheet]`
```
 1 CS#      2 SO/IO1     3 WP#/IO2   4 GND
 8 VCC 3.3V 7 HOLD#/IO3  6 SCLK      5 SI/IO0
```
(WSON pad order matches SOP-8 logically; a WSON socket presents these on DIP pins.)

### 3.1 Accessing a **WSON-8 leadless** flash — the clip won't grip it
Because the part is leadless (§1.1), a SOIC-8 clip has nothing to clamp. Pick by risk/ergonomics:

| Method | How | Trade-off |
|---|---|---|
| **A. Chip-off + WSON-8 socket (PRIMARY, most reliable)** | Hot-air desolder the WSON (~300 °C, low airflow, flux, **Kapton over the LiPo/FPCs**), read/write off-board in a **WSON-8 (6×5) → DIP/SOP-8 adapter socket** on the CH341A, then re-solder. | Zero bus contention, 100 % reliable R/W; invasive — repeated reflow risks pad lift. Best for a one-shot flash + kept golden.bin. |
| **B. Flying-lead micro-solder (in-circuit)** | Solder thin (AWG34/enamel) wires to the 8 pads (or to flash-net test vias) → programmer; **hold the ATS3085 in reset or isolate flash VCC** to stop the SoC fighting the bus. | Chip stays on; fiddly soldering; contention unless the SoC is quiesced. |
| **C. WSON pogo/clamshell socket clip** | A **WSON8-specific** spring-pin clamp (not a SOIC clip) if the side flanks are reachable. | Only if such a clamp is on hand; finicky alignment. |
| **D. JP's SOIC-8 clip** | Works **only** if the board happens to expose a SOP-8-footprint test-pad set on the flash net. | Likely **N/A** for this leadless part — verify at L1/L2, don't count on it. |

### 3.2 DUMP-FIRST discipline (before ANY write)
1. `flashrom -p ch341a_spi -r golden1.bin`, then again `-r golden2.bin`.
2. **`cmp golden1.bin golden2.bin` must match** and size = 16 MiB; then `flashrom -v golden1.bin`. Two identical reads = trustworthy dump. Keep **`golden.bin` off-device (multiple copies)** — it is the restore image.
3. Only after a verified golden.bin exists do you write anything.

### 3.3 Custom flash via the programmer (PRIMARY L5)
- **Whole-image:** patch the `app` (SYSTEM) region inside the dump, recompute the container/partition CRCs (see `firmware-ota-byte-map.md`), `flashrom -w modified.bin`, verify.
- **App-only (lower risk):** write just the `fw0_sys` offset range (partition map in `recovery-research notes`), leaving mbrec/param/recovery/nvram untouched.
- Either way this **sidesteps BLE-DFU and `word@0x2C`** entirely.

### 3.4 Unbrick = restore
`flashrom -w golden.bin` → verify → reassemble. **Guaranteed while the flash is reprogrammable** — this is the safety net that makes L5 acceptable.

**What the dump also unlocks:** the full on-flash **`firmware.xml` partition image** (mbrec@0x0, param, recovery, app, sdfs, nvram), factory **nvram/calibration**, the **secure-boot/eFuse** state, and a content diff that can finally **classify `word@0x2C`**. `[I,H]`

---

## 4. BROM / ADFU recovery — FALLBACK transport (no chip-off needed) `[S,H mechanism]`
Secondary to the programmer (§3): useful because it needs **no NOR desolder** and works **even if NOR is blank/corrupt**. The on-chip **mask-ROM (BROM)** always runs first and exposes ROM download launchers (`brom_interface.h`: `p_adfu_launcher` USB, `p_brom_uart_launcher` UART).

- **Entry trigger:** `board.h` `{KEY_ADFU, 0x05}` (ADFU in the key matrix); `bootloader.ini [adfu config] adfu_txrx=1, adfu_gpio=0`. ⇒ ADFU is entered by a **key/GPIO held at power-on** (`adfu_gpio=0` = GPIO0 is the ADFU trigger `[I]`), and can transport over **both USB and UART** (`adfu_txrx=1`). A live UART shell can also force it (`dbg reboot adfu`). `[S,H]`
- **Transports:**
  - **UART ADFU** (realistic here): UART0 pads (§2.1) @2 Mbaud + the Actions tool speaking the ADFU-UART protocol. Preferred because **USB D± is likely not exposed.**
  - **USB ADFU** (only if D± pads exist, §2.3): device enumerates as Actions VID:PID **`10d6:10d6`**.
- **Host tools:** Windows **Actions "USB Production Tool" / config-tool** (the LVGL port README flashes an EVB via "hold KEY ADFU → select ADFU0"). Open reference: **`96boards-bubblegum/linaro-adfu-tool`** (sibling Actions SoC; confirms ADFU works with blank/corrupt storage; would need adaptation for ATS3085). `[S]`
- **What it flashes:** everything (`enable_dfu=true` on all partitions incl. `mbrec`) — factory-grade, can rewrite the whole image (unlike BLE OTA which only touches `app`+resources). A no-solder alternative recovery/flash route **if** it works on the shipping unit; the **programmer restore (§3.4) remains the primary safety net** since it doesn't depend on ADFU being reachable/unsigned.
- **Caveat — secure boot:** `p_mbrc_brec_data_check(buf, digital_sign)` shows the BROM *can* require a **signed mbrec** if the secure-boot **eFuse** is burned. Whether the shipping GTX2 burns it is **unknown** (§7) — the NOR dump (§3) + a test read of eFuse state via ADFU would settle it. If fused, ADFU only accepts vendor-signed boot images (app-only reflash still fine; see byte-map — app path is CRC-only). `[S mechanism; I enforcement, M]`

---

## 5. SWD debug attach `[S present; I exposure]`
If §2.2 finds SWDIO/SWCLK:
- **Tooling:** J-Link (best M33 support) or ST-Link v2/v3 + OpenOCD; 3.3 V ref, GND, SWDIO, SWCLK, and ideally nRESET + SWO (for `printf`/ITM trace).
- **Uses:** halt/step the M33, read RAM/flash, set breakpoints in the OTA/verify path, dump the running image, and — if not locked — **write flash directly** (an alternative recovery/flash route to ADFU). `openocd -f interface/jlink.cfg -c "transport select swd" -f target/<m33>.cfg`.
- **If locked:** SWD may be disabled by eFuse/option — then only ADFU remains.

---

## 6. Graduated plan (L1 → L5)
Each level is a decision gate; do not proceed until the prior level's "unlocks" are in hand. Risk is to the **spare**.

| Lvl | Action | Tooling | Unlocks | Risk to spare |
|---|---|---|---|---|
| **L1** | **Teardown + photograph + confirm chip IDs.** Pop back cover, lift PCB, high-res macro of both sides; locate ATS3085, XT25F128F, WTM2101 candidate, PMIC, FPCs, crown, pads. | spudger/plastic picks, heat pad (~50 °C) to soften adhesive, tweezers, loupe/USB microscope, camera | Ground-truth board map; find candidate pads (start at the Fig-13 red-arrow cluster) | **LOW** — loses water resistance (reseal gasket); take care near LiPo/FPCs |
| **L2** | **Test-pad probing.** Trace/confirm UART0 TX/RX (2 Mbaud console), hunt SWDIO/SWCLK, check for USB D± pair, map NOR pins. | multimeter, 3.3 V logic analyzer, 3.3 V USB-UART, fine probes | Live console + crash logs; SWD presence yes/no; confirms USB-data absence | **LOW–MED** — shorting adjacent pads; ESD |
| **L3** | **NOR dump via external programmer.** Access the WSON-8 by **chip-off + WSON socket** (primary) or **flying leads** — **not** a SOIC clip (§3.1). Read golden.bin ×2, `cmp`+verify. | CH341A/FT2232H + `flashrom` @**3.3 V** + **WSON-8→DIP socket** (chip-off) or AWG34 flying leads; hot-air for chip-off | **golden.bin** (= restore image); full partition map; classify `word@0x2C`; read eFuse/secure-boot; factory nvram/calibration | **MED** — chip-off pad-lift / LiPo heat; contention if flying-lead in-circuit |
| **L4** | **Restore-proof.** Write **golden.bin back** with the programmer and confirm the watch boots — proving unbrick *before* any risky write. (ADFU-over-UART is the no-solder alternative if it works.) | same programmer/socket as L3; or ADFU (§4) | **A proven unbrick path** — prerequisite for any L5 write | **MED** — write is validated against your own golden.bin; keep off-device copies |
| **L5** | **Custom-FW flash.** Only after L4 proven + **explicit JP go/no-go**. **Primary: write the modified NOR (whole-image or app-offset) directly with the programmer (§3.3)** — sidesteps DFU & `word@0x2C`. Secondary: repacked BLE-OTA / ADFU. | programmer + repack tooling (byte-map); (BLE-OTA needs `word@0x2C` resolved) | Running custom firmware with a proven way back | **HIGH** — brick risk mitigated by the L3/L4 golden.bin restore; deferred/gated |

**Sequencing logic:** L1–L3 are **non-destructive to firmware** and independently valuable (map + golden backup + secure-boot answer). With a programmer, **L4 is nearly free** — reading *and* writing the WSON proves restore inherently. L4 is the hinge that converts "brick = dead" into "brick = re-flash golden.bin"; **L5 must never precede a proven L4.** The direct-programmer L5 path also means `word@0x2C` is **not** on the critical path (it only gates the BLE-OTA container route).

---

## 7. Open uncertainties (resolved by the physical spare)
- **Secure-boot eFuse burned?** Decides whether ADFU accepts arbitrary `mbrec`. Resolve via NOR dump + ADFU eFuse read (L3/L4). `[I, L–M]`
- **Are UART0 / SWD / USB-D± actually on solderable pads?** Not determinable from FCC photos; L2 answers it. `[I]`
- **Which small IC is the WTM2101**, and its host interface (SPI/UART/I²C) + whether its `wtm2101_ota` is flashed through the ATS3085 or independently. L1/L2 + a dump answer it. `[I,M]`
- **Exact GTX2 (cb05) pin map** — the SDK GPIO numbers cited are from the **reference/leopard board**, not guaranteed identical to `CB05-MTL`; treat as indicative and verify by continuity. `[I]`
- **`A+/A-` pads** — antenna feed vs speaker vs 2nd sensor. `[I]`

---

## 8. Sources
- **FCC 2ASAU-GTX2** (I inspected the exhibits): index <https://fccid.io/2ASAU-GTX2>; Internal Photos (Figs 1–13, chip IDs) & External Photos (2-pin charger). Local copies: `work/recovery-research/fcc/{internal,external,manual}.pdf`.
- **SDK / boot chain** (Actions leopard): <https://github.com/lvgl/lv_port_actions_technology> — `boards/.../{board.h,board_cfg.h,bootloader.ini,firmware.xml}`, `zephyr/soc/arm/actions/leopard/{brom_interface.h,soc_boot.h,Kconfig.series}`, `application/bt_watch/src/ota_recovery/recovery_main.c`, `zephyr/tools/build_boot_image.py`. Local: `work/recovery-research/sdk/`.
- **XT25F128F** — voltage & package verdict: XTX product page **"电压: 2.7~3.6V"** <https://www.xtxtech.com/en/Products/info.aspx?productModel=XT25F128FWOIGT> (WSON order code `…WO…`; SOP variant `XT25F128FSSHGT-W`); datasheet titled *"XT25F128F-W 3.3V Quad I/O Serial Flash"* (Rev 0.6) via LCSC <https://datasheet.lcsc.com/lcsc/2207051802_XTX-XT25F128FWOIGT-W_C3202839.pdf>; WSON8 6×5 mm packaging confirmed by distributor listings. Package (leadless WSON vs leaded SOP) determined from **FCC Fig 12 zoom** (no gull-wing leads).
- **WTM2101**: Witmem/WITINMEM CIM AI SoC — <https://verimake.io/2022/03/14/nuclei-help-witinmem-to-release-the-worlds-first-integrated-storage-and-computing-chip-wtm2101/>, tooling <https://github.com/witmem/Algorithms-in-WTM2101>.
- **ADFU / recovery**: <https://github.com/96boards-bubblegum/linaro-adfu-tool> (mechanism, VID:PID 10d6:10d6); Actions USB Production/config tool (via SDK README).
- **PSA Certified ATS3085/ATS3089** (M33/TF-M): <https://products.psacertified.org/products/ats3085-ats3089-series>.
- Recovery verdict + partition map depth: `recovery-research notes`.
