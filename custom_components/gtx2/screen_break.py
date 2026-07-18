"""Screen-break — port of gtx2_screen_break.yaml.

Per-watch 5-min on-wrist countdown: opening chime -> guaranteed-end native alarm (slot 2, T+5) ->
"SCREEN BREAK" announce -> 5 countdown faces (60 s each) -> double chime -> restore weather face +
reset the switch. Faces are pre-rendered dials staged on HA /local/ (25030-25035), pushed via the
node url-based push_face. Each watch runs independently as its own asyncio.Task.

Cancel = switch OFF mid-break: the task is cancelled and we restore the weather face + disable the
alarm. The natural-end path resets the switch itself; the `_natural_end` guard makes the resulting
switch-off skip the manual-cancel restore (replaces the YAML's context.parent_id guard).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import router

_LOGGER = logging.getLogger(__name__)

ANNOUNCE = (25030, "gtx2_break_25030.bin")
COUNTDOWN = [(25031, "gtx2_break_25031.bin"), (25032, "gtx2_break_25032.bin"),
             (25033, "gtx2_break_25033.bin"), (25034, "gtx2_break_25034.bin"),
             (25035, "gtx2_break_25035.bin")]
RESTORE_DIAL = 25023
RESTORE_FILE = "gtx2_weather.bin"
ALARM_SLOT = 2
ANNOUNCE_HOLD_S = 15
COUNTDOWN_HOLD_S = 60


class ScreenBreakController:
    def __init__(self, hass: HomeAssistant, hub, *, www_url_base: str) -> None:
        self.hass = hass
        self.hub = hub
        self.www_url_base = www_url_base
        self._tasks: dict[str, asyncio.Task] = {}
        self._natural_end: set[str] = set()

    # --------------------------------------------------------------- switch entry points
    async def async_start(self, watch: str) -> None:
        old = self._tasks.pop(watch, None)
        if old is not None and not old.done():
            old.cancel()
        self._natural_end.discard(watch)
        self._tasks[watch] = self.hass.async_create_task(self._run(watch))

    async def async_cancel(self, watch: str) -> None:
        if watch in self._natural_end:
            self._natural_end.discard(watch)   # runner's own reset — not a manual cancel
            return
        task = self._tasks.pop(watch, None)
        if task is not None and not task.done():
            task.cancel()
        await self._restore(watch)
        await self._disable_alarm(watch)

    async def async_stop(self) -> None:
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()
        self._natural_end.clear()

    # --------------------------------------------------------------- sequences
    async def _run(self, watch: str) -> None:
        try:
            await self._chime(watch)
            end = dt_util.now() + timedelta(minutes=5)
            await router.route(self.hass, self.hub, watch, "set_alarm",
                               node_data={"index": ALARM_SLOT, "hour": end.hour,
                                          "minute": end.minute, "enabled": True})
            await self._push(watch, *ANNOUNCE)
            await asyncio.sleep(ANNOUNCE_HOLD_S)
            for dial, fname in COUNTDOWN:
                await self._push(watch, dial, fname)
                await asyncio.sleep(COUNTDOWN_HOLD_S)
            # natural end: double chime, restore, self-reset (guard the resulting switch-off)
            await self._chime(watch)
            await self._chime(watch)
            self._natural_end.add(watch)
            await self._restore(watch)
            await self.hass.services.async_call(
                "switch", "turn_off", {"entity_id": f"switch.gtx2_{watch}_screen_break"},
                blocking=False)
        except asyncio.CancelledError:
            raise                                  # manual cancel handled by async_cancel
        finally:
            self._tasks.pop(watch, None)

    async def _chime(self, watch: str) -> None:
        await router.route(self.hass, self.hub, watch, "buzz", node_data={}, fb_data={"duration": 5})
        await asyncio.sleep(2)
        await router.route(self.hass, self.hub, watch, "stop_buzz", node_data={})
        await asyncio.sleep(0.4)

    async def _push(self, watch: str, dial: int, fname: str) -> None:
        url = f"{self.www_url_base}/local/{fname}"
        await router.route(self.hass, self.hub, watch, "push_face",
                           node_data={"dial_id": dial, "url": url})

    async def _restore(self, watch: str) -> None:
        url = f"{self.www_url_base}/local/{RESTORE_FILE}"
        await router.route(self.hass, self.hub, watch, "push_face",
                           node_data={"dial_id": RESTORE_DIAL, "url": url})

    async def _disable_alarm(self, watch: str) -> None:
        await router.route(self.hass, self.hub, watch, "set_alarm",
                           node_data={"index": ALARM_SLOT, "hour": 0, "minute": 0, "enabled": False})
