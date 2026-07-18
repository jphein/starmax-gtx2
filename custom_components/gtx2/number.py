"""Number platform — alarm slot index (0-2). Replaces input_number.gtx2_alarm_index."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .entity import Gtx2Entity

_INVALID = (None, "unknown", "unavailable")


class Gtx2Number(Gtx2Entity, NumberEntity, RestoreEntity):
    def __init__(self, hub, watch, key, name, *, min_v, max_v, step, default,
                 icon=None, mode=NumberMode.BOX) -> None:
        super().__init__(hub, watch, key, name, "number")
        self._attr_native_min_value = min_v
        self._attr_native_max_value = max_v
        self._attr_native_step = step
        self._attr_mode = mode
        self._attr_icon = icon
        self._attr_native_value = default

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last is not None and last.state not in _INVALID:
            try:
                self._attr_native_value = float(last.state)
            except (TypeError, ValueError):
                pass

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        Gtx2Number(hub, None, "alarm_index", "GTX2 Alarm Slot",
                   min_v=0, max_v=2, step=1, default=0, icon="mdi:numeric"),
    ])
