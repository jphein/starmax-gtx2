"""GTX2 Watch integration — aggregator + router hub.

Builds the Gtx2Hub over the per-node ESPHome entities, forwards the entity platforms, registers the
gtx2.* routed services (holder-first, MQTT host-bridge fallback), and subscribes the MQTT status
topics. The scheduler (Task 8), media/screen-break listeners (Task 9) and frontend resource
(Task 10) are wired in their own tasks.
"""
from __future__ import annotations

import json
import logging

import voluptuous as vol
from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from . import logic, router
from .const import (BLOBD_BASE_DEFAULT, DEFAULT_DIAL_ID, DEFAULT_MAX_W, DEFAULT_WATCHES, DOMAIN,
                    GRIDKW_GATE_DEFAULT, GRIDWATTS_SOURCE_DEFAULT, MQTT_AVAILABILITY_TOPIC,
                    MQTT_CMD_TOPIC, MQTT_REGISTRY_TOPIC, MQTT_RESULT_TOPIC, NODE_ROOMS,
                    PUSH_TEXT_DIAL_ID, WEATHER_ENTITY, WWW_URL_BASE_DEFAULT)
from .coordinator import Gtx2Hub
from .frontend import async_register_frontend
from .media import MediaController
from .scheduler import Gtx2Scheduler
from .screen_break import ScreenBreakController

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH, Platform.TEXT,
             Platform.SELECT, Platform.NUMBER, Platform.TIME]


# --------------------------------------------------------------------------- entry -> config
def _watches_from_entry(entry: ConfigEntry) -> dict[str, dict]:
    w = entry.options.get("watches") or entry.data.get("watches")
    if not w:
        return {k: dict(v) for k, v in DEFAULT_WATCHES.items()}   # bootstrap: all three
    # options configured: a watch with an empty MAC is disabled -> no entities (watch3 auto-hides)
    return {k: dict(v) for k, v in w.items() if (v.get("mac") or "").strip()}


def _nodes_from_entry(entry: ConfigEntry) -> dict[str, str]:
    n = entry.options.get("nodes") or entry.data.get("nodes")
    return dict(n) if n else dict(NODE_ROOMS)


def _gw(entry: ConfigEntry, key: str, default):
    return (entry.options.get("gridwatts") or {}).get(key, default)


# --------------------------------------------------------------------------- service helpers
def _state(hass: HomeAssistant, entity_id: str):
    st = hass.states.get(entity_id)
    if st is None or st.state in ("unknown", "unavailable", ""):
        return None
    return st.state


def _resolve_watch(hass: HomeAssistant, call: ServiceCall, hub: Gtx2Hub) -> str | None:
    """Explicit watch, else the current select.gtx2_push_target (the Push-view buttons rely on this)."""
    w = call.data.get("watch")
    if not w:
        w = _state(hass, "select.gtx2_push_target")
    return w if w in hub.watches else None


def _parse_dial_id(label: str | None) -> int:
    """'Weather (25023)' -> 25023 (0 if unparseable)."""
    if label and "(" in label and ")" in label:
        try:
            return int(label.split("(", 1)[1].split(")", 1)[0])
        except (ValueError, IndexError):
            return 0
    return 0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hub = Gtx2Hub(hass, _watches_from_entry(entry), _nodes_from_entry(entry))
    await hub.async_start()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub
    hub._mqtt_unsubs = []

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _register_services(hass, entry, hub)
    await _subscribe_mqtt(hass, hub)

    watches = list(hub.watches)
    grid_target = _gw(entry, "target", "daily" if "daily" in watches else (watches[0] if watches else "daily"))
    # Live-kW is MULTI-TARGET (watch3 durably mirrors the daily). Build the protected set from the
    # configured gridkw_target (str or comma/space list), keep only real watch slugs, and always add
    # watch3 when it's configured — so the /30 sync never clobbers watch3's day=kW ("16" flicker).
    _gkw_opt = _gw(entry, "gridkw_target", grid_target)
    _gkw_raw = (_gkw_opt if isinstance(_gkw_opt, (list, set, tuple))
                else str(_gkw_opt).replace(",", " ").split())
    gridkw_targets = {t for t in _gkw_raw if t in watches}
    if "watch3" in watches:
        gridkw_targets.add("watch3")
    if not gridkw_targets:
        gridkw_targets = {grid_target}
    hub._scheduler = Gtx2Scheduler(
        hass, hub, weather_entity=WEATHER_ENTITY,
        grid_source=_gw(entry, "source_entity", GRIDWATTS_SOURCE_DEFAULT),
        grid_target=grid_target,
        grid_max_w=int(_gw(entry, "max_w", DEFAULT_MAX_W)),
        gridkw_gate=_gw(entry, "gridkw_gate", GRIDKW_GATE_DEFAULT),
        gridkw_targets=gridkw_targets)
    await hub._scheduler.async_start()

    hub._media = MediaController(hass, hub, entry)
    await hub._media.async_start()
    hub.screen_break = ScreenBreakController(
        hass, hub, www_url_base=_gw(entry, "www_url_base", WWW_URL_BASE_DEFAULT))
    _register_media_services(hass, hub)

    await async_register_frontend(hass)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change (re-reads watches/nodes/gridwatts/media)."""
    await hass.config_entries.async_reload(entry.entry_id)


def _register_media_services(hass: HomeAssistant, hub: Gtx2Hub) -> None:
    """The 4 no-arg media dispatch services the dashboard media/find rows bind."""
    empty = vol.Schema({})

    async def _play_pause(_call: ServiceCall) -> None:
        await hub._media.play_pause()

    async def _next(_call: ServiceCall) -> None:
        await hub._media.next_track()

    async def _prev(_call: ServiceCall) -> None:
        await hub._media.prev_track()

    async def _find(_call: ServiceCall) -> None:
        await hub._media.find_my_phone()

    reg = hass.services.async_register
    reg(DOMAIN, "media_play_pause", _play_pause, schema=empty)
    reg(DOMAIN, "media_next", _next, schema=empty)
    reg(DOMAIN, "media_prev", _prev, schema=empty)
    reg(DOMAIN, "find_my_phone", _find, schema=empty)


def _register_services(hass: HomeAssistant, entry: ConfigEntry, hub: Gtx2Hub) -> None:
    watches = hub.watches
    req_watch = vol.Schema({vol.Required("watch"): vol.In(watches)}, extra=vol.ALLOW_EXTRA)

    async def buzz(call: ServiceCall) -> None:
        w = call.data["watch"]
        await router.route(hass, hub, w, "buzz", node_data={},
                           fb_data={"duration": int(call.data.get("duration", 5))})

    async def push_weather(call: ServiceCall) -> None:
        w = call.data["watch"]
        frame = await router.fetch_weather_frame(hass, WEATHER_ENTITY)
        if frame is None:
            _LOGGER.warning("gtx2.push_weather %s skipped: %s unavailable — last frame kept",
                            w, WEATHER_ENTITY)
            hub.set_last_result("push_weather: skipped (weather unavailable)")
            return
        await router.route(hass, hub, w, "push_weather", node_data=frame, fb_data=frame)

    async def set_time_custom(call: ServiceCall) -> None:
        # RTC live-value write (node-only; no host-bridge fallback — only a link-holder can write it).
        await router.route(hass, hub, call.data["watch"], "set_time_custom", node_data={
            "hour": int(call.data["hour"]), "minute": int(call.data["minute"]),
            "second": int(call.data["second"]), "day": int(call.data["day"])})

    async def set_alarm(call: ServiceCall) -> None:
        w = _resolve_watch(hass, call, hub)
        if w is None:
            hub.set_last_result("set_alarm: no target watch")
            return
        index = call.data.get("index")
        if index is None:
            idx_s = _state(hass, "number.gtx2_alarm_index")
            index = int(float(idx_s)) if idx_s is not None else 0
        hour = call.data.get("hour")
        minute = call.data.get("minute")
        if hour is None or minute is None:
            t = _state(hass, "time.gtx2_alarm_time") or "07:00:00"
            parts = t.split(":")
            hour = int(parts[0]) if hour is None else hour
            minute = int(parts[1]) if (minute is None and len(parts) > 1) else (minute or 0)
        enabled = call.data.get("enabled")
        if enabled is None:
            enabled = _state(hass, "switch.gtx2_alarm_enabled") == "on"
        await router.route(hass, hub, w, "set_alarm",
                           node_data={"index": int(index), "hour": int(hour),
                                      "minute": int(minute), "enabled": bool(enabled)})

    async def switch_dial(call: ServiceCall) -> None:
        w = _resolve_watch(hass, call, hub)
        if w is None:
            hub.set_last_result("switch_dial: no target watch")
            return
        dial_id = call.data.get("dial_id")
        if dial_id is None:
            dial_id = _parse_dial_id(_state(hass, "select.gtx2_dial"))
        if not dial_id or int(dial_id) <= 0:
            hub.set_last_result("switch_dial: no dial id")
            return
        await router.route(hass, hub, w, "switch_dial", node_data={"dial_id": int(dial_id)})

    async def push_text(call: ServiceCall) -> None:
        w = _resolve_watch(hass, call, hub)
        if w is None:
            hub.set_last_result("push_text: no target watch")
            return
        text = call.data.get("text")
        if not text:
            text = _state(hass, "text.gtx2_push_text")
        if not text or text in ("unknown", "unavailable"):
            hub.set_last_result("push_text: empty text")
            return
        from urllib.parse import quote
        base = _gw(entry, "blobd_base", BLOBD_BASE_DEFAULT)
        url = f"{base}/face.bin?title={quote(text)}"
        # GATED (blobd render-on-fetch): node-only url-based push_face, dial 25040.
        await router.route(hass, hub, w, "push_face",
                           node_data={"dial_id": PUSH_TEXT_DIAL_ID, "url": url}, allow_fallback=False)

    async def push_text_label(call: ServiceCall) -> None:
        w = _resolve_watch(hass, call, hub)
        if w is None:
            hub.set_last_result("push_text_label: no target watch")
            return
        text = call.data.get("text")
        if not text:
            text = _state(hass, "text.gtx2_push_text")
        if not text or text in ("unknown", "unavailable"):
            hub.set_last_result("push_text_label: empty text")
            return
        cur_s = None
        st = hass.states.get(WEATHER_ENTITY)
        if st is not None:
            cur_s = st.attributes.get("temperature")
        t_cur = round(float(cur_s)) if cur_s is not None else 0
        # #28(a) glanceable: ride the weather city line; condition fixed to clear (text channel).
        frame = {"temp_current": t_cur, "temp_max": t_cur, "temp_min": t_cur,
                 "condition": 8, "city": str(text)[:32]}
        await router.route(hass, hub, w, "push_weather", node_data=frame, allow_fallback=False)

    async def push_notification(call: ServiceCall) -> None:
        w = call.data["watch"]
        title = call.data.get("title") or _state(hass, "text.gtx2_notify_title") or "Notification"
        body = call.data.get("body")
        if body is None:
            body = _state(hass, "text.gtx2_notify_body") or ""
        footer = call.data.get("footer") or dt_util.now().strftime("%H:%M")
        mac = hub.watches[w].get("mac", "")
        payload = logic.fallback_payload("push_notification", mac,
                                         {"title": title, "body": body, "footer": footer})
        if payload and mac:
            await mqtt.async_publish(hass, MQTT_CMD_TOPIC, json.dumps(payload))
            hub.set_last_result("push_notification: sent via host-bridge")
        else:
            _LOGGER.warning("gtx2.push_notification %s: no mac configured — cannot render on the host", w)
            hub.set_last_result("push_notification: no route (no mac)")

    async def push_face(call: ServiceCall) -> None:
        w = call.data["watch"]
        from . import facepush  # chunked gauge delivery (slice 2)
        await facepush.push_face(
            hass, hub, w, int(call.data["watts"]),
            int(call.data.get("dial_id", DEFAULT_DIAL_ID)),
            int(call.data.get("max_w", DEFAULT_MAX_W)))

    # ---- register ----
    # Node-only / simple-routed actions share one handler factory (holder-first, fallback via table).
    SIMPLE = ("stop_buzz", "sync_time", "read_health", "read_state", "release_link", "activate")
    reg = hass.services.async_register
    reg(DOMAIN, "buzz", buzz,
        schema=req_watch.extend({vol.Optional("duration", default=5): vol.Coerce(int)}))
    for action in SIMPLE:
        reg(DOMAIN, action, _make_simple(hass, hub, action), schema=req_watch)
    reg(DOMAIN, "push_weather", push_weather, schema=req_watch)
    reg(DOMAIN, "set_time_custom", set_time_custom, schema=req_watch.extend({
        vol.Required("hour"): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
        vol.Required("minute"): vol.All(vol.Coerce(int), vol.Range(min=0, max=59)),
        vol.Required("second"): vol.All(vol.Coerce(int), vol.Range(min=0, max=59)),
        vol.Required("day"): vol.All(vol.Coerce(int), vol.Range(min=1, max=31))}))
    reg(DOMAIN, "set_alarm", set_alarm, schema=vol.Schema({
        vol.Optional("watch"): vol.In(watches), vol.Optional("index"): vol.Coerce(int),
        vol.Optional("hour"): vol.Coerce(int), vol.Optional("minute"): vol.Coerce(int),
        vol.Optional("enabled"): cv.boolean}))
    reg(DOMAIN, "switch_dial", switch_dial, schema=vol.Schema({
        vol.Optional("watch"): vol.In(watches), vol.Optional("dial_id"): vol.Coerce(int)}))
    reg(DOMAIN, "push_text", push_text, schema=vol.Schema({
        vol.Optional("watch"): vol.In(watches), vol.Optional("text"): cv.string}))
    reg(DOMAIN, "push_text_label", push_text_label, schema=vol.Schema({
        vol.Optional("watch"): vol.In(watches), vol.Optional("text"): cv.string}))
    reg(DOMAIN, "push_notification", push_notification, schema=req_watch.extend({
        vol.Optional("title"): cv.string, vol.Optional("body"): cv.string,
        vol.Optional("footer"): cv.string}))
    reg(DOMAIN, "push_face", push_face, schema=req_watch.extend({
        vol.Required("watts"): vol.Coerce(int),
        vol.Optional("dial_id", default=DEFAULT_DIAL_ID): vol.Coerce(int),
        vol.Optional("max_w", default=DEFAULT_MAX_W): vol.Coerce(int)}))


def _make_simple(hass: HomeAssistant, hub: Gtx2Hub, action: str):
    async def _h(call: ServiceCall) -> None:
        await router.route(hass, hub, call.data["watch"], action, node_data={})
    return _h


async def _subscribe_mqtt(hass: HomeAssistant, hub: Gtx2Hub) -> None:
    """Port of gtx2_notify.yaml's MQTT status entities into hub state."""

    @callback
    def _availability(msg) -> None:
        hub.bridge_online = str(msg.payload).strip().lower() == "online"
        hub._notify()

    @callback
    def _registry(msg) -> None:
        try:
            hub.detected_watches = int(json.loads(msg.payload).get("count", 0))
        except (ValueError, TypeError, AttributeError):
            hub.detected_watches = 0
        hub._notify()

    @callback
    def _result(msg) -> None:
        try:
            data = json.loads(msg.payload)
            cmd = data.get("command", "?")
            if data.get("ok"):
                hub.set_last_result(f"{cmd}: ok")
            else:
                hub.set_last_result(f"{cmd}: ERR {data.get('error', '')}")
        except (ValueError, TypeError):
            _LOGGER.debug("gtx2 result topic: non-JSON payload %r", msg.payload)

    hub._mqtt_unsubs.append(await mqtt.async_subscribe(hass, MQTT_AVAILABILITY_TOPIC, _availability))
    hub._mqtt_unsubs.append(await mqtt.async_subscribe(hass, MQTT_REGISTRY_TOPIC, _registry))
    hub._mqtt_unsubs.append(await mqtt.async_subscribe(hass, MQTT_RESULT_TOPIC, _result))


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        return False
    hub: Gtx2Hub = hass.data[DOMAIN].pop(entry.entry_id)
    sched = getattr(hub, "_scheduler", None)
    if sched is not None:
        await sched.async_stop()
    media = getattr(hub, "_media", None)
    if media is not None:
        await media.async_stop()
    sb = getattr(hub, "screen_break", None)
    if sb is not None:
        await sb.async_stop()
    for unsub in getattr(hub, "_mqtt_unsubs", []):
        unsub()
    await hub.async_stop()
    # Remove services only when the last entry unloads (single-instance today, but be safe).
    if not hass.data[DOMAIN]:
        from .const import ALL_SERVICES
        for name in ALL_SERVICES:
            if hass.services.has_service(DOMAIN, name):
                hass.services.async_remove(DOMAIN, name)
    return True
