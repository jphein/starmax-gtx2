"""Switch platform — screen-break (×watch) + feature gates (gridwatts / weatherface / alarm).

Feature-gate switches are self-managed state holders (the scheduler in Task 8 reads their state).
Screen-break switches delegate start/cancel to the ScreenBreakController wired onto the hub in
Task 9 — they no-op gracefully until that controller exists. All persist via RestoreEntity.
"""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .entity import Gtx2Entity


class Gtx2Switch(Gtx2Entity, SwitchEntity, RestoreEntity):
    def __init__(self, hub, watch, key, name, *, icon=None, default=False) -> None:
        super().__init__(hub, watch, key, name, "switch")
        self._attr_icon = icon
        self._attr_is_on = default

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last is not None:
            self._attr_is_on = last.state == "on"

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()
        await self._apply(True)

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()
        await self._apply(False)

    async def _apply(self, on: bool) -> None:
        """Feature-gate default: state only (scheduler reads it). Subclasses override."""


class Gtx2ScreenBreakSwitch(Gtx2Switch):
    async def _apply(self, on: bool) -> None:
        ctrl = getattr(self._hub, "screen_break", None)
        if ctrl is None:
            return
        if on:
            await ctrl.async_start(self._watch)
        else:
            await ctrl.async_cancel(self._watch)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]
    ents: list[Gtx2Switch] = []
    for w in hub.watches:
        wname = hub.watches[w]["name"]
        ents.append(Gtx2ScreenBreakSwitch(hub, w, "screen_break", f"GTX2 {wname} Screen Break",
                                          icon="mdi:television-off"))
    ents.append(Gtx2Switch(hub, None, "gridwatts_face", "GTX2 Gridwatts Face", icon="mdi:gauge"))
    # weatherface_live defaults ON (gtx2_weatherface_live.yaml initial: "on") — a background
    # keep-current gate. RestoreEntity overrides this after the first run.
    ents.append(Gtx2Switch(hub, None, "weatherface_live", "GTX2 Weatherface Live",
                           icon="mdi:weather-partly-cloudy", default=True))
    ents.append(Gtx2Switch(hub, None, "alarm_enabled", "GTX2 Alarm Enabled", icon="mdi:alarm-check"))
    async_add_entities(ents)
