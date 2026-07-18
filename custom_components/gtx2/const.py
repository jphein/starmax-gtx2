"""Constants for the GTX2 watch integration.

Scaffold (task #11) — the official HA home for the GTX2 watch surface that currently lives as
`/homeassistant/packages/gtx2_*.yaml` + the ha-bridge scripts. This first cut ships the
`gtx2.push_face` service (the chunked API-direct gauge push); helpers/automations migrate in later.
"""
from __future__ import annotations

DOMAIN = "gtx2"

# Watches the integration can target (mirrors input_select.gtx2_gridwatts_target for now).
WATCHES = ("daily", "spare", "watch3")

# Grid-watts gauge face (the arc-gauge, dial 25041) + JP's 0-12 kW arc scale.
DEFAULT_DIAL_ID = 25041
DEFAULT_MAX_W = 12000

# Raw bytes per chunk before base64. Reserved for the D-plane chunked-install lane (logic.chunk_blob);
# the proven /local/ face-push path (Task 7) does NOT chunk. ~6 KB -> ~8 KB base64, under the 32 KB
# ESPHome single-API-message cap.
RAW_CHUNK_BYTES = 6144

# ---------------------------------------------------------------------------
# Full-migration tables (Task 2). The pure-logic module (logic.py) reads these;
# NO homeassistant imports here so both stay plain-pytest importable.
# ---------------------------------------------------------------------------

# Node slug -> friendly room (order matters: LAST match wins, matching the deployed engine).
NODE_ROOMS: dict[str, str] = {
    "gtx2_bedroom": "Bedroom",
    "gtx2_kitchen": "Kitchen",
    "gtx2_office": "Office / Laundry",
    "gtx2_garage": "Garage",
    "gtx2_studio": "Studio",
}

# Per-watch metric surface (drives the sensor platform + coordinator, table-driven — no copy-paste).
METRICS = ("heart_rate", "spo2", "steps", "distance", "calories",
           "link_rssi", "firmware", "active_face")

# ---------------------------------------------------------------------------
# Per-NODE entity contract (options-driven "auto-add nodes"): each node prefix
# in options["nodes"] gets binary_sensor.gtx2_node_<short>_online on the hub
# device, so adding a node in the options flow auto-creates its entity. Pure
# (no HA imports) so the contract tooling can import node_entity_ids/NODE_ENTITY_IDS.
# ---------------------------------------------------------------------------
NODE_ENTITY_FMT = "binary_sensor.gtx2_node_{short}_online"


def node_short(node: str) -> str:
    """'gtx2_bedroom' -> 'bedroom' (strip the gtx2_ prefix; passthrough if absent)."""
    prefix = "gtx2_"
    return node[len(prefix):] if node.startswith(prefix) else node


def node_entity_ids(nodes) -> list[str]:
    """The per-node online entity id for each node prefix in `nodes` (dict or iterable)."""
    return [NODE_ENTITY_FMT.format(short=node_short(n)) for n in nodes]


# Static contract list for the 5 default nodes (derived; equals node_entity_ids(NODE_ROOMS)).
NODE_ENTITY_IDS: list[str] = node_entity_ids(NODE_ROOMS)

# #25 source-derived condition table (CONFIRMED 4=fog, 6=rain). Default COND_DEFAULT=8 (clear) —
# NEVER 6. Unifies the node path with the stale 0-7 host-bridge map (survey risk 5): the code is
# computed once here and passed down BOTH the node and MQTT-fallback routes.
COND_MAP = {"sunny": 8, "clear-night": 8, "partlycloudy": 3, "cloudy": 3, "overcast": 5,
            "fog": 4, "rainy": 6, "pouring": 6, "snowy": 7, "snowy-rainy": 7, "lightning": 6,
            "lightning-rainy": 6, "windy": 9, "hail": 6, "exceptional": 8}
COND_DEFAULT = 8

# Weather-entity states that mean "no data" -> skip the push entirely (never 0/0/0/0).
UNAVAILABLE_STATES = ("unknown", "unavailable", "none", "", None)

# MQTT host-bridge (the host) contract.
MQTT_CMD_TOPIC = "gtx2/cmd"
MQTT_AVAILABILITY_TOPIC = "gtx2/bridge/availability"
MQTT_REGISTRY_TOPIC = "gtx2/registry"
MQTT_RESULT_TOPIC = "gtx2/result"

# Gridwatts face push cadence (semantics from the staged gtx2_gridwatts_face.yaml).
GRIDWATTS_DEADBAND_W = 100
GRIDWATTS_MIN_INTERVAL_S = 90
GRIDWATTS_HEARTBEAT_S = 120

# action -> node ESPHome service suffix. Engine parity with gtx2_command_routing.yaml:
# the `sync_time` action drives the node's `set_time` service; everything else is 1:1.
NODE_SUFFIX = {
    "buzz": "buzz", "stop_buzz": "stop_buzz", "sync_time": "set_time",
    "set_time_custom": "set_time_custom", "read_health": "read_health",
    "read_state": "read_state", "release_link": "release_link",
    "push_weather": "push_weather", "activate": "activate", "set_alarm": "set_alarm",
    "switch_dial": "switch_dial", "push_face": "push_face",
}

# Weather source (°F; converted to °C on the wire). Hardcoded per gtx2_periodic_sync.yaml.
WEATHER_ENTITY = "weather.home"

# Face-push seam (Task 7, REVISED): the gauge is rendered in-component (render.py), staged to
# HA /config/www and fetched by the holding node over TLS from /local/. www_url_base is the VLAN8
# leg the room nodes reach. Configurable via options (Task 10).
WWW_URL_BASE_DEFAULT = "https://homeassistant.local:8123"
GRIDWATTS_SOURCE_DEFAULT = "sensor.total_grid_power"

# Live-kW RTC feature (the live-kW automation): when this gate is on, its automation is the SOLE RTC writer for
# the gridkw target watch (real hour/min + day=round(kW)); the scheduler must NOT sync_time /
# push_weather / gauge-push that watch, or it clobbers day=kW with the real date. Coordinate the
# exact entity id with the live-kW automation.
GRIDKW_GATE_DEFAULT = "input_boolean.gtx2_gridkw_live"

# blobd render-on-fetch host — used ONLY by the GATED push_text rendered-text face (#27), a separate
# (still-unsolved) feature. NOT the push_face gauge path, which uses www_url_base above.
BLOBD_BASE_DEFAULT = "http://homeassistant.local:8088"

# push_text rides the blobd render-on-fetch face at this dial id (gtx2_command_routing.yaml).
PUSH_TEXT_DIAL_ID = 25040

# Default watch roster before the options flow (Task 10) fills real MACs. NO real MAC ever here.
DEFAULT_WATCHES: dict[str, dict] = {
    "daily": {"name": "Daily", "mac": ""},
    "spare": {"name": "Spare", "mac": ""},
    "watch3": {"name": "Watch3", "mac": ""},
}

# ---------------------------------------------------------------------------
# Registered gtx2.* service names — the single source of truth. The dashboard
# lane's contract test imports ALL_SERVICES to assert every dashboard-bound
# service exists. Keep this in lockstep with the async_register calls (Task 6)
# and the media dispatch services (Task 9).
# ---------------------------------------------------------------------------
SERVICE_BUZZ = "buzz"
SERVICE_STOP_BUZZ = "stop_buzz"
SERVICE_SYNC_TIME = "sync_time"
SERVICE_SET_TIME_CUSTOM = "set_time_custom"   # RTC live-value write (day=int kW, second=tenths kW)
SERVICE_READ_HEALTH = "read_health"
SERVICE_READ_STATE = "read_state"
SERVICE_RELEASE_LINK = "release_link"
SERVICE_PUSH_WEATHER = "push_weather"
SERVICE_ACTIVATE = "activate"
SERVICE_SET_ALARM = "set_alarm"
SERVICE_SWITCH_DIAL = "switch_dial"
SERVICE_PUSH_FACE = "push_face"
SERVICE_PUSH_TEXT = "push_text"
SERVICE_PUSH_TEXT_LABEL = "push_text_label"
SERVICE_PUSH_NOTIFICATION = "push_notification"
# Media dispatch services (no-arg; bound by the dashboard media/find rows) — Task 9.
SERVICE_MEDIA_PLAY_PAUSE = "media_play_pause"
SERVICE_MEDIA_NEXT = "media_next"
SERVICE_MEDIA_PREV = "media_prev"
SERVICE_FIND_MY_PHONE = "find_my_phone"

ALL_SERVICES: tuple[str, ...] = (
    SERVICE_BUZZ, SERVICE_STOP_BUZZ, SERVICE_SYNC_TIME, SERVICE_SET_TIME_CUSTOM,
    SERVICE_READ_HEALTH, SERVICE_READ_STATE, SERVICE_RELEASE_LINK, SERVICE_PUSH_WEATHER,
    SERVICE_ACTIVATE, SERVICE_SET_ALARM, SERVICE_SWITCH_DIAL, SERVICE_PUSH_FACE,
    SERVICE_PUSH_TEXT, SERVICE_PUSH_TEXT_LABEL, SERVICE_PUSH_NOTIFICATION,
    SERVICE_MEDIA_PLAY_PAUSE, SERVICE_MEDIA_NEXT, SERVICE_MEDIA_PREV, SERVICE_FIND_MY_PHONE,
)
