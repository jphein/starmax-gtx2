# starmax-client — full command reference

Every command the standalone client exposes: the **8 interactive core verbs** + the
auto-discovered module commands — **36 today across 5 groups** (dials / files / health / notify /
settings), **37 across 6** once the `weather` group lands ([PR #3]). For where each capability
stands (live / pending / tabled / schema-unverified), see the
[feature-status matrix](../README.md#feature-status-matrix) in the README. This is the human-readable
companion to the live, always-authoritative listing:

```bash
./venv/bin/python -m starmax_client commands --list
```

All examples assume the venv (`./venv/bin/python -m starmax_client …`). **Every module command
supports `--dry-run`** — it builds the frame and prints it as hex without connecting to any watch,
so the whole surface is safe to explore offline.

---

## Confidence tags

Each builder's docstring (and the `--list` output) carries a confidence tag. **Read it before
trusting a command against real hardware.**

| Tag | Meaning |
|---|---|
| **`[CAP]`** | Byte-shape **confirmed against a real capture** frame (cited to `../docs/protocol-spec.md` §). The wire opcode and payload are what the vendor app actually sent. Safe to send. |
| **`[SCHEMA]`** | Payload built faithfully from the vendor APK protobuf schema, but the **wire opcode is UNRESOLVED** — the feature never appeared in a capture, and per protocol-spec §1.4 the SDK's high-level opcodes are *not* guaranteed to be the wire opcodes. The message *body* is schema-exact; the framing is a best-effort carrier. **Experimental — prefer `--dry-run`.** |
| **`[SCHEMA/INFERRED]`** / **`[SCHEMA, UNVERIFIED]`** | Same as `[SCHEMA]`: schema-derived, opcode/behaviour inferred, not hardware-validated. |

> Rule of thumb: `[CAP]` commands are real; `[SCHEMA]` commands are educated experiments you should
> `--dry-run` and, ideally, confirm with a live capture before relying on.

---

## Core verbs (interactive; top-level)

| Verb | Wire opcode | Tag | Purpose |
|---|---|---|---|
| `scan` | — | — | Find advertising `GTX2-*` watches |
| `pair` | `0x01` | [CAP] | Connect + bind handshake, print the device descriptor (fw build, chipset `UC6228CI`, screen, MAC) |
| **`activate`** | multi | [CAP] | **Full setup handshake** — takes a fresh/factory-reset watch **off its "pair with the app" screen** (see below) |
| `set-time` | `0x02` | [CAP] | Set the watch clock to now (local timezone) |
| `notify` | `0x11`/`0x13` | [CAP] | Push a notification (`--body` for detailed; `--summary` for a count line) |
| `find` | `0x18` | [CAP] | Ring/buzz the watch (`--duration N`, `--stop`) |
| `sync-health` | `0x0e` f1 | [CAP] | Pull health/history records (`--category N`, `--raw`) — see [findings](#health-sync-findings) |
| `monitor` | `0x0e` | [SCHEMA] | Poll loop over health categories + link RSSI/MTU — the watch has **no** live sensor stream (all data is polled; see `../../docs/health-metrics.md`) |
| **watch-face verbs** | `0x16` / bulk | [CAP] | `dial-list` · `dial-build` · `dial-push` · `dial-activate`/`dial-switch` — see [Watch faces](#watch-faces--bulk-transfers) |
| `weather` | `0x12` | [CAP] | Push current weather ([PR #3]) — see [weather group](#weather-group-1--0x12-push) |
| `commands --list` | — | — | Print every auto-discovered module command, grouped, with a sample frame |

[PR #3]: https://github.com/jphein/starmax-gtx2-client/pull/3
[#5]: https://github.com/jphein/starmax-gtx2-client/issues/5

```bash
./venv/bin/python -m starmax_client scan
./venv/bin/python -m starmax_client pair --address AA:BB:CC:DD:EE:FF
./venv/bin/python -m starmax_client activate                 # off the pairing screen
./venv/bin/python -m starmax_client set-time
./venv/bin/python -m starmax_client notify "Build green" --body "142 tests passed"
./venv/bin/python -m starmax_client find --duration 5
./venv/bin/python -m starmax_client sync-health --category 2 # SpO2 only
```

### The `activate` handshake (what it sends)

`activate` replays the **captured, byte-faithful** first-pairing sequence the Runmefit app uses,
so a fresh watch accepts the client as its companion and leaves the pairing screen. It mirrors the
Gadgetbridge coordinator's `initializeDevice`. Order (each step a real opcode):

1. `0x01` **bind** (empty payload) → device descriptor
2. `0x22` setting query · `0x05` device-state query · `0x16` dial/resource-list query
3. `0x02` **set-time** (local now)
4. `0x0e` health-switch read (flag 0) → `0x0e` health-switch write (enable categories)
5. `0x04` preferences → `0x03` user-profile read → `0x03` **user-profile push** (the finalizer)

If the watch still shows "pair with the app" after `activate`, re-run once connected, and confirm
the BlueZ **LE-only** workaround below is applied.

---

## `health` group (12) — measurement, history, realtime

| Command | Tag | Wire | Purpose |
|---|---|---|---|
| `health-switch-read` | [CAP] | `0x0e` f0 | Read per-category health-detection switches |
| `health-switch-write` | [CAP] | `0x0e` f0 | Enable/disable per-category detection (`--categories`, `--value`) |
| `history-sync <cat>` | [CAP] | `0x0e` f1 | Request one history record (reply is a **binary** record) |
| `history-status <cat>` | [CAP] | `0x0e` f1 | Read-status variant (`subop=1`) |
| `hr-history` | [CAP] | `0x0e` f1 | Daily HR history (**category 0** — intraday HR slots) |
| `spo2-history` | [CAP] | `0x0e` f1 | SpO2 history (category 2) |
| `sleep-history` | [CAP] | `0x0e` f1 | Sleep history (**category 3** — corrected, was mislabeled cat 5) |
| `realtime-open` | [SCHEMA] | — | SDK-derived "realtime" enable — **the watch has no live stream** (all data polled); opcode UNRESOLVED |
| `realtime-measure` | [SCHEMA] | — | SDK-derived one-shot trigger — unverified; watch data is polled, not streamed |
| `hr-config` | [SCHEMA] | — | Continuous HR-monitoring config |
| `health-interval` | [SCHEMA] | — | Auto-measure interval for a metric |
| `female-health-set` | [SCHEMA] | — | Menstrual-cycle config |

```bash
./venv/bin/python -m starmax_client history-sync 3 --dry-run     # sleep (cat 3) request frame
./venv/bin/python -m starmax_client history-sync 5 --dry-run     # activity: steps/dist/cal (cat 5)
./venv/bin/python -m starmax_client hr-history --dry-run         # daily HR (cat 0)
```

> <a id="health-sync-findings"></a>**Health-sync findings (on-device):**
> - **Consume-on-read** — a `0x0e` flag=1 detail record is handed over **once**; re-reading the
>   same category then returns only a ~55-byte **status stub**. A sync drains the detail; persist it.
> - **Steps ARE in the cat-5 activity record** (corrected 2026-07-12) — the daily **step total is a
>   `u32` at date-marker+6 in the category-5 activity record**, live-confirmed (162 & 225 matched the
>   watch face). The earlier "steps not in the record" note was reading the wrong category (**cat 0 =
>   HR**, which carries no steps); with the corrected map (activity = cat 5) steps decode cleanly.
>   Distance/calories (adjacent u32s) are still PROVISIONAL. Full decode: `../../docs/health-metrics.md`.

## `settings` group (14) — profile, reminders, display

| Command | Tag | Wire | Purpose |
|---|---|---|---|
| `user-profile` | [CAP] | `0x03` | Set profile (height/weight/birth-year/sex) + step & distance goals |
| `setting-query` | [CAP] | `0x22` | Read a generic setting value (`--key`) |
| `alarm-get` / `alarm-set` | [CAP] | `0x07` | Read / write an alarm (`--hour --minute [--off]`) |
| `device-state` | [SCHEMA] | `0x82` | time/unit/temp format, language, backlight, screen, raise-to-wake |
| `wrist-raise` | [SCHEMA] | `0x82` | Toggle raise-to-wake only (`--on 0/1`) |
| `sport-goals` | [SCHEMA] | `0x8A` | Daily step / calorie / distance goals |
| `dnd` | [SCHEMA] | `0xB4` | Do-not-disturb / quiet hours (`--start --end [--all-day] [--off]`) |
| `sedentary` | [SCHEMA] | `0xB6` | Sedentary / long-sit reminder (`--interval` min) |
| `drink-water` | [SCHEMA] | `0xB7` | Drink-water reminder (`--interval` min) |
| `event-reminders` | [SCHEMA] | `0xBB` | One event reminder (`--date --time --content`) |
| `world-clock` | [SCHEMA] | `0x22`† | World-clock cities (`--cities id,id`) |
| `aod` | [SCHEMA] | `0x22`† | Always-on-display schedule |
| `date-format` | [SCHEMA] | `0x22`† | Date format (`--format N`) |

`†` opcode absent from the SDK map too — framed on the observed generic-setting channel `0x22` as
a best-effort carrier; the message body is schema-exact.

```bash
./venv/bin/python -m starmax_client user-profile --height 175 --weight 70 --birth-year 1990 --dry-run
./venv/bin/python -m starmax_client dnd --start 22:00 --end 07:00 --dry-run
./venv/bin/python -m starmax_client alarm-set --hour 7 --minute 30 --dry-run
```

## `notify` group (5) — phone → watch pushes

| Command | Tag | Wire | Purpose |
|---|---|---|---|
| `notify-detailed` | [CAP] | `0x11` | Rich notification, title (f6) + body (f7), UTF-8 |
| `notify-summary` | [CAP] | `0x13` | Summary/count line (f5), flag=1 |
| `call` | [SCHEMA, UNVERIFIED] | — | Notify the watch of an incoming call (caller-ID) |
| `music` | [SCHEMA, UNVERIFIED] | — | Push now-playing music state ("follow the phone") |
| `camera` | [SCHEMA, UNVERIFIED] | — | Control the watch camera-remote UI |

> **⛔ Notification display is TABLED ([#5]).** `notify-detailed`/`notify-summary` build+send a
> **byte-correct** frame, but on an **LE-only** link the watch reports *"app not connected"* and
> suppresses it: display is gated on a connected **classic-BT** companion (HFP/RFCOMM), which an
> LE-only client can't be. The LE enable exchange (`0x04`+`0x03`) landed in **PR #4** — necessary,
> not sufficient. `call`/`music`/`camera` are schema-derived experiments; on Android incoming-call
> ring and camera-shutter ride **classic BT** (HFP/HID), not this channel (`../docs/protocol-spec.md`
> §8, §9.6).

## `files` group (4) — dials, resources, sport

| Command | Tag | Wire | Purpose |
|---|---|---|---|
| `dial-list` | [CAP] | `0x16` | Read the installed dial/resource list + active dial + storage |
| `dial-switch` | [SCHEMA/INFERRED] | — | Switch the active watch face by id |
| `nfc-list` | [SCHEMA/INFERRED] | — | Request the NFC card list |
| `sport-control` | [SCHEMA/INFERRED] | — | Start / pause / resume / stop a sport session |

> `dial-list`/`dial-switch` read/select installed faces on the `0x16` command channel. **Installing**
> a face, AGPS, and firmware OTA ride the separate **bulk plane** (`D1`→`D2`→`D4` on the same
> `0x0FF0` handles); see [Watch faces](#watch-faces--bulk-transfers) and `../docs/firmware-dfu.md`.

## Watch faces & bulk transfers

The full **watch-face install** path is live-validated ([#1]/[#2]). Details in
[`../../docs/watchface-install.md`](../../docs/watchface-install.md).

| Command | Tag | Wire | Purpose |
|---|---|---|---|
| `dial-list` | [CAP] | `0x16` | List installed dials/resources + active dial + storage |
| `dial-build <zip> <out>` | [CAP] | — (offline) | Transcode a dial `.bin` (ZIP of images) → native container (`transcode` extra) |
| `dial-push <zip\|blob>` | [CAP] | bulk plane | Install a face and **auto-activate** (transcodes a ZIP on the fly); reliable via ATT write-with-response |
| `dial-activate` / `dial-switch <id>` | [SCHEMA/INFERRED] | `0x16` | Switch the active face to an installed dial id |
| `ota-preview` / `send-file` | preview | — | Build (but don't send) the bulk-transfer plan for a file / OTA |

```bash
./venv/bin/python -m starmax_client dial-build myface.bin myface.native   # offline
./venv/bin/python -m starmax_client dial-push  myface.bin                 # install + activate
./venv/bin/python -m starmax_client dial-list                             # what's installed
```

[#1]: https://github.com/jphein/starmax-gtx2-client/pull/1
[#2]: https://github.com/jphein/starmax-gtx2-client/pull/2

## `weather` group (1) — `0x12` push

Ships in **[PR #3]** (auto-discovered like every other group — no `cli.py` edit). Pushes current
conditions (protocol-spec §3.7). It maps CLI args onto `base.Weather` and delegates to
`base.build_weather` (byte-parity with the Gadgetbridge `StarmaxMessages.buildWeather`).

| Command | Tag | Wire | Purpose |
|---|---|---|---|
| `weather` | [CAP] | `0x12` | Push current weather (temp/condition/hi-lo/city + optional hourly[24]/daily[3]/pressure) |

All args optional with PII-free defaults (city defaults to synthetic `Anytown`); only condition
code `6` (clear) is capture-confirmed — `--condition` takes the watch's raw 1-based code.
**Live-validated** on-device (widget showed the sent hi/lo). Display is gated on the `0x04`
feature-enable, so `weather` **auto-sends the `0x04` first by default** (Just Works without a
separate `activate`); it's `0x04`-only (no profile reset, unlike notify's enable). Pass
`--no-enable` to send the `0x12` weather frame alone.

```bash
./venv/bin/python -m starmax_client weather --temp 25 --condition 6 --city Anytown
./venv/bin/python -m starmax_client weather --temp 25 --hi 30 --lo 18 --dry-run
#   optional: --hourly 31/21,31/20,... (max 24)   --daily 33/22/6,... (max 3)   --pressure 1013.25
```

---

## Adding a command (module contract)

Drop a `starmax_client/commands/<group>.py` that defines `COMMANDS: {name: builder}` and
`register(subparsers, client=None)` (each subcommand supporting `--dry-run`); it is auto-discovered
— no CLI edit. Optional `GROUP`, `PARSERS`, and `SMOKE_ARGS`. Import the core absolutely
(`from starmax_client import framing`). See the README's *module contract* section.
