# gtx2_client — ESPHome external component for the GTX2 watch

Make an **ESP32-C3 the whole-house gateway for a GTX2 watch**, reusing your room-aware iTag
mechanism. The node holds the GATT link to the watch, speaks our custom **0x0FF0 / C1** protocol
(bind + framing + CRC — not a bare characteristic), and:

- fires **`esphome.gtx2_input {input, detail, node}`** for the watch's LE inputs (music
  play/pause/prev/next, find-phone) so HA automations bind them to lights/scenes — the
  `itag_lights.yaml` pattern, where **the node holding the link IS the room**;
- polls **health → sensors** (heart-rate, SpO2, steps, distance, calories) plus device
  **firmware / active-face / connected** and link **RSSI**;
- exposes the **SAFE command set** (find/buzz, set-time, weather, alarms, dial-list/switch) as
  methods you call from YAML lambdas. The **DANGER tier** (flash-firmware / dnd / music / camera /
  call) is **not present** — same accidental-brick guard as the host bridge.

This is the **production path** for driving the watch from HA. It **supersedes the range-limited
Python host bridge** (`../gtx2_bridge`), which stays as a CLI / reference proof (and still owns the
one feature a C3 can't do — see [Watchface notifications](#watchface-notifications-host-only)).

## How it rides the proxies / nodes

```
   Home Assistant (HAOS)                ESP32-C3 gtx2-node (per room)            GTX2 watch
  ┌──────────────────────┐   Wi-Fi   ┌──────────────────────────────────┐  BLE ┌──────────┐
  │ automations, lights, │◄────────► │ ble_client link + gtx2_client:    │◄───►│  GTX2    │
  │ dashboards, events    │  API/     │  C1 framing/CRC (ported from      │ 0x0FF0│ (stock  │
  │ (esphome.gtx2_input)  │  events   │  starmax_client) + bind + decode  │      │  fw)    │
  └──────────────────────┘           └──────────────────────────────────┘      └──────────┘
```

Exactly like the iTags: each node **contests** the connection on
advertisement (strongest-RSSI node wins the head-start race), so only one node holds the link — and
that node is, by definition, the room the watch is in. Multi-room = add the same watch MAC to more
nodes + label each room's lights. A weak-link release (RSSI < -85 dBm ×3) hands the link off when
the watch leaves the room.

> **Not a bluetooth_proxy.** The original brief imagined routing through HA's `bluetooth_proxy`
> active-connection path + a Python `custom_components/gtx2`. JP pivoted to the ESPHome route: the
> node connects and speaks the protocol **on the ESP32 itself** (like the iTags), so there's no
> fixed BLE host and no HA-side Python integration to maintain.

## Install

This is an ESPHome **external component** (not a HACS integration). Point a node's
`external_components:` at this directory:

```yaml
external_components:
  - source:
      type: local          # or a git source once this lands on a branch/repo
      path: components      # ha-bridge/esphome/components (this repo)
    components: [gtx2_client]
```

Then add a `ble_client:` for the watch and a `gtx2_client:` bound to it (see `gtx2-node.yaml`).
The `api:` section **must** set `homeassistant_services: true` — that's what lets the node fire
`esphome.gtx2_input` events into HA. Flash with `esphome run gtx2-node.yaml`. Secrets (`wifi_*`, `gtx2_api_key`, `gtx2_ota_password`,
`gtx2_ap_password`) live in `secrets.yaml` / vault — **never committed** (`.gitignore`).

`gtx2-node.yaml` is a complete, deploy-ready example (C3, esp-idf, contested claiming, weak-link
release, all SAFE command buttons). Fill in your watch's real static MAC (`F4:4E:FD:xx:xx:xx`) in the
two `# TODO` spots.

### HA glue

`gtx2_lights.yaml` is a drop-in `ha/packages/` sibling of `itag_lights.yaml`: it turns
`esphome.gtx2_input` into room-light control (`node → area_id → the room's gtx2_lights-labelled
entities`). music.play_pause = toggle · music.next/prev = brightness ±15% · find-phone = your own
ringer. Deploy per `~/Projects/ha/CLAUDE.md` (ssh + `check_config`); not auto-deployed.

## Configuration (`gtx2_client:`)

| Key | Default | Meaning |
|---|---|---|
| `ble_client_id` | — (required) | the `ble_client:` holding the watch link |
| `node_name` | device name | string put in every `gtx2_input` event (`node` → room) |
| `event` | `esphome.gtx2_input` | HA event name fired for LE inputs |
| `health_interval` | `300s` | one health category polled per tick (round-robin activity→HR→SpO2); `0s` disables |
| `time_id` | — | a `time:` source for set-time / weather timestamps |
| `heart_rate`,`spo2`,`steps`,`distance`,`calories` | — | optional `sensor:` entities |
| `connected` | — | optional `binary_sensor:` (link up) |
| `firmware`,`active_face` | — | optional `text_sensor:` entities |

## The contract (reconciled with the rest of the stack)

**Events** (→ the dashboards / your automations):
`esphome.gtx2_input` with `{input, detail, node}` where `input` ∈
`music.play_pause | music.prev | music.next | find_phone | records_ready` (wire signatures:
internal opcode-resolution RE — notify char `0x0002`, op `0x10`, discriminator at `value[11]`).

**Entities** (→ the dashboards): the ESPHome sensors above, named `GTX2 <X>` — a subset of the host
bridge's `metrics.SENSORS` manifest (heart_rate/spo2/steps/distance/calories + firmware/active-face
+ connected + link RSSI). `battery` is intentionally absent: **no battery opcode exists** on this
firmware (same finding as the bridge).

**Commands** (→ the HA bridge's catalog/manifest): the SAFE tier only —
`find · set-time · weather · alarm-set · dial-list · dial-switch`, exposed as component methods
(YAML lambdas / template buttons). This matches `gtx2_bridge/catalog.py`'s green+yellow safe set;
the **RED/DANGER tier is omitted entirely** so no automation or button can brick the watch.

**Presence:** continuous room presence is owned by the **HA-side passive-RSSI computation**
(the ha-bridge presence package, `sensor.gtx2_room`). This component only provides **control-time
"connection = room"** routing for `gtx2_input` (the iTag pattern) — it does **not** publish a
competing room signal.

## Watchface notifications (host-only)

The one SAFE feature deliberately **not** on the node: the "notification = a pushed watch-face".
Rendering a face needs Pillow + an RGB565/LZ4 image codec and the push is a multi-second, multi-KB
D-plane bulk transfer — neither fits an ESP32-C3, and there's no clean way to hand a ~12 KB blob to
the node over the HA API. **The range-limited host bridge (`../gtx2_bridge`, `faces.py`) keeps this
job.** The node owns the command / input / health plane; the host bridge owns heavy watchface pushes.
(A future path: HA renders the face and the node streams a precomputed blob — the D-plane streaming
is portable, blob delivery is the open problem.)

## Layout

```
ha-bridge/esphome/
  components/gtx2_client/
    __init__.py          ESPHome codegen (config schema; multi-watch via MULTI_CONF)
    gtx2_protocol.h/.cpp  PURE C1 protocol port (framing/CRC/builders/decoders) — no ESPHome dep
    gtx2_client.h/.cpp    the ble_client node: connect+bind, notify routing, events, sensors, cmds
  gtx2-node.yaml         complete example node (flash this)
  gtx2_lights.yaml       HA package: gtx2_input -> room lights (itag_lights sibling)
  test/                  offline byte-parity test (host g++, no BLE) — see below
```

## Testing (offline, no watch, no HA)

The protocol layer is **dependency-free** and proven **byte-identical to the verified
`starmax_client` Python** by a host test — the same guarantee `crown_ble.c` makes vs `crown.py`:

```bash
cd test && ./run_host_test.sh     # regenerates golden vectors from starmax_client, compiles + runs
```

It asserts (55 checks): every command builder == the Python builder byte-for-byte; inbound frames
reassemble + CRC-check + route to the right LE input (through the double-send dedup); the
activity/HR/SpO2 record decoders and the firmware-stamp / active-face reply parsers match the Python
extractors on identical bytes; CRC canonical value; PDU fragmentation round-trips. `gen_golden.py`
is the generator (fixed, PII-free inputs; placeholder MAC).

`esphome config gtx2-node.yaml` validates the YAML + codegen; `esphome compile gtx2-node.yaml`
builds the firmware (first run downloads the esp-idf toolchain).
