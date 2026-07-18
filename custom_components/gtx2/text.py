"""Text platform — notify composer (title/body) + the staged push-text calibration (max 32).

Replaces the retired notify-title, notify-body and push-text input_text helpers. All persist via
RestoreEntity (user-set values). push_text default is the PII-safe synthetic "Anytown" (never seed a
real place/name).
"""
from __future__ import annotations

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .entity import Gtx2Entity

_INVALID = (None, "unknown", "unavailable")


class Gtx2Text(Gtx2Entity, TextEntity, RestoreEntity):
    def __init__(self, hub, watch, key, name, *, max_len, initial="", icon=None) -> None:
        super().__init__(hub, watch, key, name, "text")
        self._attr_native_min = 0
        self._attr_native_max = max_len
        self._attr_mode = TextMode.TEXT
        self._attr_icon = icon
        self._attr_native_value = initial

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last is not None and last.state not in _INVALID:
            self._attr_native_value = last.state[: self._attr_native_max]

    async def async_set_value(self, value: str) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        Gtx2Text(hub, None, "notify_title", "GTX2 Notify Title", max_len=40, icon="mdi:message-text"),
        Gtx2Text(hub, None, "notify_body", "GTX2 Notify Body", max_len=120,
                 icon="mdi:message-text-outline"),
        Gtx2Text(hub, None, "push_text", "GTX2 Push Text", max_len=32, initial="Anytown",
                 icon="mdi:message-text"),
    ])
