# GTX2 → Home Assistant: the watch as a "super-iTag" node (crown dimmer + stock extras)

**Date:** 2026-07-14 · **Companion:** `cfw-crown-protocol.md` (the crown wire
protocol), `cfw-board-port.md` (crown hardware gate). **Status:** integration design. The crown-dimmer
half is gated on the custom firmware (#31 teardown); the buzz / health / watchface-notify halves work
on **stock** firmware today.

## 0. TL;DR

Model the GTX2 as a **"super-iTag"** inside an existing room-aware BLE-button system
(`ha/packages/itag_lights.yaml` + the ESPHome `*-tag-node` C3 nodes).
That system's core idea already does the hard part: **the ESP32-C3 node that holds the GATT connection
to a tag IS, by definition, the room the tag is in**, so an event fired with `{tag, node}` lets HA act
on `area_id(node)`'s lights. The GTX2 slots straight in — with one difference: the watch speaks our
custom **0x0FF0 C1 protocol** (bind + framing + CRC), not a bare GATT characteristic, so the node needs
a tiny custom ESPHome component instead of the stock `ble_client` primitives. Everything else — the
room-mapping, the label-curated lights, the RSSI weak-link handoff — is reused verbatim.

| Capability | Firmware needed | How |
|---|---|---|
| **Crown → dim the room's lights** | **CUSTOM (#31)** | node decodes `0xA1` crown notifies → `esphome.gtx2_crown {delta, action, node}` → HA dims `area_id(node)`'s `gtx2_lights` |
| Buzz / find the watch | **stock, now** | HA button → node sends our `find-device` (`0x18`) over the bound session |
| Health (HR / steps / battery) | **stock, now** | node polls our health/realtime opcodes → HA sensors |
| Watchface-as-notification | **stock, now** | HA renders a face → node pushes it via our dial-push → wrist shows it (sidesteps the classic-BT notification wall, #5) |
| Snappy room presence | **stock works; custom is better** | node holds the connection = room (free); the custom fw's fast-advert/iBeacon bonus sharpens *passive* presence |

---

## 1. Why the GTX2 is a "super-iTag" (and the one real difference)

The iTag system (`example-tag-node.yaml`) works like this:

- Each C3 node lists the tag in `ble_client` with `auto_connect: false` and **contests** the
  connection on advertisement: the node that hears the tag strongest waits the shortest
  RSSI-proportional head-start, then `connect()`s (`claim_itag_*`). Only one node holds the link.
- The tag reports its button **only over that GATT link** (notify on char `ffe0/ffe1`). On notify the
  holding node fires `esphome.itag_button {tag, node}`.
- HA (`itag_lights.yaml`) turns `node → device_id → area_id` into a room and toggles that room's lights
  (the set curated by the `button_lights` label). A weak link (`< -83 dBm`) triggers release + a
  handoff so the press lands in the co-located room.

**The GTX2 maps onto this 1:1, with exactly one difference:** the iTag's button is a *stock* GATT
notify (`ffe1`) that ESPHome's `ble_client` reads natively; the GTX2's crown is a *custom-firmware*
notify carried inside **our C1 framing** on service `0x0FF0` — which ESPHome cannot decode out of the
box (it's not a plain characteristic value; it needs the bind handshake, the `0xC1/0xC3` reassembly,
the CRC, and opcode routing). So the GTX2 node runs a **small custom ESPHome component** that ports the
framing. Once that component exists, the watch is just another tag: the node holding the connection is
the room, and a crown turn dims that room — **`itag_button` upgraded from toggle → dim.**

Two beautiful consequences:
1. **Room-awareness is free.** A crown notify only ever arrives at the node holding the connection, so
   `node` in the event *is* the room — no separate presence stack, no Bermuda/ESPresense trilateration
   needed for the dimmer. (Passive presence for other uses: §6.)
2. **One node, many watches, many rooms.** Add the same watch MAC to more nodes + label each room's
   `gtx2_lights`; the contested-claiming logic already picks the right room. Identical to iTag
   multi-room.

---

## 2. The GTX2 wire facts the node needs

From `starmax_client/transport.py` + `commands/base.py` (verified vs live GATT + capture):

| Thing | Value |
|---|---|
| Service | `00000ff0-0000-1000-8000-00805f9b34fb` (custom 16-bit `0x0FF0`, **not** Nordic UART) |
| Write char (app→watch) | `00000001-0000-1000-8000-00805f9b34fb` (ATT 0x0026) |
| Notify char (watch→app) | `00000002-0000-1000-8000-00805f9b34fb` (ATT 0x0028) |
| Address | GTX2 advertises a **public static** MAC `F4:4E:FD:xx:xx:xx` (fill in your unit's — placeholder below) |
| Framing | `0xC1` SOF · seq · dir · ver · flag · opcode · LEN(LE) · 3×00 · payload · CRC-16/CCITT-FALSE (watch→app protobuf) — `docs/protocol-spec.md` §1 |
| Bind | opcode `0x01`, empty payload — must run once after connect before other commands |
| Find/buzz | opcode `0x18` (`build_find_device(on=True)`) |
| Health sync | opcode `0x0E` (`build_health_sync`, flag=1 binary records) |
| **Crown (custom fw)** | opcode `0xA1` flag=1 notify — `cfw-crown-protocol.md` |
| MTU | 247 negotiated on-device (244-byte PDU); crown frames are single-PDU |

The reference implementation of all of the above (framing, CRC, bind, find, health, dial-push, and the
crown decoder) is `starmax_client/` — the C++ node component is a straight port of `framing.py`,
`crc.py`, `commands/base.py` and `crown.py` (a few hundred lines total; no protobuf lib needed — the
payloads are hand-rolled varints, and the crown data frame is fixed binary).

---

## 3. The ESPHome C3 node (crown + stock extras)

**Hardware:** a spare **ESP32-C3**, esp-idf framework, exactly like `example-tag-node.yaml`.
**Connection budget:** each held GATT link costs one slot of `esp32_ble: max_connections`. An example node
node already holds up to 3 iTags — adding the watch means bumping `max_connections` (the C3/esp-idf BT
stack supports it) or dedicating a node to the watch. A dedicated "watch node" is cleanest: it can also
be the room's crown-dimmer brain.

**Can one C3 double as presence receiver + crown client?** Yes — the C3 already runs
`esp32_ble_tracker` (passive scan) alongside `ble_client` (active links). Tradeoff: active scanning +
holding a connection + being a `bluetooth_proxy` all share the single C3 radio; that deployment
deliberately **split** proxy/BMS duty (`example-ble-proxy`) from tag-GATT duty (`example-tag-node`) to avoid
radio contention. Recommendation: reuse that split — put the watch on the tag-node role, leave passive
proxy/presence on its own radio.

### 3.1 Node YAML sketch (custom-component form)

This is the shape; the `gtx2_client` custom component (§3.2) carries the protocol. Placeholders marked
`# TODO`.

```yaml
# gtx2-crown-node.yaml — a "super-iTag" node for the GTX2 watch
esphome:
  name: gtx2-crown-node
  friendly_name: gtx2-crown-node
  platformio_options:
    board_build.mcu: esp32c3
    board_build.variant: esp32c3
    board_build.flash_mode: dio
  # Pull in the protocol component (ported from starmax_client)
  includes:
    - gtx2_client.h          # framing + CRC + bind + crown decode (see §3.2)

esp32:
  variant: ESP32C3
  board: esp32-c3-devkitm-1
  framework:
    type: esp-idf
    sdkconfig_options:
      CONFIG_BT_BLE_50_FEATURES_SUPPORTED: y
      CONFIG_BT_BLE_42_FEATURES_SUPPORTED: y

logger:
api:
  encryption:
    key: !secret gtx2_node_api_key     # generate per node, store in vault (never commit)
ota:
  - platform: esphome
    password: !secret gtx2_node_ota
wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password
  ap:
    ssid: "GTX2-Crown-Node"

esp32_ble:
  max_connections: 3

# Contested claiming — identical pattern to example-tag-node: strongest node grabs the link,
# so the holder == the room the watch is in.
esp32_ble_tracker:
  scan_parameters: { continuous: true, interval: 320ms, window: 240ms }
  on_ble_advertise:
    - mac_address: F4:4E:FD:00:00:01      # TODO: your watch's real static MAC
      then:
        - lambda: |-
            int d = (int)((-55 - x.get_rssi()) * 80);
            if (d < 0) d = 0; if (d > 3000) d = 3000;
            id(claim_delay_w) = d; id(last_adv_w) = millis();
            id(claim_watch).execute();

globals:
  - { id: claim_delay_w, type: int, initial_value: '0' }
  - { id: last_adv_w, type: uint32_t, initial_value: '0' }

ble_client:
  - mac_address: F4:4E:FD:00:00:01        # TODO: your watch's real static MAC
    id: gtx2
    auto_connect: false
    on_connect:
      then:
        - lambda: 'id(gtx2_connected).publish_state(true);'
        # Run the bind handshake + subscribe to the 0x0FF0 notify char, then enable the crown stream.
        - lambda: 'gtx2_client::on_connect(id(gtx2));'
    on_disconnect:
      then:
        - lambda: 'id(gtx2_connected).publish_state(false);'

binary_sensor:
  - platform: template
    name: "GTX2 Connected"
    id: gtx2_connected

# Link RSSI for the weak-link handoff (same release logic as the iTags)
sensor:
  - platform: ble_client
    ble_client_id: gtx2
    id: gtx2_rssi
    name: "GTX2 Link RSSI"
    type: rssi
    update_interval: 2s
    filters: [ { median: { window_size: 3, send_every: 1, send_first_at: 1 } } ]
    on_value:
      then:
        - lambda: |-
            static int weak = 0;
            if (x < -85 && !std::isnan(x)) { if (++weak >= 3) { weak = 0; id(gtx2)->disconnect(); } }
            else weak = 0;

script:
  - id: claim_watch
    mode: single
    then:
      - delay: !lambda 'return id(claim_delay_w);'
      - lambda: 'if (millis() - id(last_adv_w) < 2500) id(gtx2)->connect();'

# Stock-firmware extras (work today) — see §5
button:
  - platform: template
    name: "Buzz GTX2"
    icon: mdi:watch-vibrate
    on_press:
      - lambda: 'gtx2_client::find(id(gtx2), true);'   # our 0x18 find-device over the bound session
  - platform: template
    name: "Stop Buzz GTX2"
    on_press:
      - lambda: 'gtx2_client::find(id(gtx2), false);'
```

### 3.2 The `gtx2_client` component (what it does)

A header-only C++ port of the client protocol. Responsibilities:

1. **`on_connect(BLEClient*)`** — send the bind frame (`0x01`), locate the `0x0FF0` service + write
   (`0x0001`) / notify (`0x0002`) chars, subscribe to notifies, then send the crown-enable
   (`build_crown_enable`) — the exact byte-exact frame is
   `c1 01 01 01 00 a1 13 00 00 00 00 08 01 10 01 18 01 20 00` (seq differs).
2. **notify handler** — feed inbound PDUs to a `Reassembler` (port of `framing.Reassembler`: join
   `0xC1`/`0xC3`), `parse_frame` (verify CRC), and route by opcode:
   - `0xA1 flag=1` → `parse_crown_frame` → for each event fire the HA event (below);
   - `0x0E`/`0x05`/etc. → health/battery decode → publish sensors (§5.2).
3. **`find(BLEClient*, bool on)`** — send `build_find_device(on)` (`0x18`) over the bound link.
4. **crown → HA event** — per decoded crown batch:
   ```cpp
   // net rotation this frame → one event; buttons → one event each
   if (batch.net_rotation() != 0)
     fire_homeassistant_event("esphome.gtx2_crown",
         {{"node", App.get_name()}, {"kind", "rotate"}, {"delta", batch.net_rotation()}});
   for (auto &b : batch.buttons())
     fire_homeassistant_event("esphome.gtx2_crown",
         {{"node", App.get_name()}, {"kind", "button"}, {"action", b.action_name()}});
   ```

Porting effort: framing+CRC ≈ 80 lines, bind/find ≈ 20, crown decode ≈ 40, glue ≈ 60. All logic is
already proven in `starmax_client/{framing,crc,crown}.py` + `commands/base.py`; the C++ is a
transliteration. **No protobuf dependency** — the control payloads are fixed varint byte sequences and
the crown data frame is fixed binary.

> If a full custom component is more than you want to maintain, an interim path: ESPHome
> `ble_client.ble_write` can send *precomputed* frames (bind, enable, find) as raw byte arrays to char
> `0x0001`, and a `ble_client` `characteristic` sensor with `notify: true` on `0x0002` can hand raw
> notify bytes to a `lambda` that runs the reassembler/decoder inline. This avoids a separate `.h`
> file at the cost of inlining ~150 lines of lambda. The component form is cleaner and reusable.

---

## 4. HA side — `gtx2_lights.yaml` (dim the crown's room)

A drop-in sibling of `itag_lights.yaml`. **Crown rotation → brightness step; button → toggle / off.**
Room = the node that fired the event (`area_id(node)`); the light set is curated by a **`gtx2_lights`**
label (mirrors `button_lights`).

```yaml
# ha/packages/gtx2_lights.yaml — crown → room light dimmer (mirrors itag_lights.yaml)
automation:
  - id: gtx2_crown_room_lights
    alias: "GTX2 crown: dim/toggle the watch's room lights"
    mode: queued
    max_exceeded: silent
    triggers:
      - trigger: event
        event_type: esphome.gtx2_crown
    actions:
      - variables:
          node: "{{ trigger.event.data.node }}"
          room: "{{ area_id(device_id(node)) }}"
          kind: "{{ trigger.event.data.kind }}"
          delta: "{{ trigger.event.data.delta | int(0) }}"
          action: "{{ trigger.event.data.action | default('') }}"
          lights: >-
            {{ area_entities(room) | select('in', label_entities('gtx2_lights')) | list }}
      - condition: template
        value_template: "{{ room not in [none, ''] and lights | count > 0 }}"
      - choose:
          # rotation → relative brightness step (each detent ~4%); scale as you like
          - conditions: "{{ kind == 'rotate' }}"
            sequence:
              - action: light.turn_on
                target: { entity_id: "{{ lights }}" }
                data:
                  brightness_step_pct: "{{ delta * 4 }}"
                  transition: 0.2
          # short click → toggle the room (same behaviour as itag_button)
          - conditions: "{{ kind == 'button' and action == 'click' }}"
            sequence:
              - action: homeassistant.toggle
                target: { entity_id: "{{ lights }}" }
          # long-press → hard off (scene reset / "all off in this room")
          - conditions: "{{ kind == 'button' and action == 'long' }}"
            sequence:
              - action: light.turn_off
                target: { entity_id: "{{ lights }}" }
```

Notes:
- `brightness_step_pct` with a signed value is exactly `light.turn_on`'s relative-dim contract:
  `+delta*4` brightens, `−delta*4` dims, HA clamps at 0/100. A coalesced spin (net delta from one
  frame) becomes one smooth step.
- Reuse `itag_lights.yaml`'s **weak-link handoff** verbatim if you want the crown to follow the watch
  across rooms mid-session — but for a dimmer it's usually fine to just act on the current holder.
- Curate with the `gtx2_lights` label (create it in HA, label the entities). You can point it at the
  **same** entities as `button_lights` so the crown dims what the iTag toggles.

---

## 5. Stock-firmware extras (work TODAY, no teardown)

These need no custom firmware — they ride the stock GTX2 command protocol through the same node
component. They make the node useful immediately while the crown-dimmer waits on #31.

### 5.1 Buzz / find
`gtx2_client::find(id(gtx2), true/false)` sends `build_find_device` (`0x18`) — the HA `button`s in
§3.1. (Unlike the iTag's stateless IAS write, this rides the **bound** C1 session, so it lives in the
component, not a bare `ble_write`.) Add HA `script`s mirroring `find_itag_*` to fan out across nodes.

### 5.2 Health → HA sensors
The node periodically sends the health/realtime opcodes (`0x0E` sync, or the realtime monitor path used
by `starmax_client monitor`) and decodes replies → publishes ESPHome `sensor`s: heart-rate, steps,
battery %. HA gets them as normal entities (history, alerts, dashboards). Poll gently (e.g. every
5–15 min for steps/battery, on-demand for HR) to spare the watch battery.

### 5.3 Watchface-as-notification (sidesteps the classic-BT notification wall, #5)
The GTX2 has **no BLE notification service** (notifications ride classic-BT SPP the client doesn't
implement — #5). Workaround: HA renders a small "info face" (text/number → image) and the node pushes it
as a **watchface** via our dial-push (`starmax_client/commands/dials.py` + `dialfmt`/`dialtranscode`).
The wrist shows the pushed face → effectively a glanceable notification.

**Caveats (design around them):**
- A dial push is a **multi-second D-plane bulk transfer** (announce → chunks → complete), **not** a live
  stream — good for *periodic* info (next event, temperature, a reminder), not a rapid ticker.
- The push **auto-activates** the face (takeover, not an overlay) — it replaces the current face, so use
  it deliberately (e.g. a "notification face" the user dismisses back to their normal face), and rate-limit.
- **Speed tip:** author a **minimal small-background notification face** (mostly flat color, one text
  zone) — smaller payload = faster push. Reserve full-art faces for manual changes.

This is a genuinely useful stock capability; treat it as "push an info card to the wrist every N
minutes," not as an interrupt-driven notifier.

---

## 6. Room presence — connection = room (free), plus a passive option

- **For the crown dimmer:** presence is already solved — the connection-holding node is the room (§1).
  Nothing else required.
- **For passive presence** (knowing the watch's room even when not actively controlling, or feeding
  `person`/occupancy): the watch is a trackable static-MAC beacon. Two routes, both reuse existing
  receivers, no new stack:
  - **ESPHome `bluetooth_proxy` + Bermuda** (HA integration) — trilaterates from the RSSI your existing
    proxy nodes already report. Cleanest given you're all-ESPHome.
  - **ESPresense** — per-room nodes publishing distance over MQTT.
- **Stock advert is slow (~4 s)** → coarse/laggy room resolution for the passive route. The custom
  firmware's **fast-advert / iBeacon bonus** (`cfw-crown-protocol.md` §6.5: drop the advert interval to
  ~250–500 ms and/or emit an iBeacon manufacturer AD via `bt_manager_ble_adv_start`) makes passive
  presence snappy. Same firmware that adds the crown → so the teardown that unlocks the dimmer also
  unlocks fast presence.

**One detail to confirm:** the iTag stack is ESPHome `ble_client`
nodes (confirmed from `example-tag-node.yaml`), so the GTX2 crown node is a direct sibling. If you *also*
want passive presence, tell me whether you'd add Bermuda or ESPresense — the beacon side is identical
either way; only the HA-integration glue differs.

---

## 7. Build order (what to do, in sequence)

1. **Now (stock):** flash a spare C3 with the `gtx2-crown-node` yaml + the `gtx2_client` component
   (bind + find + health first). Confirm buzz + a battery sensor in HA. Add the watchface-notify push.
2. **Now (HA):** create the `gtx2_lights` label, add `ha/packages/gtx2_lights.yaml`. It's inert until
   crown events arrive.
3. **After #31 teardown → custom fw:** the firmware starts emitting `0xA1` crown notifies + fast advert.
   The node's crown decoder lights up `esphome.gtx2_crown`, and the dimmer works with **zero** further
   HA changes.

## Sources
- An example iTag/HA setup: `ha/packages/itag_lights.yaml`, `ha/packages/example-tag-node.yaml`.
- GTX2 protocol: `starmax_client/{transport,framing,crc,crown}.py`, `commands/base.py`, `commands/dials.py`;
  `docs/protocol-spec.md`, `docs/cfw-crown-protocol.md`.
- Notification wall (#5): stock GTX2 notifications ride classic-BT SPP (unimplemented) → watchface-push
  workaround.
- Fast-advert/iBeacon firmware bonus: `docs/cfw-crown-protocol.md` §6.5, `bt_manager_ble.c`
  (`bt_manager_ble_adv_start`, `struct bt_le_adv_param`).
