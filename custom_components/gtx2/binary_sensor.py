"""Binary-sensor platform — per-watch connected/present + hub any_present/bridge_online.

`connected` (connectivity) and `present` (presence) are BOTH required and permanent aliases: the
dashboard consumes each vocabulary for a different UI meaning (link badge vs presence chip). In the
connected-node model they're logically equal — both read hub.data[watch]["present"].
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .const import DOMAIN, node_short
from .entity import Gtx2Entity

CONNECTIVITY = BinarySensorDeviceClass.CONNECTIVITY
PRESENCE = BinarySensorDeviceClass.PRESENCE


class Gtx2BinarySensor(Gtx2Entity, BinarySensorEntity):
    def __init__(self, hub, watch, key, name, value_fn, *, device_class=None, icon=None,
                 attrs_fn=None) -> None:
        super().__init__(hub, watch, key, name, "binary_sensor")
        self._value_fn = value_fn
        self._attrs_fn = attrs_fn
        self._attr_device_class = device_class
        self._attr_icon = icon

    @property
    def is_on(self) -> bool:
        return bool(self._value_fn(self))

    @property
    def extra_state_attributes(self):
        return self._attrs_fn(self) if self._attrs_fn else None


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]
    ents: list[Gtx2BinarySensor] = []
    for w in hub.watches:
        wname = hub.watches[w]["name"]
        ents.append(Gtx2BinarySensor(hub, w, "connected", f"GTX2 {wname} Connected",
                                     lambda e: e._hub.data[e._watch]["present"],
                                     device_class=CONNECTIVITY, icon="mdi:bluetooth"))
        ents.append(Gtx2BinarySensor(hub, w, "present", f"GTX2 {wname} Present",
                                     lambda e: e._hub.data[e._watch]["present"],
                                     device_class=PRESENCE, icon="mdi:watch"))
    ents.append(Gtx2BinarySensor(hub, None, "any_present", "GTX2 Any Present",
                                 lambda e: any(d["present"] for d in e._hub.data.values()),
                                 device_class=PRESENCE, icon="mdi:watch-vibrate"))
    ents.append(Gtx2BinarySensor(hub, None, "bridge_online", "GTX2 Bridge Online",
                                 lambda e: e._hub.bridge_online,
                                 device_class=CONNECTIVITY, icon="mdi:bridge"))
    # Per-NODE online sensors (options-driven): one per configured node prefix, on the hub device.
    # online = ANY of the node's per-watch sources is present & available; attrs expose room + holds.
    for node in hub.node_rooms:
        short = node_short(node)
        ents.append(Gtx2BinarySensor(
            hub, None, f"node_{short}_online", f"GTX2 node {short.replace('_', ' ').title()}",
            lambda e, n=node: e._hub.node_data[n]["online"],
            device_class=CONNECTIVITY, icon="mdi:access-point-network",
            attrs_fn=lambda e, n=node: {"room": e._hub.node_data[n]["room"],
                                        "holding": e._hub.node_data[n]["holding"]}))
    async_add_entities(ents)
