"""Catalog-driven dispatcher — turn a (command, mac, params) request into watch frames.

The single entry point is :meth:`Dispatcher.handle`. It:

  1. looks the command up in :mod:`catalog` (unknown → clean error),
  2. enforces the safety tier — **red** commands refuse to run live without ``confirm=True``;
     yellow/red carry the library's ``--force`` intent implicitly (we send raw, so the gate is
     ours to honour),
  3. builds the frame(s) by ``kind`` (headline specials get bespoke drivers; the long tail of
     group commands go through the library builders + :func:`invoke_builder`),
  4. on ``dry_run`` returns the frames as hex (no BLE); otherwise connects to the target watch,
     runs the live driver, and disconnects.

Every builder/driver is from ``starmax_client`` — this module orchestrates, it never reframes a
byte itself. ``connect`` and ``clock`` are injected so the whole thing is unit-testable offline.
"""
from __future__ import annotations

import datetime as _dt
from typing import Callable, List, Optional

from starmax_client.commands import base, files, invoke_builder
from starmax_client.commands.base import (Alarm, Weather, build_alarm_set, build_health_sync,
                                          build_set_time, build_weather)
from starmax_client.commands.settings import (UserProfile, EventReminder, build_event_reminders,
                                              build_feature_bitmap, build_setting_query,
                                              build_user_profile, parse_setting_reply)

from . import actions, catalog, faces, metrics


class DispatchError(ValueError):
    pass


def _hexes(frames: List[bytes]) -> List[str]:
    return [bytes(f).hex() for f in frames]


class Dispatcher:
    def __init__(self, config, connect: Optional[Callable] = None,
                 clock: Optional[Callable[[], _dt.datetime]] = None) -> None:
        self.config = config
        self._connect = connect or actions.connect_and_bind
        self._clock = clock or (lambda: _dt.datetime.now().astimezone())

    # ------------------------------------------------------------------ public
    async def handle(self, command: str, *, mac: Optional[str] = None, params: Optional[dict] = None,
                     dry_run: bool = False, confirm: bool = False) -> dict:
        params = dict(params or {})
        try:
            cmd = catalog.get(command)
        except KeyError as e:
            return {"ok": False, "command": command, "error": str(e)}

        base_res = {"ok": True, "command": cmd.name, "tier": cmd.tier, "dry_run": dry_run,
                    "mac": mac or (None if dry_run else self.config.mac)}

        # --- safety gate: red commands need explicit confirm to run LIVE ---
        if cmd.tier == catalog.RED and not dry_run and not confirm:
            return {**base_res, "ok": False,
                    "error": (f"'{cmd.name}' is a DANGER command (destructive/unverified). "
                              f"Refusing live send without confirm=true.")}

        try:
            if dry_run:
                frames, summary = self._plan(cmd, params)
                return {**base_res, "frames": _hexes(frames), "count": len(frames),
                        "summary": summary}
            return await self._run_live(cmd, base_res, params, confirm)
        except Exception as e:  # noqa: BLE001 - surface a clean error to MQTT/CLI
            return {**base_res, "ok": False, "error": f"{type(e).__name__}: {e}"}

    # ------------------------------------------------------------------ param coercion
    def _now(self) -> _dt.datetime:
        return self._clock()

    def _weather(self, p: dict) -> Weather:
        now = self._now()
        temp = int(p.get("temp", 20))
        hi = int(p.get("hi", temp))
        lo = int(p.get("lo", temp))
        return Weather(city=str(p.get("city", self.config.default_city)),
                       month=int(p.get("month", now.month)), day=int(p.get("day", now.day)),
                       hour=int(p.get("hour", now.hour)), minute=int(p.get("minute", now.minute)),
                       condition=int(p.get("condition", 6)), temp_current=temp,
                       temp_max=hi, temp_min=lo, pressure_hpa=float(p.get("pressure", 1013.25)))

    def _notify_blob(self, p: dict) -> bytes:
        if not p.get("title"):
            raise DispatchError("notify requires a 'title'")
        return faces.build_notification_blob(
            title=str(p["title"]), body=str(p.get("body", "")), footer=str(p.get("footer", "")),
            icon=p.get("icon"), bg=str(p.get("bg", "#000000")), fg=str(p.get("fg", "#FFFFFF")),
            accent=str(p.get("accent", "#00E5FF")), name=str(p.get("name", "NOTIFY")))

    def _dial_id(self, p: dict) -> int:
        return int(p.get("dial_id", self.config.dial_id))

    def _alarm(self, p: dict) -> Alarm:
        return Alarm(index=int(p.get("index", 0)), hour=int(p["hour"]), minute=int(p["minute"]),
                     enabled=bool(p.get("enabled", True)))

    def _profile(self, p: dict) -> UserProfile:
        return UserProfile(height_cm=int(p["height"]), weight_kg=float(p["weight"]),
                           birth_year=int(p["birth_year"]), sex=int(p.get("sex", 1)),
                           step_goal=int(p.get("step_goal", 8000)),
                           distance_goal_m=int(p.get("distance_goal", 5000)))

    def _event(self, p: dict) -> EventReminder:
        y, mo, d = (int(x) for x in str(p["date"]).split("-"))
        h, mi = (int(x) for x in str(p["time"]).split(":"))
        return EventReminder(y, mo, d, h, mi, str(p.get("content", "")))

    def _health_cats(self, p: dict) -> List[int]:
        cat = p.get("category")
        return [int(cat)] if cat is not None else list(base.SYNC_CATEGORIES)

    def _activate_frames(self) -> List[bytes]:
        from starmax_client import framing
        from starmax_client.cli import _ACTIVATE_QUERIES, _ACTIVATE_FINALIZE
        frames = [framing.build_command(op, bytes.fromhex(ph), flag=0)
                  for op, ph in _ACTIVATE_QUERIES]
        frames.append(build_set_time(self._now()))
        frames += [framing.build_command(op, bytes.fromhex(ph), flag=0)
                   for op, ph in _ACTIVATE_FINALIZE]
        return frames

    # ------------------------------------------------------------------ dry-run plans
    def _plan(self, cmd: catalog.Command, p: dict):
        k = cmd.kind
        if k == "buzz":
            return actions.plan_buzz(), f"find: start+stop buzz ({p.get('duration', 5)}s live gap)"
        if k == "set-time":
            when = self._now()
            return actions.plan_set_time(when), f"set-time -> {when.isoformat()}"
        if k == "weather":
            w = self._weather(p)
            return (actions.plan_weather(w, enable=bool(p.get("enable", True))),
                    f"weather {w.city} {w.temp_current}C (cond {w.condition})")
        if k == "notify":
            blob = self._notify_blob(p)
            did = self._dial_id(p)
            frames = actions.plan_notification_push(blob, did)
            return frames, (f"notify -> custom_id_{did}.bin, blob {len(blob)}B, "
                            f"{len(frames)} D-plane frames")
        if k == "activate":
            frames = self._activate_frames()
            return frames, f"activate handshake: {len(frames)} frames"
        if k == "sync-health":
            cats = self._health_cats(p)
            return ([build_health_sync(c) for c in cats],
                    f"sync-health read of categories {cats}")
        if k == "dial-list":
            return [files.build_dial_list_request()], "dial-list read (0x16)"
        if k == "dial-push":
            blob = self._load_dial_file(p)
            did = self._dial_id(p)
            return actions.plan_notification_push(blob, did), f"dial-push {len(blob)}B -> id {did}"
        if k == "alarm-set":
            return [build_alarm_set([self._alarm(p)])], "alarm-set (0x07)"
        if k == "user-profile":
            return [build_user_profile(self._profile(p))], "user-profile (0x03)"
        if k == "event-reminders":
            return [build_event_reminders([self._event(p)])], "event-reminder (schema)"
        if k == "flash":
            data = self._read_file(p)
            return [], (f"flash-firmware: would stream {len(data)}B — DESTRUCTIVE, brick risk "
                        f"(plan withheld; use the CLI flash-firmware --probe to inspect safely)")
        # generic single-frame send / request via the library builder
        frame = self._generic_frame(cmd, p)
        return [frame], f"{cmd.name} ({cmd.group}) single frame"

    def _generic_frame(self, cmd: catalog.Command, p: dict) -> bytes:
        if cmd.builder is None:
            raise DispatchError(f"{cmd.name}: no builder wired for kind {cmd.kind!r}")
        # invoke_builder fills required args from `override` (our params) or PII-free samples.
        return bytes(invoke_builder(cmd.builder, override=p))

    def _read_file(self, p: dict) -> bytes:
        path = p.get("file")
        if not path:
            raise DispatchError("this command needs a 'file' path on the bridge host")
        with open(path, "rb") as fh:
            return fh.read()

    def _load_dial_file(self, p: dict) -> bytes:
        from starmax_client.commands import dials
        path = p.get("file")
        if not path:
            raise DispatchError("dial-push needs a 'file' path (dial .bin/ZIP or native blob)")
        return dials.load_dial_blob(path)

    # ------------------------------------------------------------------ live drivers
    async def _run_live(self, cmd: catalog.Command, base_res: dict, p: dict, confirm: bool) -> dict:
        mac = base_res["mac"]
        if self.config.uses_placeholder_mac() and not p.get("mac") and mac == self.config.mac:
            return {**base_res, "ok": False,
                    "error": "no target MAC (set GTX2_MAC or pass mac=…; placeholder refused live)"}
        client = await self._connect(mac)
        try:
            result = await self._drive(cmd, client, p, confirm)
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        return {**base_res, "result": result}

    async def _drive(self, cmd: catalog.Command, client, p: dict, confirm: bool):
        k = cmd.kind
        if k == "buzz":
            return await actions.do_buzz(client, duration=float(p.get("duration", 5.0)))
        if k == "set-time":
            return await actions.do_set_time(client, self._now())
        if k == "weather":
            return await actions.do_weather(client, self._weather(p),
                                            enable=bool(p.get("enable", True)))
        if k == "notify":
            return await actions.do_notification(client, self._notify_blob(p),
                                                 dial_id=self._dial_id(p),
                                                 confirm=bool(p.get("confirm_push", True)))
        if k == "activate":
            for fr in self._activate_frames():
                await client.send_raw(fr)
            return {"sent_frames": len(self._activate_frames())}
        if k == "sync-health":
            state = await metrics.read_state(client)
            health = await metrics.read_health(client)
            return {**state, "health": health}
        if k == "dial-list":
            fr = await client.request(files.build_dial_list_request(), files.OP_DIAL_LIST, timeout=5.0)
            return files.parse_dial_list_reply(fr.payload) if fr is not None else {"reply": None}
        if k == "dial-push":
            return await actions.do_notification(client, self._load_dial_file(p),
                                                 dial_id=self._dial_id(p))
        if k == "alarm-set":
            await client.send_raw(build_alarm_set([self._alarm(p)], seq=client.next_seq()))
            return {"alarm": "set"}
        if k == "user-profile":
            await client.send_raw(build_user_profile(self._profile(p), seq=client.next_seq()))
            return {"profile": "set"}
        if k == "event-reminders":
            await client.send_raw(build_event_reminders([self._event(p)], seq=client.next_seq()))
            return {"event": "set"}
        if k == "flash":
            return await files.flash_firmware(client, self._read_file(p))
        if k == "request":
            frame = self._generic_frame(cmd, p)
            fr = await client.request(frame, cmd.opcode or 0, timeout=5.0)
            if fr is None:
                return {"reply": None}
            out = {"payload": fr.payload.hex()}
            if cmd.name == "setting-query":
                out.update(parse_setting_reply(fr.payload))
            return out
        # generic simple send (yellow/green single-frame commands)
        await client.send_raw(self._generic_frame(cmd, p))
        return {"sent": cmd.name}
