"""Sensor platform — per-watch room/metrics/holder + hub status.

Units/icons ported verbatim from the retired gtx2_presence.yaml so the migrated entities read
identically. Table-driven off METRICS — no per-metric copy-paste.
"""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity

from .const import DOMAIN, METRICS
from .entity import Gtx2Entity

# metric key -> (friendly suffix, unit, icon) — from gtx2_presence.yaml.
METRIC_META: dict[str, tuple[str, str | None, str]] = {
    "heart_rate": ("Heart Rate", "bpm", "mdi:heart-pulse"),
    "spo2": ("SpO2", "%", "mdi:lungs"),
    "steps": ("Steps", None, "mdi:shoe-print"),
    "distance": ("Distance", None, "mdi:map-marker-distance"),
    "calories": ("Calories", "kcal", "mdi:fire"),
    "link_rssi": ("Link RSSI", "dBm", "mdi:wifi"),
    "firmware": ("Firmware build", None, "mdi:chip"),
    "active_face": ("Active Face", None, "mdi:watch-variant"),
}


class Gtx2Sensor(Gtx2Entity, SensorEntity):
    """Sensor whose value is pulled from the hub via a value function (entity -> value)."""

    def __init__(self, hub, watch, key, name, value_fn, *, unit=None, icon=None) -> None:
        super().__init__(hub, watch, key, name, "sensor")
        self._value_fn = value_fn
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon

    @property
    def native_value(self):
        return self._value_fn(self)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]
    ents: list[Gtx2Sensor] = []
    for w in hub.watches:
        wname = hub.watches[w]["name"]
        ents.append(Gtx2Sensor(hub, w, "room", f"GTX2 {wname} Room",
                               lambda e: e._hub.data[e._watch]["room"],
                               icon="mdi:map-marker-radius"))
        for metric in METRICS:
            friendly, unit, icon = METRIC_META[metric]
            ents.append(Gtx2Sensor(hub, w, metric, f"GTX2 {wname} {friendly}",
                                   lambda e: e._hub.data[e._watch]["metrics"][e._key],
                                   unit=unit, icon=icon))
        ents.append(Gtx2Sensor(hub, w, "holder", f"GTX2 {wname} Holder",
                               lambda e: e._hub.data[e._watch]["holder"] or "none",
                               icon="mdi:access-point"))
    # Hub status sensors (MQTT-fed in Task 6).
    ents.append(Gtx2Sensor(hub, None, "detected_watches", "GTX2 Detected Watches",
                           lambda e: e._hub.detected_watches, icon="mdi:watch-vibrate"))
    ents.append(Gtx2Sensor(hub, None, "last_result", "GTX2 Last Result",
                           lambda e: e._hub.last_result, icon="mdi:console"))
    async_add_entities(ents)
