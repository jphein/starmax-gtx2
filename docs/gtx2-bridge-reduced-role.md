# GTX2 host-bridge — reduced role (post node-migration)

**Status:** target architecture, prepared ahead of the cutover. The cutover is **gated on the ESP
nodes being proven on hardware** (tasks #13/#14/#17) and the node-native paths landing (#37
node dial-push, #35/#19 routing wrappers). Until then the bridge keeps its full role; this doc +
the manifest `role` labels are what make the demotion a one-switch change when the gate opens.

Owner: bridge lane · Relates: #19, #20, #35 · Supersedes the "bridge = the HA⇄GTX2
gateway" framing in `ha-bridge/README.md`.

---

## TL;DR

The **ESP32-C3 `gtx2_client` nodes are the production path** and *supersede* the range-limited
host-bridge for everything they can do (find, set-time, weather, health, dial-list/switch,
alarms, LE inputs, presence). The bridge is **demoted**:

- **No longer the default fallback** for node-capable commands. Dashboard actions route to the
  node holding the watch (#35).
- Its **permanent, irreducible job shrinks to two things a C3 physically cannot do:**
  1. **Off-node image render for dial-push** — render a notification/dial face → RGB565+LZ4 blob,
     and *serve* it. The **node** fetches the blob and streams the D-plane push (#37). The bridge
     is a **one-shot render service, not a live BLE bridge**, for this.
  2. **Text-notify via a classic-BT (HFP) companion** — real phone-style notifications
     (`0x11`/`0x13`). **LE-impossible**, so no C3 node can ever do it; only a classic-BR/EDR host can.
- The **full command surface stays callable but is *labeled* last-resort** — used only for a watch
  that **no node currently holds** (out of every room's BLE range).

---

## Why these two jobs are structural, not arbitrary

| Job | Why the LE-only C3 node can't do it | Source |
|---|---|---|
| **Render a watch-face blob** | Needs Pillow + an RGB565/LZ4 image codec and ~12 KB of blob RAM; there is no clean way to build a ~12 KB image on an ESP32-C3. | `esphome/README.md` §Watchface notifications; `faces.py` |
| **Text notifications** | "App connected" *is* a **classic BR/EDR HFP companion** link (SSP bond → RFCOMM → HFP SLC). Full LE handshake + `0x04` enable + `0x11` on a held LE link **still shows "app not connected"** and displays nothing. A C3 is **LE-only** → can never be the classic companion. | `docs/notifications.md` §3 |

The D-plane *streaming* itself is portable (proven byte-identical in the node's host test), so the
**push** moves to the node; only the **render** (blob production) is stuck on the host. That split
is exactly #15 / PR #37: *off-node render → node `http_request` fetch → node D-plane push.*

---

## Per-command ownership map (the manifest `role` label)

Each catalog command carries a machine-readable `role` (+ `fallback_only`) in the manifest so
the dashboard and the #35 routing wrappers key off it instead of hard-coding names.

Bridge catalog command ⇄ node method name (they differ): `find`⇄`buzz`/`stop_buzz`,
`set-time`⇄`sync_time`, `sync-health`⇄`read_health`, `dial-list`⇄`read_state` (active-face/dials),
`weather`⇄`push_weather`, `alarm-set`⇄`set_alarm`, `dial-switch`⇄`switch_dial`, `activate`⇄on-connect
bind. The node's `release_link` has **no bridge equivalent** (the bridge disconnects after each
command; it never holds a persistent link).

| `role` | `fallback_only` | Meaning | Commands |
|---|---|---|---|
| **`node`** (+fallback) | `true` | Node holding the watch is primary; the **bridge runs it only if no node holds the watch** (the wrapper carries an `fb_script`). | `find` · `activate` · `set-time` · `weather` · `sync-health` |
| **`node`** (node-only) | `false` | Node-delegated but **no host-bridge fallback** — the wrapper has no `fb_script`, so it **no-ops with a warning** when no node holds the watch. Node-only by design (#21 ruling b): not time-critical, and we're retiring bridge scripts, not adding them. | `alarm-set` · `dial-switch` · `dial-list` |
| **`render`** | `false` | **Permanent bridge job.** Produce the face blob off-node; the node pushes it. | `notify` (render half) · `dial-push` render input |
| **`classic-notify`** | `false` | **Permanent bridge job**, LE-impossible on a node. *Reserved* — no live command yet (see Roadmap). | *(reserved)* |
| **`host`** | `false` | Bridge-only for now: advanced/file ops + settings with **no node method yet** (candidates for #19). | `dial-push` (from host file) · `feature-bitmap` · `user-profile` · `alarm-get` · `setting-query` · `aod` · `world-clock` · `date-format` · `sport-goals` · `device-state` · `wrist-raise` · `sedentary` · `drink-water` · `event-reminders` |
| **`host` (RED)** | `false` | DANGER tier — bridge-only, unchanged, still confirm+force gated. Several are classic-BT-layer anyway. | `flash-firmware` · `dnd` · `call` · `music` · `camera` |

Notes:
- `notify` is a **hybrid today** (renders *and* pushes over BLE). Post-#37 the bridge does the
  render only; the node does the push. It is labeled `render` because rendering is its permanent
  half. The live-push half survives as a `fallback_only` last-resort path.
- **node+fallback** commands are **not removed** from the bridge — it still executes them, but only
  as the labeled last-resort (`fb_script`). That preserves the "watch is in no room" case (#35's
  last checkbox). **node-only** commands (`alarm-set`/`dial-switch`/`dial-list`) have no bridge
  fallback: when no node holds the watch they no-op with a warning (the manifest exposes them under
  `node_only`, distinct from `fallback_only`).
- `activate` is node-delegated because the node runs the bind handshake **on connect** for every
  watch it claims; the bridge's `activate` stays as the last-resort for a fresh-from-box watch that
  no node can yet claim.
- `role="host"` commands have **no node method yet**; #19 may add some, flipping them to `node`.
  Until then the bridge is their only path — which is fine, they're not on the hot dashboard rows.

## Sensors (for completeness)

Health/state sensors (heart_rate, spo2, steps, distance, calories, firmware, active-face,
connected, RSSI) are now published by the **node** for the watch it holds; the bridge's
`sync-health`/`read-state` is the last-resort read. `battery` remains **unsupported** (no opcode)
on both. Continuous room presence is owned by the HA-side passive-RSSI computation
(`ha-bridge/presence/`), not the bridge.

---

## What does NOT change

- **Safety tiers + the accidental-brick guard** (`catalog.py`, `tests/test_catalog.py`): green/
  yellow/red, danger commands off-dashboard, confirm+force gating. The `role` label is *additive*.
- **Offline render + tests**: `gtx2-bridge render …` / `send notify … --dry-run` stay the render
  workhorse (now feeding the node instead of pushing directly).
- **MQTT topic contract** (`gtx2/cmd`, `gtx2/result`, `gtx2/registry`, availability, per-watch
  state/health): unchanged. The `gtx2_notify.yaml` package stays valid.

---

## Cutover sequencing (who does what, in order)

The bridge demotion is the **last** step and must not run before its replacements are proven:

1. **Nodes proven on hardware** — #13 (office recover), #14 (bedroom claims a watch), #17
   (per-instance enable-on-connect), #16 (un-draft + merge #36). *(owner: node lane)*
2. **Node-native notify** — #37 node D-plane dial-push lands + off-node blob delivery (#15).
   *(owner: node lane)*
3. **Routing wrappers** — #35/#19: `script.gtx2_{daily,spare}_<action>` resolve the holding node
   and call its `esphome.*` service; host-bridge kept ONLY as the no-node fallback.
   *(owner: node lane)*
4. **Dashboard swap** — the dashboard flips each action-path host-bridge → node in one line, reading the
   `role` label from the manifest. *(owner: dashboard lane)*
5. **Bridge demotion (this task, #20)** — land the `role`/`fallback_only` labels in `catalog.py`
   + manifest, update `README.md` to the reduced role, ship this doc. *(owner: bridge lane)*

Steps 1–4 are prerequisites for the *behavioural* cutover; **step 5's label + docs are safe to
land first** (additive, non-breaking) so 3–4 have a stable contract to bind against.

---

## Roadmap — the classic-BT text-notify companion (job 2)

Currently **out of scope** for the bridge (it runs on `bleak`, LE-only — same gate as the CLI).
To actually deliver job 2 the host needs a **classic BR/EDR HFP companion** (a Gadgetbridge-style
stack, or GB itself on an Android/host coordinator). This is the *only* place real text
notifications can ever live, which is why it is reserved as a permanent bridge role even though it
is not yet implemented. Tracked separately; not part of #20.
