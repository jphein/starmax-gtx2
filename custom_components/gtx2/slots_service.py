"""[CFW] gtx2.set_slot / gtx2.set_text_slot service handlers — SKELETON (design §4.3).

Routes generic value-slot pushes (opcode 0xA2, the arbitrary-data channel) to the holding node's
ESPHome ``set_slots`` / ``set_text_slot`` action. That node action exists ONLY on CFW firmware, so on
stock firmware these services warn + no-op (never raise). The on-wire 0xA2 frame is built ON THE
NODE (ESPHome ``gtx2_proto::build_set_slots``); the host-direct Python twin is
:mod:`starmax_client.slots`. HA only routes the field values — it does not build BLE frames — so this
module has no ``starmax_client`` import dependency.

⚠️ NOT WIRED YET (deliberate). ``__init__.py`` does not call this. Wiring is one line, left for
review so slot services land WITH the CFW node action, not before::

    from .slots_service import async_register_slot_services
    async_register_slot_services(hass, hub)   # in async_setup_entry, after the hub is built

Registered services are described in ``services.yaml`` (``set_slot`` / ``set_text_slot``).
"""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Mirror starmax_client.slots / firmware cfw_slots.h (design §1.1). Kept local so the component has
# no starmax_client dependency — the NODE builds the frame; HA routes the values.
NUM_SLOTS = 8
TXT_SLOTS = 2
DEC_MAX = 3
TXT_MAX_LEN = 19          # CFW_TXT_LEN - 1 (NUL)

SERVICE_SET_SLOT = "set_slot"
SERVICE_SET_TEXT_SLOT = "set_text_slot"

SET_SLOT_SCHEMA = vol.Schema({
    vol.Required("watch"): cv.string,
    vol.Required("index"): vol.All(vol.Coerce(int), vol.Range(min=0, max=NUM_SLOTS - 1)),
    vol.Required("value"): vol.All(vol.Coerce(int), vol.Range(min=-(2 ** 31), max=2 ** 31 - 1)),
    vol.Optional("decimals", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=DEC_MAX)),
})

SET_TEXT_SLOT_SCHEMA = vol.Schema({
    vol.Required("watch"): cv.string,
    vol.Required("index"): vol.All(vol.Coerce(int), vol.Range(min=0, max=TXT_SLOTS - 1)),
    vol.Required("text"): cv.string,
})


def async_register_slot_services(hass: HomeAssistant, hub) -> None:
    """Register gtx2.set_slot / gtx2.set_text_slot. Call from async_setup_entry after the hub exists
    (left un-wired pending the CFW node action — see module docstring)."""

    async def _set_slot(call: ServiceCall) -> None:
        watch = call.data["watch"]
        idx, value, dec = call.data["index"], call.data["value"], call.data["decimals"]
        await _route_node(hass, hub, watch, "set_slots",
                          {"slots": [{"index": idx, "value": value, "decimals": dec}]},
                          f"set_slot[{idx}]={value} dec={dec}")

    async def _set_text_slot(call: ServiceCall) -> None:
        watch = call.data["watch"]
        idx, text = call.data["index"], call.data["text"][:TXT_MAX_LEN]
        await _route_node(hass, hub, watch, "set_text_slot",
                          {"index": idx, "text": text}, f"set_text_slot[{idx}]={text!r}")

    if not hass.services.has_service(DOMAIN, SERVICE_SET_SLOT):
        hass.services.async_register(DOMAIN, SERVICE_SET_SLOT, _set_slot, schema=SET_SLOT_SCHEMA)
    if not hass.services.has_service(DOMAIN, SERVICE_SET_TEXT_SLOT):
        hass.services.async_register(DOMAIN, SERVICE_SET_TEXT_SLOT, _set_text_slot,
                                     schema=SET_TEXT_SLOT_SCHEMA)


async def _route_node(hass: HomeAssistant, hub, watch: str, node_action: str, data: dict,
                      desc: str) -> None:
    """Route a slot push to the holding node's CFW ESPHome action; warn + no-op if the watch is
    unheld or the node action is absent (stock firmware). STUB: the node ``set_slots`` /
    ``set_text_slot`` action lands with the CFW firmware — until then this logs intent + records
    last_result, never raising."""
    holder = hub.data.get(watch, {}).get("holder") if getattr(hub, "data", None) else None
    if not holder:
        _LOGGER.warning("gtx2.%s: no node holds '%s' — slot push dropped", node_action, watch)
        _set_last_result(hub, f"{desc}: no holder")
        return
    service = f"{holder}_{watch}_{node_action}"
    if not hass.services.has_service("esphome", service):
        _LOGGER.warning("gtx2.%s: node action esphome.%s absent (stock fw — CFW-only) — no-op",
                        node_action, service)
        _set_last_result(hub, f"{desc}: node action absent (CFW-only)")
        return
    await hass.services.async_call("esphome", service, data, blocking=True)
    _set_last_result(hub, f"{desc}: ok via {holder}")


def _set_last_result(hub, text: str) -> None:
    setter = getattr(hub, "set_last_result", None)
    if callable(setter):
        setter(text)
