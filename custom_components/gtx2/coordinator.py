"""Gtx2Hub — state-tracking aggregator.

Subscribes to the per-node ESPHome entities (`binary_sensor.<node>_<watch>_connected`,
`sensor.<node>_<watch>_<metric>`), resolves the single holder per watch (LAST match wins) and
derives per-watch contract state. Metric values are HELD through handoff gaps — when the watch is
Away (no node holds it) the last known value is kept, matching the retired presence package's
template behavior. Push-driven (not DataUpdateCoordinator): entity listeners are notified on change.
"""
from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from . import logic
from .const import METRICS

_LOGGER = logging.getLogger(__name__)


class Gtx2Hub:
    def __init__(self, hass: HomeAssistant, watches: dict[str, dict], nodes: dict[str, str]) -> None:
        self.hass = hass
        self.watches = watches            # slug -> {"name":…, "mac":…}
        self.node_rooms = nodes           # node prefix -> friendly room
        # derived state: {watch: {"room":…, "present":bool, "holder":str|None, metrics:{m:val}}}
        self.data: dict[str, dict] = {
            w: {"room": "Away", "present": False, "holder": None,
                "metrics": {m: None for m in METRICS}} for w in watches}
        # per-node derived state: {node: {"online":bool, "room":str, "holding":[watch,…]}}
        self.node_data: dict[str, dict] = {
            n: {"online": False, "room": room, "holding": []} for n, room in nodes.items()}
        self.last_result: str = "idle"
        # MQTT-fed hub status (Task 6 subscriptions write these).
        self.bridge_online: bool = False
        self.detected_watches: int = 0
        # Gridwatts push bookkeeping (scheduler, Task 8).
        self.grid_last_w: float | None = None
        self.grid_last_push_ts: float = 0.0
        self._listeners: list = []
        self._unsub = None
        # Per-node (per-radio) push locks: ONE dial-push at a time per node, and routed reads/weather
        # to that node queue behind an in-flight push (radio-contention was the real install killer).
        self._node_locks: dict[str, asyncio.Lock] = {}

    def node_lock(self, node: str) -> asyncio.Lock:
        """The asyncio.Lock serializing all radio traffic to `node` (created lazily, one per node)."""
        return logic.get_or_create(self._node_locks, node, asyncio.Lock)

    def _is_on(self, entity_id: str) -> bool:
        st = self.hass.states.get(entity_id)
        return st is not None and st.state == "on"

    def _state_of(self, entity_id: str):
        st = self.hass.states.get(entity_id)
        return st.state if st is not None else None

    async def async_start(self) -> None:
        tracked = [f"binary_sensor.{n}_{w}_connected"
                   for n in self.node_rooms for w in self.watches]
        tracked += [f"sensor.{n}_{w}_{m}"
                    for n in self.node_rooms for w in self.watches for m in METRICS]
        self._unsub = async_track_state_change_event(self.hass, tracked, self._on_change)
        self._recompute_all()

    async def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        self._listeners.clear()

    @callback
    def _on_change(self, _event) -> None:
        self._recompute_all()
        self._notify()

    def _recompute_all(self) -> None:
        for w in self.watches:
            d = self.data[w]
            d["holder"] = logic.resolve_holder(w, self._is_on, list(self.node_rooms))
            d["room"] = logic.room_for(w, self._is_on, self.node_rooms)
            d["present"] = d["holder"] is not None
            for m in METRICS:
                src = logic.metric_source(w, d["room"], m, self.node_rooms)
                if src:
                    st = self.hass.states.get(src)
                    if st and st.state not in ("unknown", "unavailable"):
                        d["metrics"][m] = st.state
                # else: HOLD last value (do not overwrite) — the presence-package contract
        # per-node derived state (holder set above, so read it back for "holding")
        for node in self.node_rooms:
            self.node_data[node] = {
                "online": logic.node_online(node, list(self.watches), self._state_of),
                "room": self.node_rooms[node],
                "holding": logic.node_holding(node, list(self.watches),
                                              lambda w: self.data[w]["holder"]),
            }

    def _notify(self) -> None:
        for cb in self._listeners:
            cb()

    def set_last_result(self, text: str) -> None:
        self.last_result = text
        self._notify()

    @callback
    def add_listener(self, cb) -> None:
        self._listeners.append(cb)
