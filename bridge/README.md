# gtx2-bridge — MQTT gateway (reference / legacy)

An MQTT gateway that exposes a GTX2 watch to Home Assistant from a Bluetooth host, reusing the
[`starmax_client`](../client) library for all BLE work. HA publishes a JSON command to `gtx2/cmd`;
the bridge runs it over BLE and publishes the watch registry + per-watch state/health back as
retained topics.

> **Which integration should I use?** The [`custom_components/gtx2`](../custom_components/gtx2)
> HACS integration is the primary, self-contained path and needs no external service. This MQTT
> bridge is the earlier, reference implementation — useful if you'd rather run BLE on a separate
> host and wire HA over MQTT. The face builders in [`gtx2_bridge/faces.py`](gtx2_bridge/faces.py)
> are the canonical source the component's `render.py` vendors (a byte-parity test guards drift).

## Run

```sh
# from the repo root — no install needed (uses client/ on sys.path)
PYTHONPATH=bridge:client python -m gtx2_bridge manifest        # print the command/sensor manifest
GTX2_MAC=AA:BB:CC:DD:EE:FF PYTHONPATH=bridge:client python -m gtx2_bridge serve   # run the gateway
```

Configuration is entirely environment-driven (no secrets in the repo). Common variables:

| Env | Default | Meaning |
|-----|---------|---------|
| `GTX2_MAC` | `AA:BB:CC:DD:EE:FF` | Watch MAC. `serve` refuses to act live against the placeholder. |
| `GTX2_MQTT_HOST` / `GTX2_MQTT_PORT` | `localhost` / `1883` | Broker. |
| `GTX2_MQTT_USER` / `GTX2_MQTT_PASS` | — | Broker credentials. |
| `GTX2_TOPIC_ROOT` | `gtx2` | Topic prefix. |
| `GTX2_DEFAULT_CITY` | `Anytown` | Weather label (privacy guard). |

## Requirements

`pillow`, `lz4`, `bleak` (shared with `starmax_client`), plus `paho-mqtt` for `serve` (imported
lazily — the CLI helpers and the test suite need no broker).
