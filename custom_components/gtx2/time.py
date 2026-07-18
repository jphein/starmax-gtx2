"""Time platform — alarm time (HH:MM). Replaces input_datetime.gtx2_alarm_time (time-only)."""
from __future__ import annotations

from datetime import time as dt_time

from homeassistant.components.time import TimeEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .entity import Gtx2Entity

_INVALID = (None, "unknown", "unavailable")
_DEFAULT = dt_time(7, 0)


class Gtx2Time(Gtx2Entity, TimeEntity, RestoreEntity):
    def __init__(self, hub, watch, key, name, *, default=_DEFAULT, icon=None) -> None:
        super().__init__(hub, watch, key, name, "time")
        self._attr_icon = icon
        self._attr_native_value = default

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last is not None and last.state not in _INVALID:
            try:
                hh, mm, *_ = last.state.split(":")
                self._attr_native_value = dt_time(int(hh), int(mm))
            except (TypeError, ValueError):
                pass

    async def async_set_value(self, value: dt_time) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        Gtx2Time(hub, None, "alarm_time", "GTX2 Alarm Time", default=_DEFAULT, icon="mdi:alarm"),
    ])
