# Related work: atc1441 / D6Flasher — relevance to the GTX2 Gadgetbridge task

**Investigated:** 2026-07-11 · **Subject:** <https://github.com/atc1441/D6Flasher> and atc1441's (Aaron Christophel) broader smartwatch reverse-engineering work.
**Question:** does any of it transfer to adding Gadgetbridge support for the **Starmax GTX2** (Actions ATS3085 + Zephyr, Starmax protobuf-over-BLE)?

---

## TL;DR — RELEVANCE VERDICT: **LOW**

D6Flasher is a **Nordic nRF52832 DFU firmware flasher** for atc1441's *ATCwatch* custom-firmware ecosystem. It shares **no SoC, no RTOS, no BLE application protocol, and no firmware/DFU format** with the GTX2. Nothing in the D6Flasher code or atc1441's repos is byte-level reusable for our task. The only overlap is the *generic* BLE-RE methodology (HCI snoop → Wireshark → opcode map), which our brief already covers. **Do not spend more time here beyond this doc.**

> ⚠️ **Brief correction (verified):** the assignment guessed D6Flasher targets a "Dialog DA14580/DA14585-class 'D6' watch." **That is wrong on the chip.** The "D6" is the **D6 Fitness Tracker on Nordic nRF52832**, not a Dialog part. The *conclusion* the brief drew from the wrong premise (different SoC ⇒ flashing path differs ⇒ low transfer) still holds — arguably more strongly.

---

## 1. What D6Flasher actually is

| Attribute | Finding | Evidence |
|---|---|---|
| Type | Android app (Java, 100%) that flashes firmware over BLE | GitHub repo: `/app`, gradle files, `DaFlasher.iml` (derived from atc1441's DaFlasher) |
| Target device | **D6 Fitness Tracker** and other DaFit-family watches running atc1441's **ATCwatch** firmware | README: "Flashing App for the ATCwatch Arduino Smartwatch Firmware mainly for nRF52832" |
| Target SoC | **Nordic Semiconductor nRF52832** (and other nRF52) | README + sibling repos (below) |
| Method | Standard **Nordic DFU** over BLE — pushes a `.zip`/`.bin` Nordic DFU package to the nRF52 bootloader's DFU GATT service | README: "usable to flash many other **Nordic DFU** files to your nRF52 device" |
| Goal | **Replace** stock firmware with custom Arduino firmware | ATCwatch = "Custom Arduino C++ firmware for the P8 and PineTime plus many more DaFit Smartwatches" |

**The "D6" is nRF52832, confirmed by the sibling repos in atc1441's account:**
- `D6-arduino-nRF5` — *"D6 Fitness Tracker Arduino Core for **Nordic Semiconductor nRF5** based boards"*
- `D6Emulator` — *"D6 Fitness Tracker Custom Firmware for Arduino"*
- `D6Notification` — *"Companion App … mainly for **nRF52832**"*
- `DaFlasherFiles` — *"…for the DaFlasher App and the ATCwatch Firmware for the P8 Smartwatch"*

**Ecosystem note:** the "Da" in the parent *DaFlasher* nods to both **DaFit** (the app/ODM family) and historically **DA14580** (Dialog) — the DaFlasher *compatibility list* spans several cheap-watch chips (nRF52832 explicitly; DA14580/DA14585 in the older lineage). But **D6Flasher specifically** is the **Nordic nRF52 DFU** path. So the DA14580 association is real for the umbrella tooling, just not for D6Flasher itself.

---

## 2. Why it does not transfer to the GTX2

Every axis that matters is different:

| Axis | D6Flasher / ATCwatch | Starmax GTX2 (our target) | Transfer? |
|---|---|---|---|
| SoC | Nordic **nRF52832** (Cortex-M4) | Actions **ATS3085** (Cortex-M4F + CEVA DSP) | ❌ none |
| RTOS / stack | Arduino / (FreeRTOS on PineTime) | **Zephyr + LVGL** | ❌ none |
| BLE app protocol | **DaFit** family protocol | **Starmax** protobuf: `0xC1` envelope + CRC-16/CCITT-FALSE over the `0x0FF0` command service | ❌ different framing |
| Firmware image | Nordic DFU package (`.zip`/init-packet + `.bin`) | Actions `FA EE EB DE` TOC image containing `firmware/zephyr.bin` | ❌ none |
| DFU transport | Nordic **Secure/Legacy DFU** GATT service | Custom **`0xD2` bulk channel** over the custom GATT service: `D2 \| counter \| 234 raw image bytes` | ❌ none |
| Tool *goal* | **Replace** stock firmware | **Talk to stock firmware** over BLE (no reflash) | ❌ opposite goal |

Two of these are decisive on their own:
1. **Different BLE application protocol.** DaFit (MoYoung ODM) ≠ Starmax. Gadgetbridge *already* has a MoYoung/DaFit coordinator; our GTX2 is a distinct protobuf protocol (our `decode-notes.md` has the `0xC1`/CRC-16 envelope). No opcode or struct carries over.
2. **Primary task is BLE-only on stock firmware.** The custom-firmware angle (which is D6Flasher's whole purpose) is now tracked separately as a **reopened investigation** — [issue #17](https://github.com/jphein/starmax-gtx2/issues/17) (CFW feasibility + recovery-path + expose raw accelerometer), with spare-unit teardown as its hardware path. It remains brick-risky and off the main BLE path. Even our OTA notes (the `0xD2` bulk channel) use a completely different mechanism than Nordic DFU.

---

## 3. atc1441's other work — is anything relevant? (No.)

Enumerated **all ~71 repos** authoritatively via `gh repo list atc1441`. His smartwatch/wearable RE covers these SoC families — **none is Actions ATS-series or Zephyr-based:**

- **Nordic nRF52** — `ATCwatch`, `D6-arduino-nRF5`, `D6Emulator`, `Pinetime`, `pixlAnalyzer`, `Tag_FW_nRF52811`, `ESP32_nRF52_SWD`
- **DaFit / Dialog lineage** — `DaFlasherFiles`, `Magic3_DaFit`
- **Telink TLSR** — `ATCmiBand8fw` (Mi Band 8), `ATC_MiThermometer`, `ATC_TLSR_*`, `Xiaomi-TLSR-Firmwares`
- **BES2700iMP / BEST1503** — `MiBand10-BES2700iMP-BEST1503-Hacking` (Mi Band 9/10)
- **Other rings/misc** — `ATC_RF03_Ring` (Colmi R02, BlueX RF03), `ATC_SR08_Ring`, `Disno_band_NRF31512`

**Searched explicitly for `atc1441 + Actions/ATS3085/Starmax/Runmefit/Zephyr` — zero hits.** He has never touched the Actions Zephyr platform our watch uses.

Two repos are *tangentially* interesting but **not** load-bearing for our task:
- `smart-watch-socs` — a community "watch → SoC → price" list. Could in principle help map which cheap watches share the ATS3085 (⇒ likely share the Runmefit protocol). **Checked it: the index only links Nordic and HunterSun pages — no Actions/Starmax entry yet.** Low value today.
- `walv` — a *browser LVGL GUI designer*. Cute overlap (GTX2 is LVGL) but it's a design toy, not RE, and irrelevant to the BLE coordinator.

---

## 4. What (little) is genuinely transferable

- **Generic BLE-RE methodology only.** atc1441's universal playbook — Android HCI snoop log → Wireshark `btatt` filter → identify write/notify chars → map opcodes → build a companion app — is exactly the approach in our brief and `decode-notes.md`. atc1441 adds **no GTX2-specific technique** on top of what we already have.
- **Nothing from the flashing/DFU path** (Nordic DFU ≠ Actions `0xD2`; and OTA is out of MVP scope).
- **Nothing from watch-face tooling.** For GTX2 resources use the *Actions* tools the brief already cites: `Viper7000/ATS3085S_firmware_unpacker` and `dipcore/unpack_clock_res` — **not** atc1441's DaFit face tooling.

---

## 5. Confirmed: no public Starmax/Runmefit BLE RE exists (D6Flasher isn't hiding one)

Searched beyond atc1441 for anyone who has reversed the **live** Starmax/Runmefit BLE protocol. **None found.** The ATS3085S community (XDA thread, `dipcore/unpack_clock_res`, DT No.1's `gen_clock`) has only reversed **resource/watch-face** formats — exactly what our brief states. So the GTX2 BLE coordinator remains **greenfield** — mapped from our own captures, cross-referenced against the public [RunmefitSDKDemo](https://github.com/developersth/RunmefitSDKDemo). D6Flasher does not change that.

**Closest *structural* precedents for "write a coordinator for a cheap Chinese watch"** (all different protocols — reference only, not reusable code):
- `fbiego/dt78` — DT78 (DaFit-family) RE — a clean worked example of decoding + a companion.
- `eduardoposadas/recun1sw` — Cubot N1 BLE protocol RE.
- kabbi's Umidigi `uwatch2` BLE notes (gist).
- Gadgetbridge's existing **MoYoung** coordinator — the DaFit-family coordinator to read for *code structure* (per the brief's "model on a similar device").

---

## 6. Follow-up links

**atc1441 (context / methodology only):**
- D6Flasher — <https://github.com/atc1441/D6Flasher>
- ATCwatch firmware — <https://github.com/atc1441/ATCwatch>
- D6Notification (companion protocol reference) — <https://github.com/atc1441/D6Notification>
- DaFlasher compatibility list (gist) — <https://gist.github.com/atc1441/d0a3c1f5ee69ab901bccba4eb47a6e4e>
- smart-watch-socs (SoC map, no Actions entry yet) — <https://github.com/atc1441/smart-watch-socs>
- atc1441 overview (X/YouTube "Aaron Christophel" for method videos) — <https://github.com/atc1441>

**Actually useful for GTX2 (already in the main brief — repeated so this doc stands alone):**
- ATS3085S firmware unpacker — <https://github.com/Viper7000/ATS3085S_firmware_unpacker>
- Watch-face `.res` format — <https://github.com/dipcore/unpack_clock_res/blob/main/docs/FORMAT.md>
- Public RunmefitSDKDemo (opcode cross-reference) — <https://github.com/developersth/RunmefitSDKDemo>
- Gadgetbridge BT-Protocol-RE wiki — <https://codeberg.org/Freeyourgadget/Gadgetbridge/wiki/BT-Protocol-Reverse-Engineering>

---

## Verified vs inferred

- **Verified** (primary source): D6Flasher = nRF52832 Nordic-DFU flasher (README + sibling repo descriptions); atc1441 has no Actions/Starmax/Zephyr repo (`gh repo list`, ~71 repos); no public Starmax BLE RE (web search); the Actions face-tooling names (XDA + repos).
- **Inferred**: DaFit⇄Starmax being "different protocols" is inferred from the framing mismatch (DaFit fixed-header vs our verified `0xC1`+CRC-16 protobuf) — strongly supported but not proven by a side-by-side byte diff. Doesn't affect the LOW verdict.
