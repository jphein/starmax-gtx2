"""Shared entity base for the gtx2 integration.

CONTRACT: entity ids are PINNED explicitly (`self.entity_id`) so the registry can't area-prefix or
dedupe-suffix them — the dashboards bind these exact ids. `_attr_unique_id` matches the retired
template unique_ids so any registry customizations (names, areas) carry over on migration.
"""
from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo, Entity

from .const import DOMAIN


class Gtx2Entity(Entity):
    _attr_should_poll = False

    def __init__(self, hub, watch: str | None, key: str, name: str, platform: str) -> None:
        self._hub = hub
        self._watch = watch
        self._key = key
        slug = f"gtx2_{watch}_{key}" if watch else f"gtx2_{key}"
        self._attr_unique_id = slug
        self.entity_id = f"{platform}.{slug}"
        self._attr_name = name
        ident = f"watch_{watch}" if watch else "hub"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, ident)},
            name=f"GTX2 {hub.watches[watch]['name']}" if watch else "GTX2 Hub",
            manufacturer="Starmax", model="GTX2")

    async def async_added_to_hass(self) -> None:
        self._hub.add_listener(self.async_write_ha_state)
