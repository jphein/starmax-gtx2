# GTX2 (CB05-MTL) board port — scope from the EVB `ats3085s4_dev_watch_ext_nor`

**Date:** 2026-07-14 · **Companion:** `cfw-sdk-build.md` (the build is proven),
`custom-firmware-poc.md` §2.3 (gap analysis), `hardware-teardown-guide.md` (#17 recovery/dump).

The SDK's `ats3085s4_dev_watch_ext_nor` is an **EVB / dev-watch**, not the GTX2 `CB05-MTL` board.
Same SoC (**ATS3085S4 "leopard", Cortex-M33**) → the SoC layer, `mbrec` (`libboot.a`), boot chain,
and the whole framework are reusable. The deltas below are the board-support work between a
dev-board build and a GTX2-booting image.

**Key architecture note — this is a *forked* Zephyr 2.7.0 with no per-board devicetree.** There is
**no `.dts`** for any leopard board. Board config is expressed as C: `board.c` (pinmux/init) +
`board_cfg.h` (peripheral enables, panel timing, I²C/DMA/GPIO cfg) + `board.h` (GPIO pin numbers) +
`<board>_defconfig` (Kconfig) + `firmware.xml` (partition table). **Porting = editing these C/Kconfig
files**, not DTS overlays. The cleanest path is to copy the EVB board dir to a new
`ats3085s4_gtx2_cb05` board and edit in place.

Board dir: `zephyr/boards/arm/ats3085s4_dev_watch_ext_nor/`
Panel timing / pin cfg: that dir's `board_cfg.h` + `board.h`. Sensor/accel enables: `<board>_defconfig`.

Tags: **[known]** = fully specified by the EVB files (starting point, may or may not match GTX2) ·
**[needs-NOR-dump]** = resolved by a teardown-L3 NOR dump (retail partition table / param / nvram /
res / board cfg the retail app uses) · **[needs-teardown-photo]** = resolved by a physical teardown
(chip markings on the panel FPC / sensor / PMIC / co-proc).

---

## 1. Port items (EVB → GTX2)

### 1.1 Display panel  — biggest UI risk
| Aspect | EVB (`board_cfg.h`) | GTX2 delta |
|---|---|---|
| Controller | **ICNA3310B** (`CONFIG_PANEL_ICNA3310B`, driver `drivers/display/panel/panel_icna3310b.c`) | GTX2 controller model unknown — **[needs-teardown-photo]** (panel FPC) + **[needs-NOR-dump]** (init table in retail res/param) |
| Interface | **QSPI, dual-lane** (`CONFIG_PANEL_PORT_QSPI`, CPOL/CPHA=1, AHB clk /2) | PoC calls GTX2 **MIPI-DSI** — if true this is a *port-type change*, not just a model swap. ICNA3310 supports both QSPI & MIPI; confirm which the GTX2 wires. **[needs-teardown-photo]** |
| Resolution | **466×466**, round, TE, pixel-clk 60 MHz, refresh 60 Hz | **matches GTX2 466×466** [known] — geometry likely reusable |
| GPIO | reset `GPIOB0`, TE `GPIOB3`, power1 `GPIOA21`, fix-offset X=6 | GTX2 reset/TE/power pins **[needs-NOR-dump]/[needs-teardown-photo]** |

If the GTX2 uses the same ICNA3310-family controller over QSPI, `panel_icna3310b.c` is largely
reusable (retune timing/offset). If MIPI-DSI or a different IC → new `panel_*.c` + LCDC port-type
config. **[needs-teardown-photo]**

### 1.2 Touch controller
| Aspect | EVB | GTX2 delta |
|---|---|---|
| Chip / driver | **CST820** (`CONFIG_INPUT_DEV_ACTS_CST820_TP_KEY`, `drivers/input/tpkey/cst820_tpkey_acts.c`) | GTX2 touch model + I²C addr unknown — if CST8xx-family, driver reusable; else new tpkey driver. **[needs-teardown-photo]** |
| Bus | **I2C_1** (`CONFIG_TPKEY_I2C_NAME=I2C_1`), SCL `GPIO51` / SDA `GPIO52` | confirm bus + pins **[needs-NOR-dump]** |
| GPIO | reset `GPIOB17`, INT `GPIOB18`, low-power mode on | confirm reset/INT pins **[needs-teardown-photo]** |

### 1.3 Accelerometer  — the raw-accel target sensor (#31)
| Aspect | EVB | GTX2 delta |
|---|---|---|
| Chip | **STK8321** + **QMA6100** options (`SENSOR_ACC_STK8321=y`, `SENSOR_ACC_QMA6100=y`) | GTX2 = **LIS2DH12** — **NOT present** in this SDK's sensor HAL. |
| Driver model | Actions `sensor_hal/devices/sensor_acc_*.c` on **I2CMT** (hardware-triggered I²C, see §3) | **Write `sensor_acc_lis2dh12.c`** in that model. Datasheet-easy: I²C addr 0x18/0x19, `WHO_AM_I=0x33`, OUT regs `0x28–0x2D`, 32-lvl FIFO. [known driver shape] |
| Wiring | ISR `GPIO54`, PPI trig **5**, on **I2CMT_1 / TASK0** (STK8321) | GTX2 accel bus + ISR GPIO + I²C addr **[needs-NOR-dump]/[needs-teardown-photo]** |

Note: a **mainline** ST driver exists at `zephyr/drivers/sensor/lis2dw12/` (LIS2D**W**12, a different
part) but it uses the *upstream* Zephyr sensor model, **not** the Actions `sensor_hal`/I2CMT model the
app actually uses — so it is a reference, not a drop-in. LIS2DH12 must be added as an Actions-HAL
device driver.

### 1.4 Heart-rate / PPG
| Aspect | EVB | GTX2 delta |
|---|---|---|
| Chip / algo | **HX3605** (`SENSOR_HR_HX3605`) + **proprietary** algo lib (`sensor_algo/SensorAlgoHR_HX3605/*.a`) | GTX2 PPG chip unknown. SDK ships HX3605/HX3690/GH3011 driver+algo binaries only. **If GTX2's HR chip differs, the closed-source algo lib is a hard blocker** (no source; would need the vendor lib or drop HR). **[needs-teardown-photo]** |
| Wiring | power `GPIO22`, ISR `GPIO74`, PPI trig 6 | confirm **[needs-NOR-dump]/[needs-teardown-photo]** |

### 1.5 Buttons / crown
| Aspect | EVB | GTX2 delta |
|---|---|---|
| Power/side key | **ONOFF key** via PMU (`KEY_POWER`), long-press cfg | reusable [known] |
| Extra key | one **GPIO key** on `GPIO24` (`KEY_TBD`) | GTX2 button GPIO map **[needs-NOR-dump]/[needs-teardown-photo]** |
| Crown/rotary | **none** — `CONFIG_INPUT_DEV_ACTS_KNOB=n` | GTX2 has a crown → enable `INPUT_DEV_ACTS_KNOB`, wire encoder GPIOs / ADC-knob. **[needs-teardown-photo]** |

### 1.6 Flash geometry + partition table  — must match retail exactly
EVB (`board_cfg.h` + `firmware.xml`): NOR **32 MB** chip (`0x2000000`), quad, 100 MHz, XIP
`0x12000000`, code base `0x10000000`, `CONFIG_FLASH_SIZE=1920` KB. EVB partition map:

| addr | size | type | name | file |
|---|---|---|---|---|
| 0x0 | 0x1000 | BOOT | fw0_boot | `mbrec.bin` |
| 0x1000 | 0x1000 | SYS_PARAM | fw0_param | `param.bin` |
| 0x4000 | 0x30000 | RECOVERY | fw0_rec | `recovery.bin` |
| 0x34000 | 0x10000 | DATA | coredump | — |
| **0x44000** | **0x197000** | **SYSTEM** | **fw0_sys** | **`app.bin`** (`ota_embed=TEMP`, CRC-only) |
| 0x1db000 | 0x20000 | DATA | fw0_sdfs | `sdfs.bin` |
| … | | (res / nvram partitions follow) | | |

**GTX2 delta [needs-NOR-dump]:** the retail app payload is ~2.08 MB (vs EVB `fw0_sys` 0x197000 ≈
1.6 MB), so **the retail partition offsets/sizes differ**. The custom app MUST be built to the retail
`fw0_sys` offset/size and the retail `mbrec`/`param`/`nvram`(calibration)/`res` partitions must be
**kept**. This is the single most important dump artifact: the retail partition table + the
keep-partitions. Realistic recipe: dump → build custom app to the retail SYSTEM layout →
programmer-flash **only** the app partition, retail everything-else, `golden.bin` as the net.

### 1.7 WTM2101 co-processor  — unknown, possibly from-scratch
The PoC lists a possible **WTM2101** co-processor (sensor/HR offload with its own firmware). **This
SDK has ZERO WTM2101 support** — no driver, no IPC, no references anywhere in `zephyr/`, `framework/`,
or `application/`. If the GTX2 routes the accel/HR through a WTM2101 rather than straight off the
ATS3085 I²C, then:
- the ATS3085↔WTM2101 IPC/handshake is **entirely unimplemented** → from-scratch, and
- raw-accel-over-BLE may require touching **WTM2101 firmware**, not just the ATS3085 app.

This can ~2× the effort and is the biggest open unknown. Resolved only by **[needs-NOR-dump]** (is
there a co-proc fw partition? does the sensor sit on an ATS3085 I²C bus?) + **[needs-teardown-photo]**
(is there a WTM2101 die on the board?).

### 1.8 Reusable as-is [known]
SoC series/`SOC_LEOPARD`, M33 core (FPU, Actions ARM MPU), `mbrec` (`libboot.a`), clocks (32 MHz
HOSC), UART0 console (`UART_0` @ 115200 / 2 Mbaud), PSRAM 4 MB @ `0x38000000`, SRAM 911 KB, PMU/ADC,
RTC, battery (internal coulometer), vibrator (PWM), NVRAM subsystem, LVGL + VGLite GPU + DE/LCDC/JPEG
HW, audio DAC (not watch-critical). These need at most minor cfg, not a port.

---

## 2. Effort shape

| Bucket | Effort | Gate |
|---|---|---|
| Build a custom app for the EVB target | **done** — proven, see `cfw-sdk-build.md` | none |
| Add LIS2DH12 accel driver (Actions HAL) | small (~1 day) | driver shape [known]; wiring [needs-dump] |
| Raw-accel BLE feature (see §3) | ~1–3 eng-days on a *booting* base | — |
| Panel bring-up (466×466, controller/interface) | medium–large | [teardown-photo] + [dump] |
| Touch + buttons + crown | small–medium | [teardown-photo] + [dump] |
| Partition/calibration reuse to retail layout | medium | **[NOR-dump] (hard dependency)** |
| HR/PPG (if chip ≠ HX3605) | large / possibly blocked (closed algo lib) | [teardown-photo] |
| WTM2101 IPC (if present) | large / from-scratch | [dump] + [teardown-photo] |

**Distance to a GTX2-booting custom FW is board bring-up + retail partition/calibration reuse,
gated on a NOR dump (#17 L3) and proven programmer recovery (#17 L4) — not the container, not
signing, not the build.**

---

## 3. Feature-relevant code map — raw-accel over BLE (issue #31)

Where the pieces live in the SDK, for adding a raw-XYZ notify characteristic (this refines
`custom-firmware-poc.md` §3; here the code map is confirmed against the Actions Zephyr SDK
source).

**BLE stack (3 layers):**
- **Host** (Zephyr-derived): `framework/bluetooth/bt_stack/` — `src/bt_stack/{gatt,att}.c`, standard
  services in `services/` (`bas,hrs,hog,dis,tps,ots`). Uses `BT_GATT_SERVICE_DEFINE` /
  `BT_GATT_CHARACTERISTIC` / `BT_GATT_CCC`.
- **Actions manager**: `framework/bluetooth/bt_manager/` — app-facing.
- **Custom app GATT service** (the template to copy): `bt_manager/bt_manager_super_service.c` —
  primary service **UUID 0xFFD0**, Control **0xFFD1** (WRITE), Status **0xFFD2** (**NOTIFY**+CCC),
  Test **0xFFD3** (NOTIFY|WRITE+CCC). Attr table at ~L308, registered via `ble_manager_super_register()`,
  example notify `bt_manager_ble_super_test_notify()`. *(This is the demo's example service — the
  retail Starmax/GB service UUID differs, but the mechanism is identical.)*
- **Notify send path**: `bt_manager/bt_manager_ble.c` — `bt_manager_ble_send_data(chrc_attr, …)` →
  `ble_notify_data()` → `hostif_bt_gatt_notify()`; pending/connected notifies via `os_delayed_work`.

**To add raw-accel:** add a NOTIFY characteristic (+CCC) to the super-service attr table, register it,
then push samples with `bt_manager_ble_send_data()`. Confirms the PoC verdict: **not** a binary patch
(needs new attr-table entry + CCC + notify buffers + a sampler) — an **SDK rebuild** feature.

**Sensor read path (how the app gets accel today):**
- App-facing manager: `framework/sensor/{sensor_manager,sensor_service}.c` (+ `include/sensor_manager.h`),
  results consumed via `sensor_algo.h`.
- **Pedometer/motion algo**: `zephyr/framework/sensor/sensor_algo/SensorAlgoMotion_Cywee/` (Cywee
  motion lib — steps/raise-wrist/sleep). HR algos under `SensorAlgoHR_*` (proprietary `.a`).
- **HAL** (chip-agnostic): `zephyr/framework/sensor/sensor_hal/sensor_hal.c` — API
  `sensor_hal_read(id, reg, buf, len)` (raw register read), `sensor_hal_get_value(id, dat, idx, *val)`
  (scaled XYZ), `sensor_hal_get_data(...)`.
- **Chip drivers**: `zephyr/framework/sensor/sensor_hal/devices/sensor_acc_{stk8321,qma6100}.c`,
  `sensor_hr_hx3605.c`, registered in `sensor_devices.c`.
- **Bus**: **I2CMT** = Actions "measurement-trigger" I²C with hardware auto-sampling tasks
  (`I2CMT0/1_TASKn`) fired by PPI triggers (accel PPI trig5, HR trig6) — see
  `sensor_hal/sensor_bus.c` (`device_get_binding("I2CMT_0"/"I2CMT_1")`). Raw XYZ is one
  `sensor_hal_read()`/`sensor_hal_get_value()` away once the chip driver is registered.

**App main loop / work queues:**
- Entry: `application/app_demo/lvgl_demo/src/main.c` — `main()` inits then a `while(1)` message loop
  (`app_msg` receive: `MSG_UI`, `MSG_OTA`) + `thread_timer_handle_expired()`.
- Work queues: `zephyr/framework/osal/{user_work_q,display_work_q}.c`; `os_delayed_work` /
  `thread_timer` are the periodic primitives. A raw-accel sampler = a `thread_timer`/`k_work` that
  reads the HAL and calls `bt_manager_ble_send_data()`.
