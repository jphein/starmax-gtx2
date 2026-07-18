"""Settings / reminders command surface for the Starmax GTX2 (STANDALONE client, Track B / module B2).

Covers the watch's device-settings and reminder domain: user profile, sport goals, device
state (time/unit/temp format, language, backlight, brightness, raise-to-wake), alarms,
do-not-disturb / quiet hours, sedentary (long-sit) reminder, drink-water reminder, event
reminders, world-clock, always-on-display, and date format.

Provenance / clean-room
-----------------------
STANDALONE lane: builders/parsers are derived from the vendor APK protobuf schema (package
``com.starmax.bluetoothsdk``, outer message ``Notify``) and the vendor SDK opcode map — internal
reverse-engineering notes NOT shipped in this repo — and, where a real frame exists, the
capture-verified ``docs/protocol-spec.md``. It is NOT clean-room and must never inform the
Gadgetbridge PR.

Each builder's docstring tags its confidence:
  * ``[CAP]``    — byte-shape confirmed against a real capture frame (cites protocol-spec §).
  * ``[SCHEMA]`` — payload built faithfully from the APK schema message, but the **wire opcode is
                   UNRESOLVED** on the wire (the feature never appeared in any capture, and
                   per protocol-spec §1.4 the SDK's high-level opcodes are NOT the wire opcodes).
                   These frames are experimental: prefer ``--dry-run`` until validated on hardware.

Reuses the shared core (``framing`` + ``protobuf``) and the capture-verified alarm builders from
``base``; it reimplements neither.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from starmax_client import framing
from starmax_client.protobuf import ProtobufWriter, encode_varint, parse as pb_parse, to_dict
# Reuse the capture-verified alarm surface (wire opcode 0x07) instead of reimplementing it.
from starmax_client.commands.base import Alarm, build_alarm_get, build_alarm_set, OP_ALARM

GROUP = "settings"

# --------------------------------------------------------------------------- wire opcodes
# CAPTURE-VERIFIED wire opcodes (docs/protocol-spec.md §2). These frames are real.
OP_SETTINGS_BUNDLE = 0x03   # [CAP] user profile + step/distance goals + notif toggles (§3.9)
OP_FEATURE_BITMAP = 0x04    # [CAP] boolean feature/notification bitmap (§2)
OP_SETTING_KV = 0x22        # [CAP] generic setting key/value query (§3.8)
# (alarms ride 0x07 — imported from base as OP_ALARM.)

# SDK-level opcodes (from the vendor SDK opcode map). NB per protocol-spec §1.4 these are the SDK's
# REV_TYPE values and are NOT guaranteed to be the wire opcode — used here as the best available
# carrier for [SCHEMA] commands that never appeared in a capture. Verify on-device before trusting.
#
# DECOMPILE VERDICT (internal opcode-resolution RE, §3): these REV_TYPE values are PROVEN wrong-family.
# They come from the Java com.starmax.bluetoothsdk SDK, which frames with 0xDA + CRC-16/ARC and is
# a DIFFERENT Starmax watch — NOT the GTX2 (0xC1 + CRC-CCITT-FALSE). The real GTX2 opcodes are in
# the Dart creek_blue_manage plugin (Dart-AOT-only, not extractable without blutter); DND /
# sedentary / drink were additionally never seen on the C1 channel (protocol-spec §10). So these
# are UNRESOLVED, not merely unverified — keep every command below --force-gated.
SDK_OP_DEVICE_STATE = 0x82  # SET_Device_State: time/unit/temp format, language, backlight, brightness, raise-wrist
SDK_OP_USER_INFO = 0x89     # SET_User_Info (on the wire the profile is carried inside the 0x03 bundle)
SDK_OP_SPORT_GOAL = 0x8A    # SET_Goal_Sport: step / calories / distance
SDK_OP_DND = 0xB4           # SET_Remind_NoDisturb
SDK_OP_ALARM = 0xB5         # SET_Remind_AlarmClock (wire = 0x07, see base)
SDK_OP_SEDENTARY = 0xB6     # SET_Remind_Sedentary
SDK_OP_DRINK = 0xB7         # SET_Remind_DrinkWater
SDK_OP_EVENT = 0xBB         # SET_Alarm_Even (multi-packet / unicode content)

# --------------------------------------------------------------------------- enums (SDK)
# Time / unit / temperature formats (STlTimeMode / STUnit / STlTemperatureUnit).
TIME_12H, TIME_24H = 0, 1
UNIT_METRIC, UNIT_IMPERIAL = 0, 1
TEMP_CELSIUS, TEMP_FAHRENHEIT = 0, 1
# Alarm reminder types (STAlarmClockType, from the vendor SDK opcode map).
ALARM_TYPE_DEFAULT, ALARM_TYPE_DRINK, ALARM_TYPE_MEDICINE, ALARM_TYPE_EAT = 0, 1, 2, 3
ALARM_TYPE_SPORT, ALARM_TYPE_SLEEP, ALARM_TYPE_WAKE, ALARM_TYPE_DATE = 4, 5, 6, 7
ALARM_TYPE_PARTY, ALARM_TYPE_MEETING = 8, 9


def _packed_body(values: Sequence[int]) -> bytes:
    """Body of a protobuf ``repeated packed`` field: concatenated varints (no tag/len).

    Wrap with ``ProtobufWriter().bytes(field, _packed_body(...))`` to emit the field.
    """
    return b"".join(encode_varint(int(v)) for v in values)


def _window_msg(on: bool, start_h: int, start_m: int, end_h: int, end_m: int,
                *, on_field: int = 2, interval: Optional[int] = None,
                interval_field: int = 7) -> ProtobufWriter:
    """Shared reminder shape: onOff + start/end window (+ optional interval).

    Matches ``Notify.LongSit`` / ``Notify.DrinkWater`` (status f1 elided on set; onOff f2;
    startHour f3; startMinute f4; endHour f5; endMinute f6; interval f7).
    """
    w = (ProtobufWriter()
         .bool(on_field, on)
         .varint(on_field + 1, start_h)
         .varint(on_field + 2, start_m)
         .varint(on_field + 3, end_h)
         .varint(on_field + 4, end_m))
    if interval is not None:
        w.varint(interval_field, interval)
    return w


# =========================================================================== user profile
@dataclass
class UserProfile:
    """User biometrics (``Notify.UserInfo`` / protocol-spec §3.9 profile block).

    ``weight_kg`` is transmitted in 0.1 kg units (SDK ``STlUserInfo.weight``); ``sex`` is
    1=male / 0=female (§3.9 f5).
    """
    height_cm: int
    weight_kg: float
    birth_year: int
    sex: int = 1                       # 1=male, 0=female (§3.9 f5)
    step_goal: int = 8000             # steps/day (§3.9 goals f4)
    distance_goal_m: int = 5000       # metres (§3.9 goals f5)
    notif_switches: int = 12          # count of per-app notif toggles to emit (all enabled)


def build_feature_bitmap(*, seq: int = 0) -> bytes:
    """[CAP] Notification/feature enable bitmap — wire ``0x04``. Payload ``08 01 10 02``
    (``{f1:1, f2:2}``).

    The vendor app sends this on connect, before any ``0x11`` notification; the watch replies
    with its full capability bitmap (``0801 1002 1801 …``). It is the notification-enable the
    standalone ``notify`` path was missing (internal RE notes);
    paired with the ``0x03`` toggles bundle it makes a pushed notification display. NOTE: display
    ALSO requires the watch to see the client as a connected companion — on the vendor app that
    is a classic-BT/HFP link (notif-companion-verdict.md), which an LE-only host cannot provide.
    """
    payload = ProtobufWriter().varint(1, 1).varint(2, 2).to_bytes()
    return framing.build_command(OP_FEATURE_BITMAP, payload, flag=0, seq=seq)


def build_user_profile(p: UserProfile, seq: int = 0) -> bytes:
    """[CAP] Set user profile + goals + notification toggles — wire ``0x03`` (protocol-spec §3.9).

    Reproduces the captured bundle shape: ``f1=2``, ``f2={profile}``, ``f3={notif toggles}``,
    ``f4={goals}``. The profile carries height/weight/birth-year/sex; the goals carry the
    step + distance targets. The exact bio values here are the caller's (no PII baked in).
    """
    profile = (ProtobufWriter()
               .varint(1, p.height_cm)                       # §3.9 profile f1
               .varint(2, int(round(p.weight_kg * 10)))      # weight, 0.1 kg units (§3.9 f2)
               .varint(3, 0)                                 # observed constant (§3.9 f3)
               .varint(4, p.birth_year)                      # §3.9 f4
               .varint(5, p.sex)                             # 1=male/0=female (§3.9 f5)
               .varint(6, 1))                                # observed constant (§3.9 f6)
    toggles = ProtobufWriter()
    for i in range(1, p.notif_switches + 1):
        toggles.bool(i, True)                                # ~12 per-app switches, all on (§3.9 f3)
    goals = (ProtobufWriter()
             .varint(1, 30).varint(2, 12).varint(3, 500)     # observed constants (§3.9 goals)
             .varint(4, p.step_goal)                         # step goal (§3.9 goals f4)
             .varint(5, p.distance_goal_m)                   # distance goal, m (§3.9 goals f5)
             .varint(6, 7).varint(7, 1).varint(8, 0))        # observed constants (§3.9 goals)
    payload = (ProtobufWriter()
               .varint(1, 2)
               .message(2, profile)
               .message(3, toggles)
               .message(4, goals)
               .to_bytes())
    return framing.build_command(OP_SETTINGS_BUNDLE, payload, flag=0, seq=seq)


def parse_user_profile(payload: bytes) -> dict:
    """Parse a ``0x03`` reply into ``{height_cm, weight_kg, birth_year, sex, step_goal, distance_goal_m}``."""
    top = to_dict(payload)
    out: dict = {}
    if isinstance(top.get(2), (bytes, bytearray)):
        pr = to_dict(top[2])
        out.update(height_cm=pr.get(1), weight_kg=(pr.get(2) or 0) / 10,
                   birth_year=pr.get(4), sex=pr.get(5))
    if isinstance(top.get(4), (bytes, bytearray)):
        go = to_dict(top[4])
        out.update(step_goal=go.get(4), distance_goal_m=go.get(5))
    return out


# =========================================================================== sport goals
def build_sport_goals(steps: int, calories_kcal: int, distance_km: float, seq: int = 0) -> bytes:
    """[SCHEMA] Set daily sport goals — ``Notify.Goals`` {steps, heat, distance}, SDK ``0x8A``.

    On the wire the goals are ALSO carried inside the 0x03 profile bundle (see
    :func:`build_user_profile`); this standalone form mirrors the SDK ``SET_Goal_Sport``.
    Wire opcode UNRESOLVED — experimental, prefer ``--dry-run``.
    """
    payload = (ProtobufWriter()
               .varint(2, steps)                              # Notify.Goals f2 steps
               .varint(3, calories_kcal)                      # f3 heat (kcal)
               .varint(4, int(round(distance_km * 1000)))     # f4 distance (m)
               .to_bytes())
    return framing.build_command(SDK_OP_SPORT_GOAL, payload, flag=0, seq=seq)


# =========================================================================== device state
def build_device_state(*, time_format: Optional[int] = None, unit_format: Optional[int] = None,
                       temp_format: Optional[int] = None, language: Optional[int] = None,
                       backlight_seconds: Optional[int] = None, screen: Optional[int] = None,
                       wrist_raise: Optional[bool] = None, seq: int = 0) -> bytes:
    """[SCHEMA] Set device state — ``Notify.State`` (SDK ``STDeviceState`` via ``0x82``).

    Covers units/language/time+temp format, backlight duration, screen and raise-to-wake.
    Fields left ``None`` are omitted. Wire opcode UNRESOLVED — experimental, prefer ``--dry-run``.

    Schema (``Notify.State``): timeFormat f2, unitFormat f3, tempFormat f4, language f5,
    backlighting f6, screen f7, wristUp f8.
    """
    w = ProtobufWriter()
    if time_format is not None:
        w.varint(2, time_format)
    if unit_format is not None:
        w.varint(3, unit_format)
    if temp_format is not None:
        w.varint(4, temp_format)
    if language is not None:
        w.varint(5, language)
    if backlight_seconds is not None:
        w.varint(6, backlight_seconds)
    if screen is not None:
        w.varint(7, screen)
    if wrist_raise is not None:
        w.bool(8, wrist_raise)
    return framing.build_command(SDK_OP_DEVICE_STATE, w.to_bytes(), flag=0, seq=seq)


def build_wrist_raise(on: bool, seq: int = 0) -> bytes:
    """[SCHEMA] Toggle raise-to-wake only (``Notify.State.wristUp`` f8, via device-state ``0x82``)."""
    return build_device_state(wrist_raise=on, seq=seq)


def parse_device_state(payload: bytes) -> dict:
    """Parse a ``Notify.State`` reply into named fields."""
    d = to_dict(payload)
    return {"time_format": d.get(2), "unit_format": d.get(3), "temp_format": d.get(4),
            "language": d.get(5), "backlighting": d.get(6), "screen": d.get(7),
            "wrist_raise": bool(d.get(8, 0))}


# =========================================================================== generic setting (0x22)
def build_setting_query(key: int = 1, seq: int = 0) -> bytes:
    """[CAP] Read a generic setting value — wire ``0x22`` (protocol-spec §3.8).

    Request ``f1=key``; the watch replies ``f1=key, f2=<value>`` (a plaintext protobuf frame
    with a CRC trailer). Captured example: ``f1=1`` -> ``f2=244``.
    """
    payload = ProtobufWriter().varint(1, key).to_bytes()
    return framing.build_command(OP_SETTING_KV, payload, flag=0, seq=seq)


def parse_setting_reply(payload: bytes) -> dict:
    """Parse a ``0x22`` reply ``{key, value}`` (protocol-spec §3.8)."""
    d = to_dict(payload)
    return {"key": d.get(1), "value": d.get(2)}


# =========================================================================== do-not-disturb
def build_dnd(on: bool, start_h: int = 22, start_m: int = 0, end_h: int = 7, end_m: int = 0,
              *, all_day: bool = False, seq: int = 0) -> bytes:
    """[SCHEMA] Do-not-disturb / quiet hours — ``Notify.NotDisturb`` (SDK ``0xB4``).

    Schema: allDayOnOff f2, onOff f3, startHour f4, startMinute f5, endHour f6, endMinute f7.
    Wire opcode UNRESOLVED — experimental, prefer ``--dry-run``.
    """
    payload = (ProtobufWriter()
               .bool(2, all_day)                    # allDayOnOff
               .bool(3, on)                         # onOff
               .varint(4, start_h).varint(5, start_m)
               .varint(6, end_h).varint(7, end_m)
               .to_bytes())
    return framing.build_command(SDK_OP_DND, payload, flag=0, seq=seq)


def parse_dnd(payload: bytes) -> dict:
    d = to_dict(payload)
    return {"all_day": bool(d.get(2, 0)), "on": bool(d.get(3, 0)),
            "start": (d.get(4), d.get(5)), "end": (d.get(6), d.get(7))}


# =========================================================================== sedentary (long-sit)
def build_sedentary(on: bool, start_h: int = 9, start_m: int = 0, end_h: int = 18, end_m: int = 0,
                    interval_min: int = 60, seq: int = 0) -> bytes:
    """[SCHEMA] Sedentary / long-sit reminder — ``Notify.LongSit`` (SDK ``0xB6``, ``STAlarmInterval``).

    Schema: onOff f2, startHour f3, startMinute f4, endHour f5, endMinute f6, interval f7 (min).
    Wire opcode UNRESOLVED — experimental, prefer ``--dry-run``.
    """
    payload = _window_msg(on, start_h, start_m, end_h, end_m, interval=interval_min).to_bytes()
    return framing.build_command(SDK_OP_SEDENTARY, payload, flag=0, seq=seq)


# =========================================================================== drink-water
def build_drink_water(on: bool, start_h: int = 9, start_m: int = 0, end_h: int = 18, end_m: int = 0,
                      interval_min: int = 120, seq: int = 0) -> bytes:
    """[SCHEMA] Drink-water reminder — ``Notify.DrinkWater`` (SDK ``0xB7``, ``STAlarmInterval``).

    Same shape as sedentary (onOff f2 / window f3-f6 / interval f7). Wire opcode UNRESOLVED.
    """
    payload = _window_msg(on, start_h, start_m, end_h, end_m, interval=interval_min).to_bytes()
    return framing.build_command(SDK_OP_DRINK, payload, flag=0, seq=seq)


def parse_interval_reminder(payload: bytes) -> dict:
    """Parse a LongSit/DrinkWater reply {on, start, end, interval}."""
    d = to_dict(payload)
    return {"on": bool(d.get(2, 0)), "start": (d.get(3), d.get(4)),
            "end": (d.get(5), d.get(6)), "interval_min": d.get(7)}


# =========================================================================== event reminders
@dataclass
class EventReminder:
    """One calendar/event reminder (``Notify.EventReminder``)."""
    year: int
    month: int
    day: int
    hour: int
    minute: int
    content: str
    repeats: Sequence[int] = ()       # packed uint64 weekly repeat mask(s)
    remind_type: int = 0
    repeat_type: int = 0


def build_event_reminders(events: Sequence[EventReminder], seq: int = 0) -> bytes:
    """[SCHEMA] Set event reminders — ``Notify.EventReminderData`` {status, repeated EventReminder}
    (SDK ``0xBB``, multi-packet / unicode content).

    Schema per event: year f1, month f2, day f3, hour f4, minute f5, content f6 (UTF-8),
    repeats f7 (packed uint64), remindType f8, repeatType f9, otherInfo f10.
    Content may exceed one PDU; the frame fragments via :func:`framing.frame_to_pdus`.
    Wire opcode UNRESOLVED — experimental, prefer ``--dry-run``.
    """
    top = ProtobufWriter()
    for ev in events:
        em = (ProtobufWriter()
              .varint(1, ev.year).varint(2, ev.month).varint(3, ev.day)
              .varint(4, ev.hour).varint(5, ev.minute)
              .string(6, ev.content))
        if ev.repeats:
            em.bytes(7, _packed_body(ev.repeats))            # repeats f7 (packed uint64)
        em.varint(8, ev.remind_type).varint(9, ev.repeat_type)
        top.message(2, em)                                   # repeated EventReminder (f2)
    return framing.build_command(SDK_OP_EVENT, top.to_bytes(), flag=0, seq=seq)


def parse_event_reminders(payload: bytes) -> List[dict]:
    """Parse an ``EventReminderData`` reply into a list of event dicts."""
    out: List[dict] = []
    for f, _w, v in pb_parse(payload):
        if f == 2 and isinstance(v, (bytes, bytearray)):
            d = to_dict(v)
            out.append({"date": (d.get(1), d.get(2), d.get(3)),
                        "time": (d.get(4), d.get(5)),
                        "content": (d.get(6) or b"").decode("utf-8", "replace"),
                        "remind_type": d.get(8), "repeat_type": d.get(9)})
    return out


# =========================================================================== world clock
def build_world_clock(city_ids: Sequence[int], seq: int = 0) -> bytes:
    """[SCHEMA] Set world-clock cities — ``Notify.WorldClockData`` {status, packed uint64 citys}.

    Wire opcode genuinely UNRESOLVED (absent from the SDK opcode map and every capture); this
    frames the schema body on the generic-setting channel ``0x22`` as a best-effort carrier.
    Experimental — ``--dry-run`` only until validated.
    """
    payload = ProtobufWriter().bytes(2, _packed_body(city_ids)).to_bytes()  # citys f2 (packed uint64)
    return framing.build_command(OP_SETTING_KV, payload, flag=0, seq=seq)


# =========================================================================== always-on display
def build_aod(on: bool, style: int = 0, start_h: int = 8, start_m: int = 0,
              end_h: int = 22, end_m: int = 0, seq: int = 0) -> bytes:
    """[SCHEMA] Always-on-display schedule — ``Notify.AodData``.

    Schema: style f2, switchType f3 (on/off), startHour f4, startMinute f5, endHour f6,
    endMinute f7. Wire opcode UNRESOLVED (not in SDK map) — framed on ``0x22`` as a carrier.
    Experimental — ``--dry-run`` only.
    """
    payload = (ProtobufWriter()
               .varint(2, style)                    # AodData style f2
               .varint(3, 1 if on else 0)           # switchType f3
               .varint(4, start_h).varint(5, start_m)
               .varint(6, end_h).varint(7, end_m)
               .to_bytes())
    return framing.build_command(OP_SETTING_KV, payload, flag=0, seq=seq)


# =========================================================================== date format
def build_date_format(date_format: int, seq: int = 0) -> bytes:
    """[SCHEMA] Set date format — ``Notify.DateFormat`` {status, dateFormat f2}.

    Wire opcode UNRESOLVED (not in SDK map) — framed on the generic-setting channel ``0x22``.
    Experimental — ``--dry-run`` only.
    """
    payload = ProtobufWriter().varint(2, date_format).to_bytes()
    return framing.build_command(OP_SETTING_KV, payload, flag=0, seq=seq)


# =========================================================================== registry
# name -> builder callable. Pure (no I/O); consumed by the CLI and B5's smoke test.
COMMANDS: Dict[str, object] = {
    "feature-bitmap": build_feature_bitmap,   # 0x04 notification/feature enable [CAP]
    "user-profile": build_user_profile,
    "sport-goals": build_sport_goals,
    "device-state": build_device_state,
    "wrist-raise": build_wrist_raise,
    "setting-query": build_setting_query,
    "alarm-get": build_alarm_get,          # reused from base (wire 0x07, [CAP])
    "alarm-set": build_alarm_set,          # reused from base (wire 0x07, [CAP])
    "dnd": build_dnd,
    "sedentary": build_sedentary,
    "drink-water": build_drink_water,
    "event-reminders": build_event_reminders,
    "world-clock": build_world_clock,
    "aod": build_aod,
    "date-format": build_date_format,
}

PARSERS = {
    "user-profile": parse_user_profile,
    "device-state": parse_device_state,
    "setting-reply": parse_setting_reply,
    "dnd": parse_dnd,
    "interval-reminder": parse_interval_reminder,
    "event-reminders": parse_event_reminders,
}

# Offline smoke-test overrides (consumed by ``starmax_client.commands.invoke_builder`` / B5's gate)
# for the builders that take a dataclass or list rather than trivially-synthesizable scalars.
SMOKE_ARGS = {
    "user-profile": {"p": UserProfile(height_cm=170, weight_kg=65.0, birth_year=1990)},
    "alarm-set": {"alarms": [Alarm(index=0, hour=7, minute=0)]},
    "event-reminders": {"events": [EventReminder(2026, 1, 1, 8, 0, "x")]},
    "world-clock": {"city_ids": [1, 5]},
}


# --------------------------------------------------------------------------- CLI wiring
def _mk_handler(build, opcode: int, *, expect_reply: bool = False):
    """Wrap a per-command builder into an async CLI handler.

    On ``--dry-run`` it prints the hex frame and returns without transmitting. Otherwise it
    sends (or request/replies) via ``args._client`` if the CLI wired one. ``opcode`` is used to
    match a reply when ``expect_reply`` is set.
    """
    async def _handler(args) -> int:
        frame = build(args)
        if getattr(args, "dry_run", False) or getattr(args, "_client", None) is None:
            print(frame.hex())
            return 0
        client = args._client
        if expect_reply:
            fr = await client.request(frame, opcode, timeout=5.0)
            print(fr.payload.hex() if fr is not None else "(no reply)")
        else:
            await client.send_raw(frame)
            print("sent")
        return 0
    return _handler


def register(subparsers, client=None) -> None:
    """Add the settings/reminder subcommands to ``subparsers``. B5 auto-discovers this.

    ``client`` is a connected ``StarmaxClient``-like object (or ``None`` at registration time —
    handlers read it from ``args._client``). Every subcommand supports ``--dry-run`` (print the
    hex frame, don't transmit) and ``--seq``.
    """
    def _add(name: str, help_: str) -> argparse.ArgumentParser:
        sp = subparsers.add_parser(name, help=help_)
        sp.add_argument("--dry-run", action="store_true", help="print the hex frame, don't send")
        sp.add_argument("--seq", type=lambda s: int(s, 0), default=0, help="frame seq (default 0)")
        sp.add_argument("--force", action="store_true",
                        help="send even if the wire opcode/payload is unverified (docs/command-audit.md)")
        sp.set_defaults(_client=client)
        return sp

    sp = _add("user-profile", "set user profile + goals (wire 0x03) [CAP]")
    sp.add_argument("--height", type=int, required=True, help="height in cm")
    sp.add_argument("--weight", type=float, required=True, help="weight in kg")
    sp.add_argument("--birth-year", type=int, required=True)
    sp.add_argument("--sex", type=int, choices=(0, 1), default=1, help="1=male, 0=female")
    sp.add_argument("--step-goal", type=int, default=8000)
    sp.add_argument("--distance-goal", type=int, default=5000, help="metres")
    sp.set_defaults(func=_mk_handler(lambda a: build_user_profile(
        UserProfile(a.height, a.weight, a.birth_year, a.sex, a.step_goal, a.distance_goal),
        seq=a.seq), OP_SETTINGS_BUNDLE))

    sp = _add("feature-bitmap", "enable notifications/features bitmap (wire 0x04) [CAP]")
    sp.set_defaults(func=_mk_handler(lambda a: build_feature_bitmap(seq=a.seq), OP_FEATURE_BITMAP))

    sp = _add("sport-goals", "set daily sport goals [SCHEMA]")
    sp.add_argument("--steps", type=int, default=8000)
    sp.add_argument("--calories", type=int, default=300)
    sp.add_argument("--distance", type=float, default=5.0, help="km")
    sp.set_defaults(func=_mk_handler(
        lambda a: build_sport_goals(a.steps, a.calories, a.distance, seq=a.seq), SDK_OP_SPORT_GOAL))

    sp = _add("device-state", "set time/unit/temp format, language, backlight, wrist-raise [SCHEMA]")
    sp.add_argument("--time-format", type=int, choices=(0, 1), help="0=12h, 1=24h")
    sp.add_argument("--unit", type=int, choices=(0, 1), help="0=metric, 1=imperial")
    sp.add_argument("--temp", type=int, choices=(0, 1), help="0=C, 1=F")
    sp.add_argument("--language", type=int)
    sp.add_argument("--backlight", type=int, help="backlight seconds")
    sp.add_argument("--wrist-raise", type=int, choices=(0, 1))
    sp.set_defaults(func=_mk_handler(lambda a: build_device_state(
        time_format=a.time_format, unit_format=a.unit, temp_format=a.temp, language=a.language,
        backlight_seconds=a.backlight,
        wrist_raise=None if a.wrist_raise is None else bool(a.wrist_raise), seq=a.seq),
        SDK_OP_DEVICE_STATE))

    sp = _add("wrist-raise", "toggle raise-to-wake [SCHEMA]")
    sp.add_argument("--on", type=int, choices=(0, 1), default=1)
    sp.set_defaults(func=_mk_handler(lambda a: build_wrist_raise(bool(a.on), seq=a.seq),
                                     SDK_OP_DEVICE_STATE))

    sp = _add("setting-query", "read a generic setting value (wire 0x22) [CAP]")
    sp.add_argument("--key", type=int, default=1)
    sp.set_defaults(func=_mk_handler(lambda a: build_setting_query(a.key, seq=a.seq),
                                     OP_SETTING_KV, expect_reply=True))

    sp = _add("alarm-get", "read alarms (wire 0x07) [CAP]")
    sp.set_defaults(func=_mk_handler(lambda a: build_alarm_get(seq=a.seq), OP_ALARM,
                                     expect_reply=True))

    sp = _add("alarm-set", "set one alarm (wire 0x07) [CAP]")
    sp.add_argument("--index", type=int, default=0)
    sp.add_argument("--hour", type=int, required=True)
    sp.add_argument("--minute", type=int, required=True)
    sp.add_argument("--off", action="store_true", help="create disabled")
    sp.set_defaults(func=_mk_handler(lambda a: build_alarm_set(
        [Alarm(index=a.index, hour=a.hour, minute=a.minute, enabled=not a.off)], seq=a.seq),
        OP_ALARM))

    sp = _add("dnd", "do-not-disturb / quiet hours [SCHEMA]")
    sp.add_argument("--off", action="store_true")
    sp.add_argument("--all-day", action="store_true")
    sp.add_argument("--start", default="22:00")
    sp.add_argument("--end", default="07:00")
    sp.set_defaults(func=_mk_handler(lambda a: build_dnd(
        not a.off, *_hm(a.start), *_hm(a.end), all_day=a.all_day, seq=a.seq), SDK_OP_DND))

    sp = _add("sedentary", "sedentary / long-sit reminder [SCHEMA]")
    _add_window(sp, default_interval=60)
    sp.set_defaults(func=_mk_handler(lambda a: build_sedentary(
        not a.off, *_hm(a.start), *_hm(a.end), interval_min=a.interval, seq=a.seq), SDK_OP_SEDENTARY))

    sp = _add("drink-water", "drink-water reminder [SCHEMA]")
    _add_window(sp, default_interval=120)
    sp.set_defaults(func=_mk_handler(lambda a: build_drink_water(
        not a.off, *_hm(a.start), *_hm(a.end), interval_min=a.interval, seq=a.seq), SDK_OP_DRINK))

    sp = _add("event-reminders", "set one event reminder [SCHEMA]")
    sp.add_argument("--date", required=True, help="YYYY-MM-DD")
    sp.add_argument("--time", required=True, help="HH:MM")
    sp.add_argument("--content", required=True)
    sp.set_defaults(func=_mk_handler(lambda a: build_event_reminders(
        [EventReminder(*_ymd(a.date), *_hm(a.time), a.content)], seq=a.seq), SDK_OP_EVENT))

    sp = _add("world-clock", "set world-clock cities [SCHEMA]")
    sp.add_argument("--cities", required=True, help="comma list of city ids")
    sp.set_defaults(func=_mk_handler(lambda a: build_world_clock(
        [int(x) for x in a.cities.split(",") if x != ""], seq=a.seq), OP_SETTING_KV))

    sp = _add("aod", "always-on-display schedule [SCHEMA]")
    sp.add_argument("--off", action="store_true")
    sp.add_argument("--start", default="08:00")
    sp.add_argument("--end", default="22:00")
    sp.set_defaults(func=_mk_handler(lambda a: build_aod(
        not a.off, start_h=_hm(a.start)[0], start_m=_hm(a.start)[1],
        end_h=_hm(a.end)[0], end_m=_hm(a.end)[1], seq=a.seq), OP_SETTING_KV))

    sp = _add("date-format", "set date format [SCHEMA]")
    sp.add_argument("--format", type=int, default=0)
    sp.set_defaults(func=_mk_handler(lambda a: build_date_format(a.format, seq=a.seq), OP_SETTING_KV))


def _add_window(sp: argparse.ArgumentParser, *, default_interval: int) -> None:
    sp.add_argument("--off", action="store_true")
    sp.add_argument("--start", default="09:00")
    sp.add_argument("--end", default="18:00")
    sp.add_argument("--interval", type=int, default=default_interval, help="minutes")


def _hm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


def _ymd(s: str):
    y, mo, d = s.split("-")
    return int(y), int(mo), int(d)
