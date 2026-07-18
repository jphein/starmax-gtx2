# Custom-Firmware SDK build — PROVEN end-to-end (build → app.bin → flashable image)

**Date:** 2026-07-14 · **Mode:** software-only, **nothing flashed**.
**Result:** ✅ **We can build a custom Actions Zephyr image and package it into a `valid=True`
retail `FA EE EB DE` container.** The keystone software risk for issue #31 (option C, raw-accel
over BLE) is retired. What remains is board bring-up + the physical recovery gate (#17), not the
build.

> The SDK clone, toolchain, venv, and build output live **uncommitted** in
> `build/` (huge, must persist for later waves) — see paths below. This doc + `cfw-board-port.md`
> are the only committed artifacts.

---

## 1. Verdict

| Milestone | Status | Evidence |
|---|---|---|
| Clone SDK + recognise west workspace | ✅ | `west topdir`/`west list` OK against vendored manifest |
| Set up Zephyr 2.7.0-era toolchain (SDK 0.13.2) | ✅ | `arm-zephyr-eabi-gcc 10.3.0` |
| **`west build` the EVB LVGL demo → `zephyr.bin`** | ✅ | exit 0, `[932/932]`, artifacts below |
| SDK-native package `zephyr.bin` → `app.bin` | ✅ | `pack_app_image.py`, `ACTH/HTCA` head |
| Splice `app.bin` into retail container (`ota_repack.py`) | ✅ | CRCs recomputed, `@0x2C` magic preserved |
| **`otafmt.parse` accepts the result** | ✅ | `valid=True, section_magic_ok=True`, both CRCs stored==computed |

**Bottom line:** source → `west build` → `zephyr.bin` → `pack_app_image.py` → `app.bin` →
`ota_repack.py` → **structurally valid, flasher-accepted `FA EE EB DE` image**. The full pipeline
runs on this workstation from a public clone + a public toolchain. No signing barrier on the app
path (CRC-only, confirmed by the byte-map + otafmt verdict).

---

## 2. Artifacts + hashes (built 2026-07-14)

App built: **`application/app_demo/lvgl_demo`** for board **`ats3085s4_dev_watch_ext_nor`**
(Zephyr **2.7.0**, the demo that carries both the BLE stack and the sensor HAL — the right base
for the raw-accel feature).

| File | Size (B) | sha256 |
|---|---|---|
| `build-lvgl/zephyr/zephyr.bin` | 1,266,522 | `70add3b78c482bd520755f4d590d4323a7c48484436f3199128bab59c5b198ef` |
| `build-lvgl/zephyr/zephyr.elf` | 13,423,680 | `89a3314e155f94cf108d0fa6d60c6d197c9ffd91e070e7f8f87c1d90c7d7123f` |
| `pkg/app.bin` (SDK `pack_app_image.py`) | 1,266,688 | `d588b95a62cc711c6caee7b88b37a2053b739ba12ec4b855d5d371f56ff01b8e` |
| `pkg/out.ota` (repacked retail container) | 1,276,140 | `e49c48d8bd07ca079eaf712bfaec997b83edd4e97959c73e9403e48ba465cd85` |

`zephyr.elf` sections: **text 1,253,556 · data 12,824 · bss 1,015,790**.
`app.bin` image head: magic `ACTH`(`0x48544341`)/`HTCA`(`0x41435448`) @0x0/0x4, `run_addr 0x10024b35`
(flash base `0x10000000` + 512-byte reserved head, `CONFIG_ROM_START_OFFSET=0x200`).

**otafmt verdict on `out.ota`:**
```
valid = True    section_magic_ok = True
outer_crc @0x04 : stored 0x492EE978 == computed 0x492EE978
inner_crc @0x38 : stored 0x9DF398A4 == computed 0x9DF398A4
section_magic @0x2C = 0xA578875A  (left byte-identical from retail wrapper)
total_size stored == expected (1,276,184)
```
(`out.ota` is smaller than retail 2,148,064 B because `lvgl_demo` is smaller than the retail
`bt_watch` app — expected; `ota_repack.py` recomputed the length fields and warns they are
single-sample-inferred, unvalidated until a device-accept test. That caveat is orthogonal to the
build proof.)

---

## 3. Environment (all under a working dir you pick, `$BUILD` — e.g. `~/gtx2-cfw`; large, keep it outside the repo)

| Component | Version / path |
|---|---|
| SDK clone | `$BUILD/actions-sdk/` — `github.com/lvgl/lv_port_actions_technology` (shallow `--depth 1`); Zephyr **2.7.0**; west topdir = `actions-sdk/action_technology_sdk/` |
| Zephyr SDK toolchain | **0.13.2**, `arm-zephyr-eabi-gcc 10.3.0` → `$BUILD/zephyr-sdk-0.13.2/` |
| Python venv | `$BUILD/cfw-venv/` — Python 3.12.3, **west 1.5.0**, `setuptools<81`, `pyelftools PyYAML pykwalify canopen packaging progress psutil pylink-square anytree intelhex` |
| Host tools | cmake 3.28.3, ninja 1.11.1, gperf, dtc (build used the SDK's bundled **dtc 1.6.0**) |
| Reusable env script | `$BUILD/build-env.sh` (source it to reproduce) |

`build-env.sh` exports:
```
source $BUILD/cfw-venv/bin/activate
export ZEPHYR_TOOLCHAIN_VARIANT=zephyr
export ZEPHYR_SDK_INSTALL_DIR=$BUILD/zephyr-sdk-0.13.2
export ZEPHYR_BASE=$BUILD/actions-sdk/action_technology_sdk/zephyr
export SETUPTOOLS_USE_DISTUTILS=local           # py3.12 distutils shim
cd $BUILD/actions-sdk/action_technology_sdk
```

---

## 4. Exact repeatable recipe

```bash
BUILD=~/gtx2-cfw                 # a working dir you pick (large; keep it outside the repo)
mkdir -p "$BUILD/pkg"

# 0) clone (huge; NOT in the repo tree)
git clone --depth 1 https://github.com/lvgl/lv_port_actions_technology.git $BUILD/actions-sdk

# 1) toolchain: Zephyr SDK 0.13.2 ARM only (150 MB vs 1.18 GB full)
curl -L -o $BUILD/zephyr-toolchain-arm-0.13.2.run \
  https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v0.13.2/zephyr-toolchain-arm-0.13.2-linux-x86_64-setup.run
# the makeself --target extraction misbehaves here; extract the payload manually:
offset=$(head -n 521 $BUILD/zephyr-toolchain-arm-0.13.2.run | wc -c)
tail -c +$((offset+1)) $BUILD/zephyr-toolchain-arm-0.13.2.run | gzip -cd | tar xf - -C $BUILD/sdk-raw
bash $BUILD/sdk-raw/setup.sh -d $BUILD/zephyr-sdk-0.13.2 -y -norc -nocmake

# 2) python env
python3 -m venv $BUILD/cfw-venv && source $BUILD/cfw-venv/bin/activate
pip install west "setuptools<81" pyelftools PyYAML pykwalify canopen packaging \
            progress psutil pylink-square anytree intelhex

# 3) build (env from build-env.sh)
source $BUILD/build-env.sh
west build -b ats3085s4_dev_watch_ext_nor application/app_demo/lvgl_demo \
  --build-dir $BUILD/build-lvgl -- -DEXTRA_CFLAGS=-Wno-error       # -> zephyr/zephyr.bin

# 4) package + prove flashable pipeline
cp $BUILD/build-lvgl/zephyr/zephyr.bin $BUILD/pkg/app.bin
python -B zephyr/tools/pack_app_image.py $BUILD/pkg/app.bin    # -> Actions image head
# stock.ota = your own dumped stock OTA image
python scripts/ota_repack.py stock.ota \
       $BUILD/pkg/app.bin $BUILD/pkg/out.ota
# otafmt.parse(out.ota) -> valid=True, section_magic_ok=True
```

---

## 5. Toolchain fights (resolved) + one non-blocking gotcha

The task time-boxed the 2.7.0 toolchain fight. Two real snags, both cleanly resolved; the build is
**not** an unbounded rabbit hole.

1. **`ModuleNotFoundError: distutils`** — Zephyr 2.7.0 scripts (`gen_kobject_list.py`) do
   `from distutils.version import LooseVersion`; `distutils` was removed from Python 3.12 stdlib
   (PEP 632). **Fix:** `pip install "setuptools<81"` + `export SETUPTOOLS_USE_DISTUTILS=local`
   (activates setuptools' distutils shim). `python3.10` is present at `~/.local/bin` as a fallback
   if a deeper py3.12 issue ever surfaces; it did not.
2. **`-Werror=unused-variable`** in the vendored LVGL file
   `thirdparty/lib/gui/lvgl/src/draw/actions/lv_draw_actions.c:253` (`void * ptr = NULL;`) — GCC
   10.3.0 is stricter than the vendor's original compiler. **Fix:** `-DEXTRA_CFLAGS=-Wno-error`
   (standard when building a vendor SDK with a newer toolchain; only overrides the vendor's warning
   escalation, changes no code).

**Non-blocking gotcha — `west update` cannot run and is unnecessary.** The manifest
(`zephyr/west.yml`) points every module to a **private Actions Gerrit** (`ssh://192.168.4.4:29418/…`,
unreachable). But the repo is a **self-contained monorepo**: all module paths (LVGL 2783 files,
bluetooth 282, sensor HAL, mbedtls, fatfs, …) are **vendored in-tree**, no submodules. `west build`
resolves modules from the working tree via `west list`, so it needs no fetch. A bounded
`west update` attempt DID create stray empty `.git` dirs in `thirdparty/hal/cmsis` +
`thirdparty/fs/fatfs` — these were removed to restore the pristine tree (files intact). **Do not run
`west update`** on this SDK.

---

## 6. What this proves / doesn't

- **Proves:** the SDK is a real, buildable Zephyr 2.7 for the `ats3085s4` (leopard M33) board on a
  public toolchain; the SDK's own packaging (`pack_app_image.py`) produces a valid app image; and
  that app image splices into a retail `FA EE EB DE` container the merged `#29` flasher/parser
  accepts as `valid=True`. This is the entire **software** half of custom FW for issue #31.
- **Does NOT prove:** that this EVB image *boots correctly on the retail GTX2* — that is board
  bring-up (panel/touch/sensor/GPIO/partition/calibration), scoped in `cfw-board-port.md`; nor does
  it lift the physical recovery gate (#17). Per the PoC doc, `@0x2C` is a fixed Actions sub-image
  magic (not a checksum), left byte-identical, and not a device-enforcement risk.
