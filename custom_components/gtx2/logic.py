"""Pure decision logic for the gtx2 integration — NO homeassistant imports (plain-pytest tested).

Everything the router, scheduler, coordinator and face-push seam decide flows through here so the
known defects (°F/°C split, cond-map default 6-vs-8, deadband) cannot re-diverge across call sites.
"""
from __future__ import annotations

import base64
from typing import Callable, List, Optional

from .const import (COND_DEFAULT, COND_MAP, GRIDWATTS_DEADBAND_W, GRIDWATTS_HEARTBEAT_S,
                    GRIDWATTS_MIN_INTERVAL_S, RAW_CHUNK_BYTES, UNAVAILABLE_STATES)

IsOn = Callable[[str], bool]


def resolve_holder(watch: str, is_on: IsOn, nodes: List[str]) -> Optional[str]:
    """Node prefix currently holding `watch` (LAST match wins, per the deployed engine)."""
    holder = None
    for node in nodes:
        if is_on(f"binary_sensor.{node}_{watch}_connected"):
            holder = node
    return holder


def room_for(watch: str, is_on: IsOn, node_rooms: dict[str, str]) -> str:
    holder = resolve_holder(watch, is_on, list(node_rooms))
    return node_rooms[holder] if holder else "Away"


def node_online(node: str, watches: List[str], state_of: Callable[[str], Optional[str]]) -> bool:
    """A node is ONLINE if ANY of its per-watch connected sources exists AND is not 'unavailable'.

    An online ESPHome node publishes its `binary_sensor.<node>_<watch>_connected` sources with a
    concrete state ('on'/'off'); an offline node's sources go 'unavailable'; a never-deployed node's
    are absent. So "exists and != unavailable" distinguishes a reachable node from a dead/absent one.
    """
    for w in watches:
        st = state_of(f"binary_sensor.{node}_{w}_connected")
        if st is not None and st != "unavailable":
            return True
    return False


def node_holding(node: str, watches: List[str],
                 holder_of: Callable[[str], Optional[str]]) -> List[str]:
    """Watch slugs currently held (BLE link) by `node` — i.e. whose resolved holder == node."""
    return [w for w in watches if holder_of(w) == node]


def metric_source(watch: str, room: str, metric: str, node_rooms: dict[str, str]) -> Optional[str]:
    """Entity id supplying `metric` for `watch`, or None when Away (caller holds last value)."""
    for node, friendly in node_rooms.items():
        if friendly == room:
            return f"sensor.{node}_{watch}_{metric}"
    return None


def weather_frame(state, temp_f, hi_f, lo_f, city: str = "Home", f_to_c: bool = True) -> Optional[dict]:
    """One frame for BOTH routes. None when the weather entity is unavailable (NEVER push 0/0/0/0)."""
    if state in UNAVAILABLE_STATES:
        return None
    conv = (lambda f: round((float(f) - 32.0) * 5.0 / 9.0)) if f_to_c else (lambda f: round(float(f)))
    return {"temp_current": conv(temp_f), "temp_max": conv(hi_f), "temp_min": conv(lo_f),
            "condition": COND_MAP.get(state, COND_DEFAULT), "city": city}


# Host-bridge fallback contract (the host bridge, MQTT gtx2/cmd). None => node-only action.
def fallback_payload(action: str, mac: str, data: dict) -> Optional[dict]:
    if action == "buzz":
        return {"command": "find", "mac": mac, "params": {"duration": int(data.get("duration", 5))}}
    if action == "sync_time":
        return {"command": "set-time", "mac": mac}
    if action == "read_health":
        return {"command": "sync-health", "mac": mac}
    if action == "activate":
        return {"command": "activate", "mac": mac}
    if action == "push_weather":
        return {"command": "weather", "mac": mac,
                "params": {"city": data["city"], "temp": data["temp_current"],
                           "hi": data["temp_max"], "lo": data["temp_min"],
                           "condition": data["condition"]}}
    if action == "push_notification":
        return {"command": "notify", "mac": mac,
                "params": {"title": data.get("title", "Notification"),
                           "body": data.get("body", ""), "footer": data.get("footer", "")}}
    return None  # stop_buzz / read_state / release_link / set_alarm / switch_dial / push_face


def chunk_blob(blob: bytes, *, raw_chunk: int = RAW_CHUNK_BYTES) -> List[dict]:
    """Slice a blob into ordered push_dial_chunk payloads.

    RESERVED utility for the D-plane chunked-install lane (Track B) — the PROVEN face-push path
    (facepush.py) stages a <8 KB face to /local/ and does NOT chunk. Kept (with its tests) because
    the chunked dial-list install may be revisited; do not delete.
    """
    total = len(blob)
    return [{"seq": seq, "total_len": total,
             "b64": base64.b64encode(blob[off:off + raw_chunk]).decode("ascii")}
            for seq, off in enumerate(range(0, total, raw_chunk))]


def get_or_create(registry: dict, key, factory):
    """Idempotent per-key registry get-or-create — the pure core of the per-node push locks.

    Returns the existing value for `key`, or creates it once via `factory()` and stores it. Used by
    Gtx2Hub.node_lock to hand out ONE asyncio.Lock per holder node (per radio), so dial-pushes and
    routed calls to the same node serialize (the install fix — radio contention was the real killer).
    """
    if key not in registry:
        registry[key] = factory()
    return registry[key]


def gridwatts_should_push(last_w: float, new_w: float, last_push_ts: float, now_ts: float) -> bool:
    elapsed = now_ts - last_push_ts
    if elapsed >= GRIDWATTS_HEARTBEAT_S:
        return True
    return abs(new_w - last_w) >= GRIDWATTS_DEADBAND_W and elapsed >= GRIDWATTS_MIN_INTERVAL_S


def gridkw_owns_rtc(watch: str, gridkw_enabled: bool, gridkw_targets) -> bool:
    """True when the live-kW writer owns `watch`'s RTC — the scheduler must then NOT sync_time or
    push_weather to it, and must not push the chunked gauge over it.

    The live-kW face encodes the value into the RTC (real hour/minute + day-of-month = round(kW),
    second/month = tenths). A scheduler sync_time (real date) or push_weather would OVERWRITE day=kW
    with the real day-of-month (JP saw "16" instead of the kW). While gridkw_live is enabled, its
    automation is the SOLE RTC writer for its target watches — everyone else must leave them alone.

    `gridkw_targets` may be a single slug OR a collection (set/list/tuple): the live-kW system is
    MULTI-TARGET (watch3 durably mirrors daily), so ALL configured targets are protected.
    """
    if not gridkw_enabled or gridkw_targets is None:
        return False
    if isinstance(gridkw_targets, str):
        return watch == gridkw_targets
    return watch in gridkw_targets


# --- install-confirm gate (chunked-push verification contract) -----------
# The node re-reads (0x16) after a push and emits sensor.gtx2_<node>_<watch>_last_install =
# "<dial_id>:<status>:<crc16hex>", status = ok|fail. Retry ownership is OURS (the node verifies +
# emits; it does NOT retry). crc is ignored per the contract — match dial_id + status only.
INSTALL_OK = "ok"
INSTALL_FAIL = "fail"
INSTALL_PENDING = "pending"
INSTALL_ABSENT = "absent"


def parse_install_status(value) -> "tuple[Optional[int], Optional[str]]":
    """Parse "<dial_id>:<status>:<crc16hex>" -> (dial_id, status_lower). (None, None) if unparseable."""
    if not value or not isinstance(value, str):
        return (None, None)
    parts = value.split(":")
    if len(parts) < 2:
        return (None, None)
    try:
        dial = int(parts[0])
    except (ValueError, TypeError):
        return (None, None)
    return (dial, parts[1].strip().lower())


def classify_install(value, expected_dial: int) -> str:
    """Classify a last_install reading vs `expected_dial`:

    - INSTALL_ABSENT : value is None — the sensor entity is not present (pre-reflash: it ships with
      the node reflash). The caller must NOT retry-loop; a sensor that can't appear never will.
    - INSTALL_OK / INSTALL_FAIL : the value matches `expected_dial` with that status.
    - INSTALL_PENDING : entity present but not yet a matching-dial ok/fail — a transient state
      ("unknown"/"unavailable"/""), a stale/other-dial result, or an unparseable value. Keep waiting
      until timeout (a timeout is treated as fail by the caller: re-push).
    """
    if value is None:
        return INSTALL_ABSENT
    if value in ("unknown", "unavailable", ""):
        return INSTALL_PENDING
    dial, status = parse_install_status(value)
    if dial == expected_dial and status == INSTALL_OK:
        return INSTALL_OK
    if dial == expected_dial and status == INSTALL_FAIL:
        return INSTALL_FAIL
    return INSTALL_PENDING
