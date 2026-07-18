"""Select platform — push target (which watch) + watch-face dial choice.

Replaces input_select.gtx2_push_target / gtx2_dial. The dial option label carries the id in
"Name (ID)" form; the switch_dial service (Task 6) parses the id out. Persist via RestoreEntity.
"""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .entity import Gtx2Entity

# Market-range faces, "Name (ID)" — parsed by switch_dial (Task 6). From gtx2_push_controls.yaml.
DIAL_OPTIONS = ["Test (25022)", "Weather (25023)"]
DIAL_DEFAULT = "Weather (25023)"


class Gtx2Select(Gtx2Entity, SelectEntity, RestoreEntity):
    def __init__(self, hub, watch, key, name, options, current, *, icon=None) -> None:
        super().__init__(hub, watch, key, name, "select")
        self._attr_options = options
        self._attr_current_option = current
        self._attr_icon = icon

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last is not None and last.state in self._attr_options:
            self._attr_current_option = last.state

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        self.async_write_ha_state()


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]
    watches = list(hub.watches)
    target_default = "daily" if "daily" in watches else (watches[0] if watches else "daily")
    async_add_entities([
        Gtx2Select(hub, None, "push_target", "GTX2 Push Target", watches, target_default,
                   icon="mdi:watch"),
        Gtx2Select(hub, None, "dial", "GTX2 Watch Face", list(DIAL_OPTIONS), DIAL_DEFAULT,
                   icon="mdi:watch"),
    ])
