"""Health + device metrics — the Gadgetbridge-parity sensor surface.

Every reader here REUSES the library's already-ported decoders: ``records.extract_*`` for
HR/SpO2/sleep/stress/HRV/activity,
``workout.parse_workout_klv`` for workouts, ``base.parse_state_reply`` for the device build
stamp, and ``files.parse_dial_list_reply`` for the active face + flash usage. This module only
orchestrates the reads and shapes the result for MQTT/HA — it decodes nothing itself.

``SENSORS`` is the data-driven manifest the dashboard binds MQTT sensors to. Each entry says
which JSON key on which topic (``health`` or ``state``) carries the value, plus HA hints
(unit / device_class / icon). ``unsupported=True`` marks a GB feature with no read path on this
firmware yet (battery — no opcode exists anywhere in the protocol or the library).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from starmax_client import records, workout
from starmax_client.commands import base, files
from starmax_client.commands.base import (CAT_ACTIVITY, CAT_ACTIVITY_HR, CAT_HRV, CAT_SLEEP,
                                          CAT_SPO2, CAT_STRESS, CAT_WORKOUT, OP_HEALTH_SYNC)


@dataclass
class Sensor:
    key: str                 # JSON key on the source topic
    name: str                # HA friendly name suffix
    source: str              # "health" | "state" | "link"
    unit: Optional[str] = None
    device_class: Optional[str] = None
    state_class: Optional[str] = None
    icon: Optional[str] = None
    binary: bool = False
    unsupported: bool = False

    def as_manifest(self) -> dict:
        return {"key": self.key, "name": self.name, "source": self.source, "unit": self.unit,
                "device_class": self.device_class, "state_class": self.state_class,
                "icon": self.icon, "binary": self.binary, "unsupported": self.unsupported}


# ---------------------------------------------------------------------------- the sensor manifest
SENSORS: List[Sensor] = [
    # --- health (from a sync-health read) ---
    Sensor("heart_rate", "Heart Rate", "health", "bpm", None, "measurement", "mdi:heart-pulse"),
    Sensor("spo2", "SpO2", "health", "%", None, "measurement", "mdi:water-percent"),
    Sensor("sleep_minutes", "Sleep", "health", "min", "duration", "measurement", "mdi:sleep"),
    Sensor("steps", "Steps", "health", "steps", None, "total_increasing", "mdi:shoe-print"),
    Sensor("distance_m", "Distance", "health", "m", "distance", "total_increasing", "mdi:map-marker-distance"),
    Sensor("calories", "Calories", "health", "kcal", None, "total_increasing", "mdi:fire"),
    Sensor("stress", "Stress", "health", None, None, "measurement", "mdi:emoticon-stressed-outline"),
    Sensor("hrv", "HRV", "health", "ms", None, "measurement", "mdi:heart-cog"),
    Sensor("workout_summary", "Last Workout", "health", None, None, None, "mdi:run"),
    # --- device state (from bind descriptor / 0x05 / 0x16) ---
    Sensor("connected", "Connected", "state", None, "connectivity", None, "mdi:bluetooth", binary=True),
    Sensor("firmware", "Firmware", "state", None, None, None, "mdi:chip"),
    Sensor("active_dial", "Active Face", "state", None, None, None, "mdi:watch"),
    Sensor("flash_used_kb", "Flash Used", "state", "kB", "data_size", "measurement", "mdi:memory"),
    # --- link (from the scan / registry) ---
    Sensor("rssi", "Signal", "link", "dBm", "signal_strength", "measurement", "mdi:signal"),
    Sensor("last_seen", "Last Seen", "link", None, "timestamp", None, "mdi:clock-outline"),
    # --- GB feature with no read path on this firmware ---
    Sensor("battery", "Battery", "state", "%", "battery", "measurement", "mdi:battery",
           unsupported=True),
]

_HEALTH_KEYS = [s.key for s in SENSORS if s.source == "health"]


def sensor_manifest() -> List[dict]:
    return [s.as_manifest() for s in SENSORS]


# ---------------------------------------------------------------------------- live readers
def _first_positive(seq) -> Optional[int]:
    for v in seq or ():
        if v:
            return int(v)
    return None


async def read_health(client) -> Dict[str, object]:
    """Best-effort pull of every health metric. Never raises — a failed category → key absent.

    Returns a dict with (a subset of) ``_HEALTH_KEYS``. Reuses the GB-parity decoders in
    ``records`` / ``workout``; each category is an independent 0x0e read.
    """
    out: Dict[str, object] = {}

    async def _pull(cat: int):
        try:
            fr = await client.request(base.build_health_sync(cat), OP_HEALTH_SYNC, timeout=5.0)
            return fr.payload if fr is not None else None
        except Exception:  # noqa: BLE001 - one bad category must not sink the rest
            return None

    # cat 5 — daily activity totals (steps / distance / calories)
    p = await _pull(CAT_ACTIVITY)
    if p is not None:
        try:
            act = records.extract_activity(p)
            if act is not None:
                out.update(steps=act.steps, distance_m=act.distance_m, calories=act.calories)
        except Exception:  # noqa: BLE001
            pass

    # cat 0 — intraday heart rate
    p = await _pull(CAT_ACTIVITY_HR)
    if p is not None:
        try:
            hr = _first_positive(reversed(records.extract_heart_rates(p)))
            if hr is not None:
                out["heart_rate"] = hr
        except Exception:  # noqa: BLE001
            pass

    # cat 2 — SpO2
    p = await _pull(CAT_SPO2)
    if p is not None:
        try:
            spo2 = _first_positive(reversed(records.extract_spo2(p)))
            if spo2 is not None:
                out["spo2"] = spo2
        except Exception:  # noqa: BLE001
            pass

    # cat 3 — sleep
    p = await _pull(CAT_SLEEP)
    if p is not None:
        try:
            samples = records.extract_sleep_samples(p)
            if samples:
                out["sleep_minutes"] = len(samples)  # 1 sample ≈ 1 min (GB derives stages)
        except Exception:  # noqa: BLE001
            pass

    # cat 1 / cat 7 — stress / HRV (dated values)
    for cat, key in ((CAT_STRESS, "stress"), (CAT_HRV, "hrv")):
        p = await _pull(cat)
        if p is not None:
            try:
                vals = records.extract_dated_values(p, value_delta=8, min_value=0, max_value=255)
                if vals:
                    out[key] = int(vals[-1].value)
            except Exception:  # noqa: BLE001
                pass

    # cat 4 — last workout summary
    p = await _pull(CAT_WORKOUT)
    if p is not None:
        try:
            w = workout.parse_workout_klv(p)
            if w.head is not None:
                s = w.head
                out["workout_summary"] = (f"{s.duration_s}s {s.total_distance_m}m "
                                          f"{s.total_calories}kcal HR~{s.avg_hr}")
        except Exception:  # noqa: BLE001
            pass

    return out


async def read_state(client) -> Dict[str, object]:
    """Read device build stamp (0x05) + active face / flash usage (0x16). Best-effort."""
    out: Dict[str, object] = {"connected": True}
    try:
        fr = await client.request(base.build_state_query(), base.OP_DEVICE_STATE, timeout=5.0)
        if fr is not None:
            st = base.parse_state_reply(fr.payload)
            if st.get("firmware_build_stamp"):
                out["firmware"] = st["firmware_build_stamp"]
    except Exception:  # noqa: BLE001
        pass
    try:
        fr = await client.request(files.build_dial_list_request(), files.OP_DIAL_LIST, timeout=5.0)
        if fr is not None:
            info = files.parse_dial_list_reply(fr.payload)
            if info.get("active_dial"):
                out["active_dial"] = info["active_dial"]
            used = info.get("storage_used")
            if isinstance(used, int):
                out["flash_used_kb"] = round(used / 1024, 1)
    except Exception:  # noqa: BLE001
        pass
    return out
