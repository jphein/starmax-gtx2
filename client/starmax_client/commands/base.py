"""GTX2 command builders.

Each builder returns a complete app->watch frame (via :func:`framing.build_command`).
Payload layouts are decoded from real captures (docs/protocol-spec.md); the per-field
comments cite the section. Field numbers marked "observed constant" are values that were
identical across every captured frame and whose exact semantics are unresolved -- they are
reproduced so the watch sees a byte-shape it already accepts.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from starmax_client import framing
from starmax_client.protobuf import ProtobufWriter, to_dict

# Wire opcodes (docs/protocol-spec.md §2)
OP_BIND = 0x01
OP_SET_TIME = 0x02
OP_DEVICE_STATE = 0x05
OP_ALARM = 0x07
OP_HEALTH_SYNC = 0x0E
OP_NOTIFY_DETAILED = 0x11
OP_WEATHER = 0x12
OP_NOTIFY_SUMMARY = 0x13
OP_FIND_DEVICE = 0x18

def _tz_field(when: _dt.datetime) -> int:
    """set-time f9: total UTC offset (incl. DST) in minutes, wrapped non-negative.

    Ported from the GB coordinator's `buildSetTime` (parity audit #10 G5), which is
    behaviour-verified on-device. Supersedes the earlier hardcoded ``1140`` — that was a single
    (PDT) capture constant; f9 units are UNRESOLVED (§10.7), but the watch drives its displayed
    clock from f8 (epoch) + f4-6 (local), so f9 is low-impact. ``int(.../60)`` truncates toward
    zero to mirror GB's integer ``/60000``; the ``((x%1440)+1440)%1440`` wrap matches GB exactly
    (identical result in Python and Kotlin). e.g. UTC->0, -7h(PDT)->1020, +8h->480.
    """
    minutes = int(when.utcoffset().total_seconds() / 60)
    return ((minutes % 1440) + 1440) % 1440


# --------------------------------------------------------------------------- 0x01 bind
def build_bind(seq: int = 0) -> bytes:
    """Bind / hello (§3.1, §4). Empty payload -> the 11-byte header-only frame."""
    return framing.build_command(OP_BIND, b"", flag=0, seq=seq)


# --------------------------------------------------------------------------- 0x02 set-time
def build_set_time(when: _dt.datetime, seq: int = 0) -> bytes:
    """Set date/time (§3.2).

    ``when`` MUST be timezone-aware: the local wall-clock fields are taken from it and the
    UTC epoch (f8) from its POSIX timestamp, matching how the app frames the watch's local
    time plus an absolute epoch. Weekday is Monday=0 (Python ``weekday()``). f9 = derived tz
    (see :func:`_tz_field`; G5 — ported from GB, replaces the old 1140 constant).
    """
    if when.tzinfo is None:
        raise ValueError("set-time requires a timezone-aware datetime")
    epoch = int(when.timestamp())
    time_msg = (ProtobufWriter()
                .varint(1, when.year)
                .varint(2, when.month)
                .varint(3, when.day)
                .varint(4, when.hour)
                .varint(5, when.minute)
                .varint(6, when.second)
                .varint(7, when.weekday())       # Monday=0 (§3.2 f7)
                .varint(8, epoch)                 # Unix epoch seconds, UTC (§3.2 f8)
                .varint(9, _tz_field(when)))      # derived tz, wrapped (§3.2 f9; G5)
    payload = ProtobufWriter().varint(1, 2).message(2, time_msg).to_bytes()
    return framing.build_command(OP_SET_TIME, payload, flag=0, seq=seq)


# --------------------------------------------------------------------------- 0x05 device state
def build_state_query(seq: int = 0) -> bytes:
    """Read device state (§3.3). Request ``f1=1, f2=0, f3=0`` -> ``08 01 10 00 18 00``.

    Byte-exact port of the GB coordinator's ``buildStateQuery`` and of the hardcoded query the
    activation handshake sends (cli.py ``_ACTIVATE_QUERIES``). The reply carries the MAC + firmware
    build stamp — decode it with :func:`parse_state_reply`. A second, empty 0x05 frame follows as
    an end-marker.
    """
    payload = ProtobufWriter().varint(1, 1).varint(2, 0).varint(3, 0).to_bytes()
    return framing.build_command(OP_DEVICE_STATE, payload, flag=0, seq=seq)


def parse_state_reply(payload: bytes) -> dict:
    """Parse the 0x05 device-state reply (§3.3). Byte-faithful port of GB ``parseDeviceState``.

    Layout: ``f1=1, f2=2, f3 = 18 raw bytes`` = MAC(6) + six u16LE words
    (year, month, day, hour, minute, second — the firmware build stamp). Returns
    ``{"mac": "aa:bb:cc:dd:ee:ff", "firmware_build_stamp": "YYYY-MM-DD HH:MM:SS"}``. The trailing
    empty 0x05 end-marker (``f1=1, f2=2`` with no f3, or f3 < 18 bytes) yields both ``None`` so a
    caller can tell the state frame from the end-marker (mirrors GB's nullable ``DeviceState``).

    NOTE: the six-word split (yr/mo/dy/hh/mm/ss) is the GB reading; the exact field boundaries are
    marked UNRESOLVED in protocol-spec §3.3 but the whole 12-byte block is a firmware build stamp,
    not personal data.
    """
    f3 = to_dict(payload).get(3)
    if not isinstance(f3, (bytes, bytearray)) or len(f3) < 18:
        return {"mac": None, "firmware_build_stamp": None}
    mac = ":".join(f"{b:02x}" for b in f3[:6])

    def _word(o: int) -> int:
        return f3[o] | (f3[o + 1] << 8)

    stamp = (f"{_word(6):04d}-{_word(8):02d}-{_word(10):02d} "
             f"{_word(12):02d}:{_word(14):02d}:{_word(16):02d}")
    return {"mac": mac, "firmware_build_stamp": stamp}


# --------------------------------------------------------------------------- 0x18 find-device
def build_find_device(on: bool = True, seq: int = 0) -> bytes:
    """Ring/buzz the watch (§9.2). f3=1 start, f3=0 stop; same opcode for both."""
    payload = (ProtobufWriter()
               .varint(1, 2)
               .varint(2, 1)
               .varint(3, 1 if on else 0)
               .to_bytes())
    return framing.build_command(OP_FIND_DEVICE, payload, flag=0, seq=seq)


# --------------------------------------------------------------------------- 0x11 notification
def build_notification_detailed(title: str, body: str = "", *, app_id: int = 2,
                                category: int = 6, count: int = 100, seq: int = 0) -> bytes:
    """Push a rich notification (§3.4). Title text -> f6, body -> f7 (UTF-8)."""
    payload = (ProtobufWriter()
               .varint(1, 1)
               .varint(2, app_id)         # app/channel id (§3.4 f2)
               .varint(3, category)       # category/icon (§3.4 f3)
               .varint(4, count)          # id/count (§3.4 f4)
               .varint(5, 0)
               .string(6, title)          # UTF-8 title/text (§3.4 f6)
               .string(7, body)           # body, empty in the sample (§3.4 f7)
               .to_bytes())
    return framing.build_command(OP_NOTIFY_DETAILED, payload, flag=0, seq=seq)


# --------------------------------------------------------------------------- 0x13 notification
def build_notification_summary(text: str, *, count: int = 34, seq: int = 0) -> bytes:
    """Push a summary/count line (§3.5). Text -> f5. flag=1 (distinct from 0x11)."""
    payload = (ProtobufWriter()
               .varint(1, 2)
               .varint(2, 0)
               .varint(3, 2)
               .varint(4, count)          # observed constant 34 (§3.5 f4)
               .string(5, text)           # UTF-8 summary (§3.5 f5)
               .string(6, "")             # empty (§3.5 f6)
               .to_bytes())
    return framing.build_command(OP_NOTIFY_SUMMARY, payload, flag=1, seq=seq)


# --------------------------------------------------------------------------- 0x0e health sync
SUBOP_READ_DATA = 0
SUBOP_READ_STATUS = 1

# Health-sync categories = the watch's `syncType` enum. AUTHORITATIVE (vendor Dart module analysis +
# live poll 2026-07-12) — this SUPERSEDES the earlier capture-guessed labels. The correction:
# sleep is cat 3 (NOT 5), cat 5 is activity/steps, cat 4 is workout, cat 1 is stress, cat 7 is HRV.
# This is the single source of truth; health.py / records.py import from here (no re-definition).
CAT_ACTIVITY_HR = 0     # HR + daily activity (intraday HR series)              syncType 0 [CAP]
CAT_STRESS = 1          # stress                                               syncType 1
CAT_SPO2 = 2            # blood oxygen                                         syncType 2 [CAP]
CAT_SLEEP = 3           # sleep  (CORRECTED: previously mislabeled cat 5)      syncType 3
CAT_WORKOUT = 4         # workout summary + inline GPS trail                   syncType 4
CAT_ACTIVITY = 5        # activity: steps / distance / calories (live-confirmed) syncType 5
CAT_HRV = 7             # HRV    (CORRECTED: previously mislabeled BP/temp)    syncType 7
# Deprecated aliases for the old (incorrect) names — kept so nothing breaks mid-migration.
# Note CAT_SLEEP itself moved 5->3; these point at the CORRECT categories now.
CAT_METRIC_A = CAT_STRESS    # was 1 "candidate stress" -> confirmed stress
CAT_METRIC_C = CAT_HRV       # was 7 "candidate BP/temp" -> confirmed HRV
CAT_UNUSED_3 = CAT_SLEEP     # was "empty" -> sleep (cat 3)
CAT_UNUSED_4 = CAT_WORKOUT   # was "empty" -> workout (cat 4)
# Categories the app iterates during sync (§4 step 18).
SYNC_CATEGORIES = (0, 1, 2, 3, 4, 5, 7)


def build_health_sync(category: int, *, subop: int = SUBOP_READ_DATA,
                      offset: int = 0, seq: int = 0) -> bytes:
    """Request a history/health record (§6.1). flag=1.

    ``subop`` = 0 read-data record, 1 read config/status. ``category`` per §6.3.
    The request body is protobuf; the *response* is a binary record (no CRC).
    """
    payload = (ProtobufWriter()
               .varint(1, subop)
               .varint(2, category)
               .varint(3, offset)
               .to_bytes())
    return framing.build_command(OP_HEALTH_SYNC, payload, flag=1, seq=seq)


# --------------------------------------------------------------------------- 0x07 alarm
@dataclass
class Alarm:
    """One alarm entry (§9.3). ``weekdays`` = 7 bytes, one per day; all-zero = one-shot."""
    index: int
    hour: int
    minute: int
    enabled: bool = True
    weekdays: bytes = b"\x00" * 7

    def to_message(self) -> bytes:
        if len(self.weekdays) != 7:
            raise ValueError("weekdays must be exactly 7 bytes")
        return (ProtobufWriter()
                .varint(1, self.index)
                .bool(2, self.enabled)
                .varint(3, 0)                    # observed constant (§9.3 unresolved)
                .varint(4, self.hour)            # hour (§9.3 f4)
                .varint(5, self.minute)          # minute (§9.3 f5)
                .varint(6, 1)                    # observed constant
                .bytes(7, self.weekdays)         # weekday-repeat (§9.3 f7)
                .varint(8, 1)                    # observed constant
                .varint(9, 4)                    # type ~4 (§9.3 f9)
                .varint(10, 10)                  # snooze ~10 (§9.3 f10)
                .to_bytes())


def build_alarm_get(seq: int = 0) -> bytes:
    """Read alarms (§9.3): f1=1 (get), f2=0."""
    payload = ProtobufWriter().varint(1, 1).varint(2, 0).to_bytes()
    return framing.build_command(OP_ALARM, payload, flag=0, seq=seq)


def build_alarm_set(alarms: Sequence[Alarm], seq: int = 0) -> bytes:
    """Write alarms (§9.3): f1=2 (set), f2=count, then a repeated f3 entry per alarm."""
    w = ProtobufWriter().varint(1, 2).varint(2, len(alarms))
    for a in alarms:
        w.message(3, a.to_message())
    return framing.build_command(OP_ALARM, w.to_bytes(), flag=0, seq=seq)


# --------------------------------------------------------------------------- 0x12 weather
@dataclass
class Weather:
    """Weather push (§3.7). Fields marked solid in the spec are modelled; several
    unlabelled sub-fields in the capture are omitted (their semantics are unresolved), so
    this is a faithful subset -- structurally correct, not yet hardware-verified."""
    city: str
    month: int
    day: int
    hour: int
    minute: int
    condition: int                              # current condition code (§3.7 f5)
    temp_current: int                           # current temp, C (§3.7 f6)
    temp_max: int                               # (§3.7 f8)
    temp_min: int                               # (§3.7 f9)
    hourly: Sequence[Tuple[int, int]] = ()      # up to 24 x (hi, temp) (§3.7 f11)
    daily: Sequence[Tuple[int, int, int]] = ()  # up to 3 x (hi, lo, cond) (§3.7 f19)
    pressure_hpa: float = 1013.25               # -> f22 = hPa*100 (§3.7 f22)


def build_weather(weather: Weather, seq: int = 0) -> bytes:
    """Build a weather push frame (§3.7). Large frames fragment via :func:`frame_to_pdus`."""
    fc = (ProtobufWriter()
          .varint(1, weather.month)
          .varint(2, weather.day)
          .varint(3, weather.hour)
          .varint(4, weather.minute)
          .varint(5, weather.condition)
          .varint(6, weather.temp_current)
          # UI field mapping SOLVED by differential calibration (2026-07-15) — corrects §3.7, whose
          # f7/f9 labels were degenerate (current==min at capture, so both read 22). Two pushes
          # decoded it: the range-LOW tracked f9 (22→11), the big number stayed f7's constant.
          # TRUE: f7 = UI "current" (big number) · f8 = range high (max) · f9 = range low (real min).
          .varint(7, weather.temp_current)   # f7 -> big "current" number
          .varint(8, weather.temp_max)       # f8 -> range high
          .varint(9, weather.temp_min)       # f9 -> range low
          .string(10, weather.city))
    # The native widget scrapes the big "current" temp + the condition icon from the FORECAST arrays;
    # with none populated it reads stale/uninitialised slots (calibration showed the big number =
    # temp_min and a run-to-run-varying icon). Synthesize one entry each from the current args when
    # the caller gives none (§3.7 f11 hourly = {hi, temp}, f19 daily = {hi, lo, cond}).
    hourly = list(weather.hourly)[:24] or [(weather.temp_max, weather.temp_current)]
    for hi, temp in hourly:
        fc.message(11, ProtobufWriter().varint(1, hi).varint(2, temp))
    daily = list(weather.daily)[:3] or [(weather.temp_max, weather.temp_min, weather.condition)]
    for hi, lo, cond in daily:
        fc.message(19, ProtobufWriter().varint(1, hi).varint(2, lo).varint(3, cond))
    fc.varint(22, int(round(weather.pressure_hpa * 100)))
    payload = ProtobufWriter().varint(1, 2).varint(2, 1).message(3, fc).to_bytes()
    return framing.build_command(OP_WEATHER, payload, flag=0, seq=seq)
