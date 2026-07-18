"""Unified gtx2 scheduler — one coordinator replacing gtx2_periodic_sync + gtx2_weatherface_live +
gtx2_gridwatts_face.

- /30 min: sync_time to EVERY configured watch (node-first, host-bridge fallback), then ONE shared
  weather frame pushed to every PRESENT watch — gated on switch.gtx2_weatherface_live, YIELDING
  (no weather push) when switch.gtx2_gridwatts_face is on (gridwatts owns the 0x12 city line).
- Gridwatts: state-change on the configured grid sensor + /2 min heartbeat -> logic.gridwatts_should_push
  (100 W deadband, >=90 s min interval, /120 s heartbeat) -> facepush.push_face to the target watch.
- On gridwatts disable: restore the weather face (switch_dial 25023) + repaint weather (gridwatts YAML).
- NO homeassistant-start trigger (deliberately removed upstream — reload spam clobbered calibration).
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

from . import facepush, logic, router
from .const import DEFAULT_DIAL_ID

_LOGGER = logging.getLogger(__name__)
SYNC_INTERVAL = timedelta(minutes=30)
GRID_HEARTBEAT = timedelta(minutes=2)
WEATHER_DIAL_ID = 25023          # restore/weather face (gtx2_gridwatts_face.yaml)
_UNAVAIL = ("unknown", "unavailable", "none", "")


class Gtx2Scheduler:
    def __init__(self, hass: HomeAssistant, hub, *, weather_entity: str, grid_source: str,
                 grid_target: str, grid_max_w: int, gridkw_gate: str,
                 gridkw_targets) -> None:
        self.hass = hass
        self.hub = hub
        self.weather_entity = weather_entity
        self.grid_source = grid_source
        self.grid_target = grid_target
        self.grid_max_w = grid_max_w
        # Live-kW RTC feature (MULTI-TARGET: daily + watch3 mirror): while `gridkw_gate` is on,
        # the live-kW automation owns the RTC of every watch in `gridkw_targets`; the scheduler must leave
        # those watches' clock/date/face alone (else the /30 sync clobbers day=kW — the "16" flicker).
        self.gridkw_gate = gridkw_gate
        self.gridkw_targets = gridkw_targets
        self._unsubs: list = []

    def _is_on(self, eid: str) -> bool:
        st = self.hass.states.get(eid)
        return st is not None and st.state == "on"

    async def async_start(self) -> None:
        self._unsubs.append(async_track_time_interval(self.hass, self._sync_tick, SYNC_INTERVAL))
        self._unsubs.append(async_track_time_interval(self.hass, self._grid_tick, GRID_HEARTBEAT))
        self._unsubs.append(async_track_state_change_event(
            self.hass, [self.grid_source], self._grid_change))
        self._unsubs.append(async_track_state_change_event(
            self.hass, ["switch.gtx2_gridwatts_face"], self._grid_switch))

    async def async_stop(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs.clear()

    # ---------------------------------------------------------------- /30 time + weather
    async def _sync_tick(self, _now) -> None:
        gate_on = self._is_on(self.gridkw_gate)
        for w in self.hub.watches:
            if logic.gridkw_owns_rtc(w, gate_on, self.gridkw_targets):
                continue  # live-kW owns this RTC — sync_time would overwrite day=kW with the real date
            await router.route(self.hass, self.hub, w, "sync_time", node_data={})
        # Weather refresh: gated on weatherface_live; yield to gridwatts (it owns the screen).
        if not self._is_on("switch.gtx2_weatherface_live"):
            return
        if self._is_on("switch.gtx2_gridwatts_face"):
            return
        frame = await router.fetch_weather_frame(self.hass, self.weather_entity)
        if frame is None:
            _LOGGER.debug("scheduler: weather push skipped (%s unavailable)", self.weather_entity)
            return
        for w in self.hub.watches:
            if self.hub.data[w]["present"] and not logic.gridkw_owns_rtc(w, gate_on, self.gridkw_targets):
                await router.route(self.hass, self.hub, w, "push_weather",
                                   node_data=frame, fb_data=frame)

    # ---------------------------------------------------------------- gridwatts
    def _grid_watts(self) -> int | None:
        st = self.hass.states.get(self.grid_source)
        if st is None or st.state in _UNAVAIL:
            return None
        try:
            return int(round(float(st.state)))
        except (TypeError, ValueError):
            return None

    async def _grid_push(self) -> None:
        if not self._is_on("switch.gtx2_gridwatts_face"):
            return
        if logic.gridkw_owns_rtc(self.grid_target, self._is_on(self.gridkw_gate), self.gridkw_targets):
            return  # live-kW owns the target watch — don't push the chunked gauge over its RTC face
        watts = self._grid_watts()
        if watts is None:
            return
        tgt = self.grid_target
        if tgt not in self.hub.watches or not self.hub.data[tgt]["present"]:
            return  # target not linked — nothing to push to
        now_ts = time.monotonic()
        last_w = self.hub.grid_last_w
        should = last_w is None or logic.gridwatts_should_push(
            last_w, watts, self.hub.grid_last_push_ts, now_ts)
        if not should:
            return
        await facepush.push_face(self.hass, self.hub, tgt, watts, DEFAULT_DIAL_ID,
                                 self.grid_max_w)
        self.hub.grid_last_w = watts
        self.hub.grid_last_push_ts = now_ts

    @callback
    def _grid_tick(self, _now) -> None:
        self.hass.async_create_task(self._grid_push())

    @callback
    def _grid_change(self, _event) -> None:
        self.hass.async_create_task(self._grid_push())

    @callback
    def _grid_switch(self, event) -> None:
        new = event.data.get("new_state")
        old = event.data.get("old_state")
        if (new is not None and new.state == "off" and old is not None and old.state == "on"):
            self.hass.async_create_task(self._grid_restore())

    async def _grid_restore(self) -> None:
        tgt = self.grid_target
        if tgt not in self.hub.watches:
            return
        await router.route(self.hass, self.hub, tgt, "switch_dial",
                           node_data={"dial_id": WEATHER_DIAL_ID})
        frame = await router.fetch_weather_frame(self.hass, self.weather_entity)
        if frame is not None:
            await router.route(self.hass, self.hub, tgt, "push_weather",
                               node_data=frame, fb_data=frame)
