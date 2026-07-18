"""Route gtx2.* service calls: holding node first, MQTT host-bridge fallback second.

Port of the deployed gtx2_cmd_route engine (gtx2_command_routing.yaml): if an online node holds the
watch, drive its ESPHome service; else publish the host-bridge fallback on gtx2/cmd (for the actions
that have one); else warn + record "no route". `allow_fallback=False` reproduces the node-only
wrappers (e.g. push_text_label rides push_weather but the wrapper carries no fb_script).
"""
from __future__ import annotations

import json
import logging

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant

from . import logic
from .const import MQTT_CMD_TOPIC, NODE_SUFFIX, WEATHER_ENTITY

_LOGGER = logging.getLogger(__name__)


async def route(hass: HomeAssistant, hub, watch: str, action: str, node_data: dict | None = None,
                fb_data: dict | None = None, allow_fallback: bool = True) -> bool:
    holder = hub.data[watch]["holder"]
    if holder:
        service = f"{holder}_{watch}_{NODE_SUFFIX[action]}"
        # Serialize on the holder's radio: queue (don't drop) behind any in-flight dial-push there.
        async with hub.node_lock(holder):
            await hass.services.async_call("esphome", service, node_data or {}, blocking=True)
        hub.set_last_result(f"{action}: ok via {holder}")
        return True
    if allow_fallback:
        mac = hub.watches[watch].get("mac", "")
        payload = logic.fallback_payload(action, mac, fb_data if fb_data is not None else (node_data or {}))
        if payload and mac:
            await mqtt.async_publish(hass, MQTT_CMD_TOPIC, json.dumps(payload))
            hub.set_last_result(f"{action}: sent via host-bridge")
            return True
    _LOGGER.warning("gtx2 no route for %s %s: no online node holds the watch and no fallback",
                    watch, action)
    hub.set_last_result(f"{action}: no route")
    return False


async def fetch_weather_frame(hass: HomeAssistant, weather_entity: str = WEATHER_ENTITY) -> dict | None:
    """Fetch the live weather entity + daily forecast and build the ONE shared frame.

    Mirrors gtx2_periodic_sync.yaml: weather.home is °F -> logic.weather_frame converts to °C, maps
    the condition via the unified #25 table, and returns None when the entity is unavailable so the
    caller SKIPS the push entirely (never 0/0/0/0).
    """
    st = hass.states.get(weather_entity)
    state = st.state if st else None
    cur = st.attributes.get("temperature") if st else None
    if cur is None:
        cur = 0.0
    hi = lo = cur
    try:
        resp = await hass.services.async_call(
            "weather", "get_forecasts", {"type": "daily"},
            target={"entity_id": weather_entity}, blocking=True, return_response=True)
        forecast = (resp or {}).get(weather_entity, {}).get("forecast") or []
        if forecast:
            today = forecast[0]
            hi = today.get("temperature", cur)
            lo = today.get("templow", cur)
    except Exception as err:  # noqa: BLE001 — forecast is best-effort; current temp is the floor
        _LOGGER.debug("weather forecast fetch failed (%s); using current temp for hi/lo", err)
    return logic.weather_frame(state, cur, hi, lo)
