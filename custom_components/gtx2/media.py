"""Media buttons + find-phone — port of gtx2_media.yaml.

The gtx2 room node fires `esphome.gtx2_input {input,...}` on a watch LE input. music.* drive the
configured media_player; find_phone rings the phone (notify) + speaks a TTS prompt. The same
handlers back the gtx2.media_play_pause / media_next / media_prev / find_my_phone dispatch services
(the dashboard media/find rows bind those). All four targets come from the config-entry options.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

_LOGGER = logging.getLogger(__name__)
_INVALID = (None, "", "unknown", "unavailable")


class MediaController:
    def __init__(self, hass: HomeAssistant, hub, entry: ConfigEntry) -> None:
        self.hass = hass
        self.hub = hub
        self.entry = entry
        self._unsub = None

    def _opt(self, key: str):
        v = (self.entry.options.get("media") or {}).get(key)
        return v if v not in _INVALID else None

    async def async_start(self) -> None:
        self._unsub = self.hass.bus.async_listen("esphome.gtx2_input", self._on_input)

    async def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _on_input(self, event) -> None:
        inp = event.data.get("input")
        if inp == "music.play_pause":
            self.hass.async_create_task(self.play_pause())
        elif inp == "music.next":
            self.hass.async_create_task(self.next_track())
        elif inp == "music.prev":
            self.hass.async_create_task(self.prev_track())
        elif inp == "find_phone":
            self.hass.async_create_task(self.find_my_phone())

    async def _transport(self, service: str) -> None:
        tgt = self._opt("media_player")
        if not tgt:
            _LOGGER.debug("gtx2 media %s skipped: no media_player configured", service)
            return
        await self.hass.services.async_call("media_player", service, {},
                                            target={"entity_id": tgt}, blocking=False)

    async def play_pause(self) -> None:
        await self._transport("media_play_pause")

    async def next_track(self) -> None:
        await self._transport("media_next_track")

    async def prev_track(self) -> None:
        await self._transport("media_previous_track")

    async def find_my_phone(self) -> None:
        notify_svc = self._opt("findphone_notify")
        if notify_svc and "." in notify_svc:
            domain, service = notify_svc.split(".", 1)
            await self.hass.services.async_call(
                domain, service,
                {"title": "Find my phone", "message": "GTX2 watch: find-phone tapped",
                 "data": {"ttl": 0, "priority": "high", "channel": "alarm_stream"}},
                blocking=False)
        speaker = self._opt("findphone_speaker")
        tts_engine = self._opt("tts_engine")
        if speaker and tts_engine:
            await self.hass.services.async_call(
                "tts", "speak",
                {"media_player_entity_id": speaker, "message": "Find my phone. Find my phone."},
                target={"entity_id": tts_engine}, blocking=False)
