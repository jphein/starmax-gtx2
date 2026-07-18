# starmax-client

A **standalone** Python BLE client for the **Starmax GTX2** (Runmefit) smartwatch, runnable
from this Linux desktop. It speaks the watch's custom BLE command protocol directly (GATT service
`0x0FF0`, **not** Nordic UART) — no vendor app, no cloud account (see [Bind / auth](#bind--auth-dependency)).

> This is an independent, hobbyist tool. It is **separate** from the Gadgetbridge coordinator work under
> `../Gadgetbridge/` and is **not** part of that PR. The protocol it implements is decoded in
> [`../docs/protocol-spec.md`](../docs/protocol-spec.md) (authoritative) and
> [`../docs/decode-notes.md`](../docs/decode-notes.md).

> **📖 Full command reference:** [`docs/command-reference.md`](docs/command-reference.md) — the
> **8 core verbs** + **40 auto-discovered module commands across 6 groups**, each with
> `[CAP]`/`[SCHEMA]` confidence tags, wire opcodes, and examples. Run `commands --list` for the
> always-authoritative set. **481 offline tests** green.
>
> 🖥️ Big picture: the [project README](../README.md) and the live
> [status &amp; control dashboard](https://jphein.github.io/starmax-gtx2/) (`docs/index.html`).

## What it does

| CLI command | Wire opcode | Status | Purpose |
|---|---|---|---|
| `scan` | — | ✅ live | Find advertising `GTX2-*` watches |
| `pair` | `0x01` | ✅ live | Connect + run the bind handshake, print the device descriptor |
| `activate` | multi | ✅ live | **Full setup handshake** — take a fresh watch off its "pair with the app" screen |
| `set-time` | `0x02` | ✅ live | Set the watch clock to now (local timezone) |
| `find` | `0x18` | ✅ live | Ring/buzz the watch |
| `sync-health` | `0x0e` flag=1 | ✅ live | Pull health/history records (HR/activity, SpO2, sleep) |
| `monitor` | `0x0e` (stream) | ⚠️ schema | Live telemetry: realtime sensor stream (accel/HR/…) + link RSSI/MTU |
| `raw-accel` | `0xA0` (CFW) | 🧪 custom-fw | Raw LIS2DH12 XYZ stream — **needs custom firmware** (issue #31); `--decode`/`--dry-run` work offline. Spec: [`docs/cfw-rawaccel-protocol.md`](../docs/cfw-rawaccel-protocol.md) |
| `dial-build` | — (offline) | ✅ live | Transcode a dial `.bin` (ZIP of images) into the native container |
| `dial-push` | bulk plane | ✅ live | Install a custom watch face and auto-activate it (live-validated) |
| `dial-list` | `0x16` | ✅ live | Read the installed dial/resource list + storage |
| `weather` | `0x12` | ✅ live | Push current weather (temp/condition/hi-lo + hourly/daily); auto-sends the `0x04` feature-enable (`--no-enable` to skip) |
| `notify` | `0x11` / `0x13` | ⛔ tabled | Build/send a notification frame — **display needs a classic-BT companion** ([#5]) |

Full surface (profile, alarms, reminders, display toggles, DND, realtime health, …) is under
**auto-discovered command modules** — see the [feature-status matrix](#feature-status-matrix) and
[`docs/command-reference.md`](docs/command-reference.md).

[PR #3]: https://github.com/jphein/starmax-gtx2-client/pull/3
[#5]: https://github.com/jphein/starmax-gtx2-client/issues/5

## Command modules (full command set)

Beyond the core verbs above, the client exposes a much larger command surface split into
**auto-discovered modules** under `starmax_client/commands/`. Each module (`health`, `settings`,
`notify`, `files`) contributes a group of subcommands that the CLI wires in automatically — drop
a new `commands/<group>.py` and it appears with no change to the CLI.

List everything registered, grouped by module:

```bash
./venv/bin/python -m starmax_client commands --list
```

Every module subcommand supports **`--dry-run`** — it builds the frame and prints it as hex
**without connecting to any watch**, so the full surface is safe to explore offline:

```bash
./venv/bin/python -m starmax_client health-switch-read --dry-run
./venv/bin/python -m starmax_client history-sync 2 --dry-run          # SpO2 history request
```

Groups (run `commands --list` for the live, authoritative set):

| Group | Examples | Confidence |
|---|---|---|
| `health` | `health-switch-read/write`, `history-sync`, `hr/spo2/sleep-history`, `realtime-open/measure` | HR/SpO2/sleep `[CAP]`; realtime + extra metrics `[SCHEMA]` |
| `settings` | `user-profile`, `alarm-get/set`, `dnd`, `sedentary`, `drink-water`, `world-clock`, `aod`, `date-format` | profile/alarms `[CAP]`; reminders/world-clock `[SCHEMA]` |
| `notify` | `notify-detailed/summary`, `call`, `music`, `camera` | notifications `[CAP]`; call/music/camera `[SCHEMA]` |
| `files` | `dial-list`, `dial-switch`, `nfc-list`, `sport-control`, `ota-preview`, `send-file` | dial-list `[CAP]`; transfers are preview/plan builders |

> **Confidence tags** in each builder's docstring: `[CAP]` = byte-shape confirmed against a real
> capture; `[SCHEMA]` = built from the APK protobuf schema with the wire opcode **unresolved**
> (never seen on the wire) — experimental, prefer `--dry-run` until validated on hardware.

### Adding a command group (module contract)

A `commands/<group>.py` module is auto-discovered when it defines:

- **`COMMANDS: dict[str, builder]`** — command name → a builder returning a complete app→watch
  frame (`bytes`) or a list of frames. This is the catalog `commands --list` prints and the
  dry-run smoke test exercises as its coverage gate.
- **`register(subparsers, client=None)`** — add the group's argparse subcommands; each must
  support `--dry-run`. Handlers read the live client from `args._client` (the CLI injects a
  connected+bound client for non-dry-run runs).
- optional **`GROUP`** (display name), **`PARSERS`** (reply decoders), and **`SMOKE_ARGS`**
  (`{command: kwargs}` sample args for builders whose required args aren't trivially sampled).

Import the shared core **absolutely** (`from starmax_client import framing`), never by relative
path. The discovery/registry API lives in `starmax_client/commands/__init__.py`;
`commands/health.py` is the reference implementation.

## Feature-status matrix

Where each capability stands after this cycle's work. Legend:
**✅ Works live** = validated on real hardware ·
**⚙️ Needs-prereq** = works once a setup step is met ·
**🧪 Live-test pending** = built and byte/dry-run verified, on-device confirmation still pending ·
**🔬 Schema-unverified** = built from the APK protobuf schema with the **wire opcode unresolved** (never seen on the wire) — experimental ·
**⛔ Tabled** = deferred, see the linked issue.

| Feature | Status | Prereq / notes |
|---|---|---|
| Scan · bind (`pair`) · `activate` handshake | ✅ Works live | LE-only adapter ([below](#linux--bluez-force-the-adapter-le-only-required-for-live-use)) |
| Set time (`set-time`) | ✅ Works live | — |
| Ring / find the watch (`find`) | ✅ Works live | — |
| Health sync — HR/activity, SpO2, sleep records | ✅ Works live | **consume-on-read** + **step total not in the record** — see [findings](#health-sync-findings) |
| **Watch faces** — `dial-build` → `dial-push` → auto-activate | ✅ Works live | live-validated ([#1]/[#2]); ZIP input needs the `transcode` extra |
| Dial / resource list + active dial + storage (`dial-list`) | ✅ Works live | — |
| **Weather push** (`weather`, `0x12`) | ✅ Works live | validated on-device (widget showed the sent hi/lo). Auto-sends the `0x04` feature-enable by default so it Just Works (`--no-enable` to skip) |
| User profile · step/distance goals · alarms | ✅ Works live | profile+alarms `[CAP]`; profile pushed during `activate` |
| **Notifications** (`notify`, `0x11`/`0x13`) build+send | ⛔ Tabled | frame is byte-correct, but **display requires a connected classic-BT companion** (**[#5]**). The LE enable exchange (`0x04`+`0x03`) landed in **PR #4** — necessary, **not sufficient** on LE-only. |
| `monitor` / `realtime-open`/`-measure` | 🔬 Schema-unverified | the watch has **no live sensor stream** — all health is polled (`0x0e` history); these SDK-derived "realtime" opcodes are UNRESOLVED, `--dry-run` safe. Audit: **[#6]** |
| Reminders · DND · world-clock · AOD · display toggles | 🔬 Schema-unverified | body is schema-exact; opcode is a best-effort carrier. Audit: **[#6]** |
| Call · music · camera | 🔬 Schema-unverified | on Android these ride **classic BT** (HFP/HID), not this channel |
| NFC list · sport-control · file/OTA transfer | 🔬 preview | plan/preview builders; bulk-plane transfers |

[#1]: https://github.com/jphein/starmax-gtx2-client/pull/1
[#2]: https://github.com/jphein/starmax-gtx2-client/pull/2
[#6]: https://github.com/jphein/starmax-gtx2-client/issues/6

## Feature notes

### Watch faces (live-validated)

Installing a custom face is a two-step, capture-derived flow (details in
[`../docs/watchface-install.md`](../docs/watchface-install.md)):

1. **Transcode** the distributed dial `.bin` (a ZIP of `dial.json` + PNG/BMP assets) into the flat
   **native container** the watch actually streams — `dial-build <zip> <out>` (offline), or let
   `dial-push` do it on the fly. Each image asset is re-encoded as `<type:1><(h<<13)|(w<<2):u24 LE>`
   + `lz4.block(pixels)` (`0x18` = RGBA8888, `0x04` = RGB565-LE); implemented in
   `starmax_client/dialtranscode.py` (gated behind the optional `transcode` extra: Pillow + lz4).
2. **Push** it over the **bulk plane** (`D1`→`D2`→`D4` on the `0x0FF0` handles) with
   `dial-push <zip|blob>`, which installs and auto-activates. Reliable delivery uses ATT
   Write-**With**-Response (`send_raw(response=True)`) — fire-hosing Write-Without-Response overran
   the watch and dropped the tail (a 231 KB push stalled at ~84 KB and failed the finalize CRC).

```bash
./venv/bin/python -m starmax_client dial-build myface.bin myface.native   # offline transcode
./venv/bin/python -m starmax_client dial-push  myface.bin                 # transcode + install + activate
```

### Weather (`0x12`)

`weather` pushes current conditions (protocol-spec §3.7). All args are optional with PII-free
defaults, so `weather --dry-run` prints a valid frame with no watch:

```bash
./venv/bin/python -m starmax_client weather --temp 25 --condition 6 --city Anytown
./venv/bin/python -m starmax_client weather --temp 25 --hi 30 --lo 18 --dry-run
./venv/bin/python -m starmax_client weather --temp 25 --condition 6 --no-enable   # skip the auto 0x04 enable
#   optional: --hourly 31/21,31/20,... (max 24)   --daily 33/22/6,... (max 3)   --pressure 1013.25
```

**Live-validated** — pushed 25 °C / 18–30 hi-lo / clear and the watch's weather widget showed
`18/30°C`, over LE (no classic-BT companion needed, unlike notifications).

**Enable (default-on):** display is gated on the `0x04` feature-enable, so `weather` **auto-sends
the `0x04` first by default** and Just Works whether or not the watch has been `activate`-d. It's
safe to send unconditionally — only the `0x04` (no `0x03`), so it does **not** reset the profile the
way notify's enable can (that's why notify's enable is opt-in, but weather's is default-on). Pass
`--no-enable` to send the `0x12` weather frame alone.

Notes: the builder is a **faithful subset** of the vendor frame — the solid fields
(month/day/time, current condition, temp, hi/lo, city, hourly[24], daily[3], pressure) are encoded;
unresolved units/AQI/UV sub-fields are omitted (so frames are ~40–220 B vs the vendor's ~366 B). It
delegates to `base.build_weather` — byte-parity with the Gadgetbridge `StarmaxMessages.buildWeather`.
Only condition code `6` (clear) is capture-confirmed; `--condition` takes the watch's raw 1-based
code. Ships in **[PR #3]**.

### Health-sync findings

Two behaviours to know when pulling `0x0e` flag=1 history (both verified on-device):

- **Consume-on-read.** A detail record is handed over **once**; a re-read of the same category
  returns only a ~55-byte **status stub**, not the detail again. Treat a sync as draining — persist
  what you read, don't expect to re-fetch the same detail bytes.
- **The daily step total is in the category-5 activity record** (live-confirmed 2026-07-12) — the
  `u32` at **date-marker+6** (162 & 225 matched the watch face). The head-of-record counters in
  **cat 0** are HR, not steps (the earlier `offset-36`→steps guess *there* was retracted); once the
  category map was corrected (activity = cat 5, not the old "cat 5 = sleep"), steps decode cleanly.
  Distance/calories (adjacent u32s) remain PROVISIONAL. Full decode:
  [`../docs/health-metrics.md`](../docs/health-metrics.md).

### Notifications — tabled (needs a classic-BT companion)

The client builds and sends the `0x11`/`0x13` notification frames **byte-identically** to captured
frames that displayed — but on an **LE-only** link the watch shows *"app not connected"* and
suppresses them. Root cause ([**#5**], HCI forensics + live test): notification display is gated on
the watch having a connected **classic-Bluetooth** companion (HFP/RFCOMM); the vendor app runs
entirely over classic BT. An LE-only client can't be that companion. The LE-side enable exchange
(`0x04` feature bitmap + `0x03` toggles) is correct and wired (**PR #4**) but not sufficient. Fixing
this needs GATT-over-BR/EDR (classic ATT) or a separate HFP-AG companion adapter — **tabled**;
notifications are expected to come from the watch's phone companion. Everything else (watch faces,
health sync, weather, activation) is LE-only and unaffected.

## Install

Python 3.10+. The only runtime dependency is [`bleak`](https://github.com/hbldh/bleak) (the
standard cross-platform BLE library); everything else is pure stdlib.

### As a tool (recommended) — `pipx`

Puts the **`starmax`** command on your PATH, isolated in its own venv:

```bash
cd starmax-client
pipx install .                 # the `starmax` command (all BLE features)
pipx install '.[transcode]'    # ALSO enables dial authoring (dial-build/dial-push of a ZIP): Pillow + lz4
```

Then run it directly (no venv activation, no `python -m`):

```bash
starmax --help
starmax scan
starmax weather --temp 22 --condition 6 --city Anytown
```

`pip install .` (or `pip install -e .` into an active venv) works too. `starmax-client` is kept as
a legacy alias for `starmax`.

### For development (editable)

```bash
cd starmax-client
python3 -m venv venv
./venv/bin/pip install -e '.[test,transcode]'   # editable + pytest + dial authoring
./venv/bin/starmax --help
./venv/bin/python -m pytest                      # offline suite
```

The **`transcode`** extra (Pillow + lz4) is needed only to build/push a custom watch face from a
dial ZIP; the core client and `dial-push` of an already-native blob need neither.

## Linux / BlueZ: force the adapter LE-only (required for live use)

The GTX2 is **dual-mode** (BR/EDR + LE). Its command service (`0x0FF0`) lives on the **LE**
transport, but BlueZ left to itself often connects the **classic (BR/EDR)** transport first — so
`bleak` connects yet the GATT command service is absent and every live command times out. Android
and Gadgetbridge force an LE connection automatically; on Linux you must do it by hand:

```bash
sudo btmgmt power off && sudo btmgmt bredr off && sudo btmgmt power on
```

This disables BR/EDR on the adapter so BlueZ can only connect over LE. Verify with `btmgmt info`
(BR/EDR should be gone from `current settings`). Restore dual-mode later with `sudo btmgmt bredr on`
(or reboot). Do this **before** `pair` / `activate` / any live command — it is the single most
common reason a live connection "succeeds" but no command gets through.

To make it persistent, set BlueZ to LE-only in `/etc/bluetooth/main.conf`:

```ini
[General]
ControllerMode = le
```

**The dual-mode trap (why this is mandatory):** the GTX2 is dual-mode with **two separate
addresses** (a classic BR/EDR address distinct from its LE address). On a single dual-mode adapter,
BlueZ grabs the classic transport first → **0 LE services, the `0x0FF0` notify characteristic is not
found**, and every LE command fails. LE-only `ControllerMode` forces the LE transport so the command
service is reachable. (This is also why notifications are tabled — the watch wants a *classic*
companion for those; see [#5] — you can't hold both on one adapter.)

**Coexistence (single-owner peripheral):** the GTX2 accepts **one central at a time** — while a
phone holds it, the watch **stops advertising**, so this CLI cannot discover or connect. Gadgetbridge
(phone) and the CLI (Linux) **time-share** the watch, never concurrently; disconnect one before using
the other. (See [`../docs/health-metrics.md`](../docs/health-metrics.md).)

## Usage

```bash
# Scan for the watch (8s by default)
./venv/bin/python -m starmax_client scan

# Connect + bind, print firmware / chipset / screen / MAC
./venv/bin/python -m starmax_client pair --address AA:BB:CC:DD:EE:FF

# Full setup handshake — takes a fresh/factory-reset watch off its "pair with the app" screen:
./venv/bin/python -m starmax_client activate

# If you omit --address, the tool scans and uses the first GTX2-* it finds:
./venv/bin/python -m starmax_client set-time
./venv/bin/python -m starmax_client weather --temp 22 --condition 6 --city Anytown   # push weather (PR #3)
# notify builds+sends a byte-correct frame, but DISPLAY needs a classic-BT companion (tabled, #5):
./venv/bin/python -m starmax_client notify "Build finished" --body "All tests green"
./venv/bin/python -m starmax_client notify "3 new messages" --summary
./venv/bin/python -m starmax_client find --duration 5      # buzz for 5s
./venv/bin/python -m starmax_client find --stop            # stop buzzing
./venv/bin/python -m starmax_client sync-health            # all categories
./venv/bin/python -m starmax_client sync-health --category 2   # SpO2 only

# -v adds debug logging (frame-level recv traces)
./venv/bin/python -m starmax_client -v pair
```

`sync-health` prints one line per category (date, byte size, present flag) and **does not**
dump biometric sample bytes unless you pass `--raw`.

## Architecture

Transport-independent codec (pure stdlib, fully unit-tested) + a thin bleak transport:

```
starmax_client/
  crc.py          CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF; check 0x29B1)
  protobuf.py     minimal protobuf writer/reader (varint, len-delim, i32/i64)
  framing.py      0xC1 frame codec + 0xC3 fragment reassembly (direction-aware LEN/CRC)
  commands/       command groups (auto-discovered; see "Command modules" above):
    __init__.py   discovery + registry (COMMANDS/register contract) + legacy re-export
    base.py       core builders (bind, set-time, notify, weather, health-sync, find, alarms)
    health.py     health switches/history/realtime + parsers (HR, SpO2, sleep, …)
    settings.py   profile, alarms, DND, reminders, world-clock, display toggles
    notify.py     app notifications, incoming call, music, camera
    files.py      dial list/switch, file/OTA transfer plans, sport, NFC
    dials.py      dial activate (switch the installed face by id)
    weather.py    weather push (0x12) — CLI wrapper over base.build_weather ([PR #3])
  dialtranscode.py  transcode a dial ZIP -> native container (Pillow+lz4; `transcode` extra)
  records.py      0x0e flag=1 binary health-record header decode (date/category)
  transport.py    bleak BLE transport (custom 0x0FF0 service; scan, connect, reassembly,
                  fragmented C1/C3 writes, ATT write-with-response for the bulk plane)
  cli.py          argparse CLI: core verbs + runtime auto-discovery/wiring of command modules,
                  plus `monitor` (polls health categories + link RSSI/MTU; no live stream)
```

### Frame envelope (from `docs/protocol-spec.md` §1)

```
off 0     0xC1 SOF (0xC3 = continuation fragment)
off 1     seq   (watch->app frames OR-in 0x80)
off 2     dir   0x01 app->watch, 0x00 watch->app
off 3     0x01  protocol version
off 4     flag  per-opcode sub-type
off 5     opcode
off 6-7   LEN   16-bit little-endian
off 8-10  00 00 00 reserved
off 11..  payload (protobuf; or binary record for 0x0e flag=1)
[tail]    CRC-16/CCITT-FALSE, little-endian — watch->app protobuf frames ONLY
```

Key rule the codec enforces: **outgoing (app→watch) frames carry NO CRC** and `LEN = total`;
**incoming protobuf frames carry a 2-byte CRC** and `LEN = total − 2`; the `0x0e` flag=1
binary health records carry no CRC in either direction.

## Offline tests

The whole suite is offline — no BLE, no `bleak` import. It validates the codec against **real
frames captured from the watch** (`../captures/*.log`), plus the canonical CRC check value.

```bash
cd starmax-client
./venv/bin/python -m pytest        # full offline suite (grows as command modules are added)
```

Coverage:
- **CRC** — canonical `crc16("123456789") == 0x29B1`, the spec §1.2 reference frame, and
  little-endian storage; verified against a real watch→app reply trailer.
- **protobuf** — varint round-trips (incl. a real set-time epoch), field-order preservation,
  nested messages, UTF-8, truncation errors.
- **framing** — header layout, 16-bit LE length, CRC verify/reject on a **real** `0x22` reply,
  binary-record (no-CRC) handling, and 0xC3 reassembly of the **real fragmented `0x16`
  dial-list reply** (C1 240 B + C3 → 255 B, CRC OK) plus a synthetic split/reassemble.
- **commands** — every builder reproduces its **real captured frame byte-for-byte**
  (bind, set-time, find on/off, alarm get/set, health-sync cat 0/5), plus structural checks
  for notifications and weather.
- **integration** — a dry-run smoke gate (`tests/test_integration_smoke.py`) discovers every
  command-group module and asserts each registered builder produces a structurally valid
  app→watch frame with no BLE; it grows automatically as `commands/<group>.py` modules land.

Test vectors live in `tests/fixtures.py` with per-frame provenance; all are PII-free by
construction (control frames, a setting value, and published watch-face filenames only).

## Bluetooth adapter (this machine)

Verified present and working via bleak on the dev host:

```
hci0  Intel Corp.  BD XX:XX:XX:XX:XX:XX  Bluetooth 5.2  UP RUNNING
```

A 3-second `scan` through the client's own code path saw 12 advertisers, confirming the
adapter + bleak path is functional. Good to go for the live test once the watch is present.

## LIVE test procedure (hardware-gated)

The LE feature set has been exercised on real hardware: bind/`activate`, `set-time`, `find`,
`sync-health`, `dial-list`, and a full **watch-face install** (`dial-push`) are **validated**
on-device. Still pending an on-device pass: **weather** ([PR #3]) and the `[SCHEMA]` commands
(audit [#6]). Notifications are **tabled** ([#5]). The procedure below reproduces a full run.

> **Prerequisite (Linux/BlueZ):** apply the [LE-only workaround](#linux--bluez-force-the-adapter-le-only-required-for-live-use)
> first, or BlueZ connects the classic transport and the `0x0FF0` command service is missing.
> For a fresh watch, `activate` (not just `pair`) is what clears the "pair with the app" screen.

1. **Power the watch and bring it near the host.** If it was previously bonded to the phone,
   forget it there first (or it may not accept a new central).
2. **(If the watch demands OS-level BLE bonding)** pair once via BlueZ:
   ```bash
   bluetoothctl        # then: scan on; pair <MAC>; trust <MAC>; quit
   ```
   The custom vendor bind (wire `0x01`) is separate from OS bonding; most GTX2 flows do not
   require bonding, but do this if `connect` fails with an auth/insufficient-encryption error.
3. **Discover it:** `./venv/bin/python -m starmax_client scan` → note the `GTX2-xxxx` address.
4. **Bind:** `./venv/bin/python -m starmax_client pair --address <MAC>` → expect firmware
   build, chipset `UC6228CI`, screen `466 x 466`, and the device MAC.
5. **Exercise commands** and confirm on the watch face:
   - `set-time` → watch clock updates.
   - `find --duration 5` → watch buzzes ~5s.
   - `notify "hello from the host" --body "live test"` → notification shows.
   - `sync-health` → dated records per category (0 activity/HR, 2 SpO2, 5 sleep).
6. **Capture while testing** (optional, to confirm bytes match the decode):
   run `hcidump`/`btmon` or Wireshark on `hci0` and compare against `../captures/`.

Report anomalies against `docs/protocol-spec.md` — the builders are byte-verified against the
phone-app captures, so a divergence means either a firmware difference or a spec gap.

## Bind / auth dependency

**Question: does the watch bind without the vendor (Runmefit) cloud account?**

**Capture-based answer: yes, the BLE bind appears accountless and authless.** In the pairing
capture the watch was *"never paired, no account"* (`docs/capture-guide.md`), yet the full
bind handshake completed: wire `0x01` with an **empty payload** → device descriptor, then
`0x22`/`0x05`/`0x16`/`0x02`/`0x0e` (`protocol-spec.md` §4). There is **no token, challenge, or
account credential on the C1 channel**, and the command channel is plaintext protobuf (no comms
encryption — `decode-notes.md`). Account creation in the app is *"network only; not
BLE-relevant."* So a standalone client should be able to bind and drive the watch with no
vendor account.

**Caveats:** the RunmeFit SDK names its bind step `writeDeviceBind` (result key
`ST_DeviceBindKey`) and a *separate* `writeDevicePair` for OS BT pairing
(per the vendor SDK opcode map) — so a bind *key* may be exchanged at a layer we did not see
carry data on the wire, and OS-level bonding may still be required by some firmware. This is
**pending confirmation from the vendor APK reverse-engineering** (internal RE notes,
not present at build time). If that analysis shows an
app-layer token gate on bind, this section and the `pair` flow must be revisited.

## Privacy

No PII or secrets are committed. Captured personal data (watch MAC, notification text,
biometrics, caller ID) is never baked into code or tests; runtime output that shows your own
device's MAC/health data stays local. `sync-health` withholds biometric bytes unless `--raw`.
