# GTX2 Control Center — Home Assistant dashboard

A Gadgetbridge-equivalent Starmax GTX2 surface for Home Assistant: a multi-watch
fleet (up to 3 watches), live activity rings + health, every SAFE command, and a
Media & Find card — carrying the round-watch identity, palette, and typography
from the [project site](https://jphein.github.io/starmax-gtx2/) into live Lovelace.

> **Migrated to the `gtx2` custom component (2026-07-15).** These dashboards bind the
> integration's contract: the `gtx2.*` routed **services** (`{watch: <slug>}` → holding
> node first, MQTT host-bridge fallback), the per-watch aggregate entities
> `sensor.gtx2_<slug>_*` / `binary_sensor.gtx2_<slug>_*`, and the hub controls
> (`text.*` / `switch.*` / `select.*` …). They **replace** the transitional
> `script.gtx2_*` wrappers + `input_*.gtx2_*` helpers, all retired with their packages.

## Files

| File | What | Needs |
|------|------|-------|
| **`gtx2-dashboard.yaml`** | **Recommended** — 3 views, two-watch heroes + gated 3rd, Media & Find | mushroom + the `gtx2-watch-card` (served by the integration) |
| **`gtx2-dashboard-core.yaml`** | Zero custom-JS / zero-HACS fallback, same IA & bindings | core HA only |

The custom card `gtx2-watch-card.js` now lives in the integration at
`custom_components/gtx2/www/gtx2-watch-card.js` (single source of truth) and is served at
`/gtx2-static/gtx2-watch-card.js` — registered as a frontend module by the integration, so
there is **no** `/config/www` copy and no manual resource to add for the recommended build.

`apexcharts` is intentionally **not** used (native `history-graph` instead), so the
recommended build needs only mushroom (already common on this install). Pick **one** YAML.

## The entity / command contract

Per-watch **slug**: `daily`, `spare`, `watch3` (generic 3rd, gated — see Scaling). Every
entity follows the `gtx2_<slug>_*` pattern; every command is a `gtx2.*` service taking
`{watch: <slug>}`.

### Presence + health (integration aggregates; hold-last on roam)
- `sensor.gtx2_<slug>_room` · `binary_sensor.gtx2_<slug>_present` · `binary_sensor.gtx2_any_present`
- `binary_sensor.gtx2_<slug>_connected` (link on the node holding the watch)
- `sensor.gtx2_<slug>_heart_rate | _spo2 | _steps | _distance | _calories`
- `sensor.gtx2_<slug>_firmware` (a build **stamp**, not a version) · `_active_face` · `_link_rssi`
- `sensor.gtx2_<slug>_holder` (the node prefix currently holding the watch, or `none`)

Bind the aggregate `<slug>` names — **never** the per-node `gtx2_<room>_*` source entities
(the holder changes as the watch roams; the coordinator resolves it for you).

### Commands — `gtx2.*` services (holding node first, host-bridge fallback)
The integration owns MAC lookup + routing, so buttons pass only `{watch: <slug>}`:
- `gtx2.{buzz, stop_buzz, sync_time, read_health, read_state, push_weather, release_link, activate}`
- `gtx2.{set_alarm, switch_dial}` take parameters → run from **Developer Tools → Actions**.
- `gtx2.{push_text, push_text_label, push_face}` — the staged calibration / gauge push paths.

The recommended build's custom card injects `watch:` in JS at click time (Lovelace `tap_action`
`data:` is not templated); `-core`'s plain buttons pass `data: { watch: <slug> }` explicitly.

### Notify — `gtx2.push_notification` (host-bridge render)
- `gtx2.push_notification` (`{watch, title, body, footer}`; footer defaults to the current time)
- compose entities `text.gtx2_notify_title | text.gtx2_notify_body` (the card's 💬 Notify reads
  these and passes them; `-core` per-watch Notify buttons rely on the service reading them).

### Media & Find (watch → HA; the watch's buttons are the remote)
- `gtx2.media_play_pause | media_next | media_prev` · `gtx2.find_my_phone` (all no-arg)
- The media player + find-phone notify/speaker/TTS targets are configured in the **integration
  options** (Settings → Devices & Services → GTX2 → Configure), not dashboard helpers.

### Fleet + hub controls
- `binary_sensor.gtx2_bridge_online` · `sensor.gtx2_detected_watches` · `sensor.gtx2_last_result`
- Push view / feature gates (hub): `select.gtx2_push_target | gtx2_dial`, `text.gtx2_push_text`,
  `number.gtx2_alarm_index`, `switch.gtx2_alarm_enabled`, `time.gtx2_alarm_time`,
  `switch.gtx2_gridwatts_face | gtx2_weatherface_live`, `switch.gtx2_<slug>_screen_break`.
- Per-watch MAC is **not** an entity anymore — it lives in the integration config entry, so the
  dashboard stays PII-free.

**Not available anywhere** (no card references them): battery (no GTX2 opcode),
stress/HRV/sleep/workouts, and every danger-tier command. Flashing is CLI-only.

## The three views

1. **Watches** — the fleet. One live round-watch hero per watch (tri-ring
   steps/calories/distance + center clock/steps/HR, status header, per-watch SAFE
   action row) + a notify composer + the Media & Find card. Count-agnostic header.
2. **Health** — per-watch activity gauges, cardio (HR tile + SpO₂ gauge), device
   read-out (link/firmware-build/active-face/RSSI/room), and a both-watch 24 h trend.
3. **Status** — bridge/registry health, presence + follow-me-lights summary, and the
   safety posture (safe surface only; flashing CLI-only).

## Scaling to 3 watches

The layout does **not** assume exactly 2. The fleet header counts every
`binary_sensor.gtx2_*_connected` that's `on`. **Watch 3** is pre-staged (slug `watch3`) as a
**visibility-gated** hero + Health section — hidden until watch3 is configured with a MAC in the
integration options (empty MAC ⇒ no `gtx2_watch3_*` entities ⇒ `unavailable` ⇒ hidden), then it
auto-appears with full rings + `gtx2.*` actions. To add a 4th, copy a hero + Health section, swap
the slug in `watch:` / `entities:`, add the slug to the integration's watch list, and gate it:

```yaml
visibility:
  - condition: state
    entity: binary_sensor.gtx2_<slug>_connected
    state_not: unavailable
```

(daily + spare stay ungated so they never vanish on a flaky-node drop; extra watches gate on
their `connected` entity so an unconfigured slot stays hidden — no broken card.)

## Install / deploy

1. **Integration**: install the `gtx2` custom component + create the config entry (enter the
   watch MACs + options). It serves the card at `/gtx2-static/gtx2-watch-card.js` automatically.
2. **Card resource** (recommended build): the integration registers the card as a frontend
   module. If a stale `/local/gtx2-watch-card.js` storage resource exists from the pre-migration
   build, **delete it** (Settings → Dashboards → Resources, or WS `lovelace/resources/delete`)
   so the card isn't double-loaded. `-core` needs no card at all.
3. **Mushroom** (recommended build): install `lovelace-mushroom` via HACS.
4. **Dashboard**: copy the chosen YAML to `/config/`, register a yaml-mode dashboard under
   `lovelace: → dashboards:` in `configuration.yaml`, then `check_config`; content edits
   thereafter need only a browser refresh. Full step-by-step + verify + rollback:
   `../deploy/MIGRATION.md`. Deploy is the maintainer's via the `ha`-skill ssh workflow.

## Safety

Every command on the card is SAFE/`[CAP]`-verified. The danger tier
(flash · dnd · music · camera · call) is **not** exposed. Firmware flashing is
CLI-only (no verified un-brick path). Dark + light follow the active HA theme.
