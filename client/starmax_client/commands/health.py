"""Health command surface for the Starmax GTX2 (STANDALONE client, Track B / module B1).

Covers the watch's full health domain: per-category detection **switches**, **history sync**,
**realtime** measurement triggers, and **parsers** for every health reply — HR, SpO2, blood
pressure, HRV, skin-temperature, respiration, blood-sugar/CGM, ECG, stress/pressure, MET/MAI,
and female-health.

Provenance / clean-room
-----------------------
This is the STANDALONE lane: builders/parsers are derived from the vendor APK protobuf schema
(package ``com.starmax.bluetoothsdk``, outer message ``Notify``) — internal reverse-engineering
notes NOT shipped in this repo — and, where a real frame exists, from the capture-verified
``docs/protocol-spec.md``. It is NOT clean-room and must never inform the Gadgetbridge PR.

Each builder's docstring tags its confidence:
  * ``[CAP]``   — byte-shape confirmed against a real capture frame (protocol-spec §5/§6).
  * ``[SCHEMA]`` — payload built from the APK schema message; the **wire opcode is UNRESOLVED**
                   (the feature never appeared in any capture, see gap-analysis §E). These frames
                   are experimental — prefer ``--dry-run`` until validated on hardware.

Reuses the shared core (``framing`` + ``protobuf``); it does not reimplement either.
"""
from __future__ import annotations

import argparse
from typing import Dict, List, Optional, Sequence

from starmax_client import framing
from starmax_client.protobuf import ProtobufWriter, parse as pb_parse
# Single source of truth for the syncType/category enum (AUTHORITATIVE, see base.py).
from starmax_client.commands.base import (  # noqa: F401  (re-exported for callers/tests)
    CAT_ACTIVITY_HR, CAT_STRESS, CAT_SPO2, CAT_SLEEP, CAT_WORKOUT, CAT_ACTIVITY, CAT_HRV,
    CAT_METRIC_A, CAT_METRIC_C, SYNC_CATEGORIES)

# --------------------------------------------------------------------------- opcodes / flags
OP_HEALTH = 0x0E          # [CAP] health opcode: flag=0 switches, flag=1 history (protocol-spec §5/§6)

FLAG_SWITCH = 0x00        # 0x0e flag=0 — detection switches (protobuf)
FLAG_HISTORY = 0x01       # 0x0e flag=1 — history/data sync (request protobuf, reply binary)

# History sub-op (flag=1 request f1), protocol-spec §6.1
SUBOP_DATA = 0            # read a data record
SUBOP_STATUS = 1          # read config / status

# Health categories on the 0x0e flag=1 channel = the watch syncType enum. Imported from base
# (AUTHORITATIVE): 0=HR, 1=stress, 2=SpO2, 3=sleep, 4=workout, 5=activity/steps, 7=HRV.
SWITCH_CATEGORIES = SYNC_CATEGORIES  # the set the vendor app writes on connect (§4 step 16)

# metric -> history category. RESOLVED from captures; others are candidates or genuinely unknown
# (never triggered in any capture — see gap-analysis §E). Use build_history_sync(category=...) to
# probe an unmapped metric.
METRIC_CATEGORY: Dict[str, int] = {
    "hr": CAT_ACTIVITY_HR,     # cat 0
    "spo2": CAT_SPO2,          # cat 2
    "sleep": CAT_SLEEP,        # cat 3 (CORRECTED — was cat 5)
    "stress": CAT_STRESS,      # cat 1 (authoritative)
    "activity": CAT_ACTIVITY,  # cat 5 — steps/distance/calories (live-confirmed)
    "steps": CAT_ACTIVITY,     # alias of activity
    "workout": CAT_WORKOUT,    # cat 4 — workout summary + GPS trail
    "hrv": CAT_HRV,            # cat 7 (authoritative — was mislabeled BP)
}
# Metrics whose wire category is unknown (schema message exists, never seen on the wire):
UNMAPPED_METRICS = ("temp", "respiration", "blood_sugar", "met", "mai", "ecg", "female_health")


# =========================================================================== SWITCHES (0x0e f0)
def build_health_switch_read(seq: int = 0) -> bytes:
    """[CAP] Read the per-category health-detection switches. Schema: ``Notify.HealthOpen`` (reply).

    Opcode 0x0e flag=0, payload ``f1=1`` (protocol-spec §5). Byte-exact vs pairing capture
    (``c1 08 01 01 00 0e 0d 00 00 00 00 08 01``).
    """
    payload = ProtobufWriter().varint(1, 1).to_bytes()
    return framing.build_command(OP_HEALTH, payload, flag=FLAG_SWITCH, seq=seq)


def build_health_switch_write(categories: Sequence[int] = SWITCH_CATEGORIES,
                              value: int = 0, seq: int = 0) -> bytes:
    """[CAP] Write per-category health-detection switches. Schema: category-indexed switch write.

    Opcode 0x0e flag=0, payload ``f1=2, f2=repeated{ f1=category, f2=value }`` (protocol-spec §5,
    §4 step 16). The captured connect-time write uses ``value=0`` for categories
    ``(0,1,2,3,4,5,7)`` — reproduced byte-exact with the defaults. NOTE: the on/off semantics of
    ``f2`` are UNRESOLVED (the capture shows 0; the vendor may treat non-zero as "enable" — see
    gap-analysis D8). Pass ``value=1`` to try enabling.
    """
    w = ProtobufWriter().varint(1, 2)
    for cat in categories:
        w.message(2, ProtobufWriter().varint(1, cat).varint(2, value))
    return framing.build_command(OP_HEALTH, w.to_bytes(), flag=FLAG_SWITCH, seq=seq)


# =========================================================================== HISTORY (0x0e f1)
def build_history_sync(category: int, *, subop: int = SUBOP_DATA,
                       offset: int = 0, seq: int = 0) -> bytes:
    """[CAP] Request one history record. Schema: ``Notify.*History`` (reply is a BINARY record).

    Opcode 0x0e flag=1, payload ``f1=subop, f2=category, f3=offset`` (protocol-spec §6.1).
    ``subop`` 0=read-data, 1=read-status. Byte-exact vs pairing capture for cat 0 / cat 5.
    The response is a fixed binary record (no CRC) — decode it with ``starmax_client.records``.
    """
    payload = (ProtobufWriter()
               .varint(1, subop)
               .varint(2, category)
               .varint(3, offset)
               .to_bytes())
    return framing.build_command(OP_HEALTH, payload, flag=FLAG_HISTORY, seq=seq)


def build_history_status(category: int, *, offset: int = 0, seq: int = 0) -> bytes:
    """[CAP] Read-status variant of :func:`build_history_sync` (``subop=1``, protocol-spec §6.1)."""
    return build_history_sync(category, subop=SUBOP_STATUS, offset=offset, seq=seq)


# Per-metric convenience wrappers over the category channel.
def build_hr_history(seq: int = 0, **kw) -> bytes:
    """[CAP] HR / daily-activity history (category 0). Schema: ``Notify.HeartRateHistory``."""
    return build_history_sync(CAT_ACTIVITY_HR, seq=seq, **kw)


def build_spo2_history(seq: int = 0, **kw) -> bytes:
    """[CAP] SpO2 history (category 2). Schema: ``Notify.BloodOxygenHistory``."""
    return build_history_sync(CAT_SPO2, seq=seq, **kw)


def build_sleep_history(seq: int = 0, **kw) -> bytes:
    """[CAP] Sleep history (category 3 — CORRECTED, was cat 5). Schema: ``Notify.SleepHistory``."""
    return build_history_sync(CAT_SLEEP, seq=seq, **kw)


def build_activity_history(seq: int = 0, **kw) -> bytes:
    """Activity history (category 5): daily steps / distance / calories.

    Wire opcode 0x0e [CAP]; the request is a normal history-sync. The cat-5 record's
    ActivityDataModel field map (steps/distance/calories) is decoded by
    :func:`starmax_client.records.extract_activity` — LIVE-CONFIRMED 2026-07-12.
    """
    return build_history_sync(CAT_ACTIVITY, seq=seq, **kw)


def build_workout_history(seq: int = 0, **kw) -> bytes:
    """Workout history (category 4): SportDataModel summary + inline GPS trail.

    Wire opcode 0x0e [CAP] history-sync (the RIGHT layer — supersedes the wrong-layer Java-SDK
    0x61). The record carries the workout summary; the GPS trail is inline (see
    :mod:`starmax_client.gpstrack`) and is UNVERIFIED pending a real GPS-locked workout.
    """
    return build_history_sync(CAT_WORKOUT, seq=seq, **kw)


def build_metric_history(metric: str, seq: int = 0, **kw) -> bytes:
    """History request for a named metric via :data:`METRIC_CATEGORY`.

    ``[CAP]`` for hr/spo2/sleep; ``[SCHEMA]`` candidate for stress(cat1)/bp(cat7); raises for
    metrics with no known wire category (use :func:`build_history_sync` with an explicit category
    to probe them — see gap-analysis §E).
    """
    if metric not in METRIC_CATEGORY:
        raise ValueError(
            f"metric {metric!r} has no known history category "
            f"(known: {sorted(METRIC_CATEGORY)}; unmapped: {list(UNMAPPED_METRICS)}). "
            f"Use build_history_sync(category=N) to probe.")
    return build_history_sync(METRIC_CATEGORY[metric], seq=seq, **kw)


# =========================================================================== REALTIME [SCHEMA]
# The realtime channel never appeared in any capture, so its WIRE OPCODE IS UNRESOLVED. The
# payloads below faithfully build the APK schema messages; the frame is wrapped with `opcode`
# (default OP_HEALTH as the best-guess health-domain opcode). Treat as experimental: prefer
# --dry-run, or override `opcode=` once discovered from a fresh capture.
def build_realtime_open(*, gsensor: bool = False, steps: bool = False, heart_rate: bool = False,
                        blood_pressure: bool = False, blood_oxygen: bool = False, temp: bool = False,
                        blood_sugar: bool = False, opcode: int = OP_HEALTH, seq: int = 0) -> bytes:
    """[SCHEMA] Enable realtime streaming for the given metrics. Schema: ``Notify.RealTimeOpen``.

    Fields (bool): ``f2=gsensor f3=steps f4=heartRate f5=bloodPressure f6=bloodOxygen f7=temp
    f8=bloodSugar`` (``f1=status`` omitted on requests). ⚠ wire opcode UNRESOLVED — see module note.
    """
    payload = (ProtobufWriter()
               .bool(2, gsensor).bool(3, steps).bool(4, heart_rate).bool(5, blood_pressure)
               .bool(6, blood_oxygen).bool(7, temp).bool(8, blood_sugar)
               .to_bytes())
    return framing.build_command(opcode, payload, flag=FLAG_SWITCH, seq=seq)


def build_realtime_measure(data_type: int, *, opcode: int = OP_HEALTH, seq: int = 0) -> bytes:
    """[SCHEMA] One-shot realtime measurement trigger. Schema: ``Notify.RealTimeMeasure``.

    Payload ``f1=dataType`` (the ``data`` result field f2 is reply-only). ⚠ wire opcode UNRESOLVED.
    """
    payload = ProtobufWriter().varint(1, data_type).to_bytes()
    return framing.build_command(opcode, payload, flag=FLAG_SWITCH, seq=seq)


# =========================================================================== CONFIG [SCHEMA]
# Per-metric monitoring config. These map to distinct SDK commands (e.g. HR-interval config is
# SDK 0x31 REV_Interval_HR) but their GTX2 WIRE opcode was never captured — UNRESOLVED, flagged
# like the realtime block. Payloads are faithful to the schema; prefer --dry-run.
def build_hr_config(*, start_hour: int = 0, start_minute: int = 0, end_hour: int = 23,
                    end_minute: int = 59, period: int = 10, alarm_threshold: int = 0,
                    oxygen_period: int = 0, opcode: int = OP_HEALTH, seq: int = 0) -> bytes:
    """[SCHEMA] Continuous HR-monitoring config. Schema: ``Notify.HeartRate``.

    ``f2..f8 = startHour, startMinute, endHour, endMinute, period(min), alarmThreshold(bpm,
    0=off), oxygenPeriod`` (``f1=status`` reply-only). ⚠ wire opcode UNRESOLVED (SDK 0x31).
    """
    payload = (ProtobufWriter()
               .varint(2, start_hour).varint(3, start_minute).varint(4, end_hour)
               .varint(5, end_minute).varint(6, period).varint(7, alarm_threshold)
               .varint(8, oxygen_period)
               .to_bytes())
    return framing.build_command(opcode, payload, flag=FLAG_SWITCH, seq=seq)


def build_health_interval(*, metric_type: int, measure_interval: int, store_interval: int = 0,
                          opcode: int = OP_HEALTH, seq: int = 0) -> bytes:
    """[SCHEMA] Auto-measure interval for a metric. Schema: ``Notify.HealthInterval``.

    ``f1=type f2=measureInterval(min) f3=storeInterval``. ⚠ wire opcode UNRESOLVED.
    """
    payload = (ProtobufWriter()
               .varint(1, metric_type).varint(2, measure_interval).varint(3, store_interval)
               .to_bytes())
    return framing.build_command(opcode, payload, flag=FLAG_SWITCH, seq=seq)


def build_female_health(*, number_of_days: int, cycle_days: int, year: int, month: int, day: int,
                        reminder_on: bool = False, opcode: int = OP_HEALTH, seq: int = 0) -> bytes:
    """[SCHEMA] Set the menstrual-cycle config. Schema: ``Notify.FemaleHealthData``.

    ``f2..f7 = numberOfDays, cycleDays, year, month, day, reminderOnOff`` (``f1=status``
    reply-only). ⚠ wire opcode UNRESOLVED. Pair with :func:`parse_female_health`.
    """
    payload = (ProtobufWriter()
               .varint(2, number_of_days).varint(3, cycle_days).varint(4, year)
               .varint(5, month).varint(6, day).bool(7, reminder_on)
               .to_bytes())
    return framing.build_command(opcode, payload, flag=FLAG_SWITCH, seq=seq)


# =========================================================================== PARSERS
def _fields(payload: bytes) -> Dict[int, object]:
    """First value per field number (health replies are non-repeated except the data arrays)."""
    out: Dict[int, object] = {}
    for f, _w, v in pb_parse(payload):
        if f not in out:
            out[f] = v
    return out


def _history_points(payload: bytes) -> List[Dict[str, int]]:
    """Decode the repeated ``HistoryData{hour,minute,value}`` entries (field 7 or 8)."""
    pts: List[Dict[str, int]] = []
    for f, w, v in pb_parse(payload):
        if f in (7, 8) and w == 2 and isinstance(v, (bytes, bytearray)):
            sub = _fields(bytes(v))
            if 1 in sub or 2 in sub or 3 in sub:
                pts.append({"hour": sub.get(1, 0), "minute": sub.get(2, 0), "value": sub.get(3, 0)})
    return pts


def parse_history(payload: bytes) -> dict:
    """Parse a protobuf history reply (shared shape of ``Notify.*History``, schema).

    ``{status, interval, year, month, day, data_length, points:[{hour,minute,value}]}``. Applies to
    HeartRate/BloodOxygen/Hrv/NightHrv/Pressure/Respiration/Temp/BloodSugar history (schema).
    NOTE: the captured 0x0e flag=1 replies are BINARY records (use ``starmax_client.records``); this
    parser is for the schema's protobuf history form.
    """
    d = _fields(payload)
    return {
        "status": d.get(1, 0), "interval": d.get(2, 0),
        "year": d.get(3, 0), "month": d.get(4, 0), "day": d.get(5, 0),
        "data_length": d.get(6, 0), "points": _history_points(payload),
    }


def parse_health_detail(payload: bytes) -> dict:
    """[SCHEMA] Parse the live snapshot ``Notify.HealthDetail`` (22 fields)."""
    d = _fields(payload)
    keys = {1: "status", 2: "total_steps", 3: "total_heat", 4: "total_distance", 5: "total_sleep",
            6: "total_deep_sleep", 7: "total_light_sleep", 8: "heart_rate", 9: "bp_fz", 10: "bp_ss",
            11: "blood_oxygen", 12: "pressure", 13: "met", 14: "mai", 15: "temp", 16: "blood_sugar",
            17: "is_wear", 18: "respiration_rate", 19: "shake_head", 20: "hrv", 21: "hrv_low",
            22: "hrv_high"}
    return {name: d.get(num, 0) for num, name in keys.items()}


def parse_health_open(payload: bytes) -> dict:
    """[SCHEMA] Parse the detection-switch state ``Notify.HealthOpen`` (bool per metric)."""
    d = _fields(payload)
    keys = {1: "status", 2: "heart_rate", 3: "blood_pressure", 4: "blood_oxygen", 5: "pressure",
            6: "temp", 7: "blood_sugar", 8: "respiration_rate", 9: "data_length", 10: "hrv_rmssd"}
    return {name: d.get(num, 0) for num, name in keys.items()}


# 0x0e flag=0 READ reply field-number -> sync CATEGORY (spec §5), mirrors the GB coordinator's
# SWITCH_FIELD_TO_CATEGORY. f8 is skipped (there is no category 6).
SWITCH_FIELD_TO_CATEGORY = {2: 0, 3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 9: 7}


def parse_health_switch_state(payload: bytes) -> dict:
    """[CAP] Category-keyed view of the 0x0e flag=0 READ reply (spec §5). G9 — mirrors GB
    ``parseHealthSwitchState``: ``{category_index: enabled}`` for cats 0,1,2,3,4,5,7.

    Requires ``f1=1`` (a READ reply); a WRITE ack (``f1=2, f2=273``) carries no per-category bits
    and returns ``{}``, so callers can tell the two 0x0e flag=0 replies apart. This complements
    :func:`parse_health_open` (metric-keyed labels) and is the form a category-indexed sync-gate
    (audit G7) would consume.
    """
    d = _fields(payload)
    if d.get(1) != 1:
        return {}
    return {cat: bool(d.get(field, 0)) for field, cat in SWITCH_FIELD_TO_CATEGORY.items()}


def parse_realtime_data(payload: bytes) -> dict:
    """[SCHEMA] Parse a streamed ``Notify.RealTimeData`` sample."""
    d = _fields(payload)
    keys = {2: "steps", 3: "calories", 4: "distance", 5: "heart_rate", 6: "bp_ss", 7: "bp_fz",
            8: "blood_oxygen", 9: "temp", 10: "blood_sugar"}
    out = {name: d.get(num, 0) for num, name in keys.items()}
    out["gsensors"] = [
        {"x": s.get(1, 0), "y": s.get(2, 0), "z": s.get(3, 0)}
        for v in (val for f, w, val in pb_parse(payload) if f == 1 and w == 2)
        for s in [_fields(bytes(v))]
    ]
    return out


def parse_health_measure(payload: bytes) -> dict:
    """[SCHEMA] Parse ``Notify.HealthMeasureData``/``RealTimeMeasure`` (status/type + packed data)."""
    d = _fields(payload)
    return {"status": d.get(1, 0), "type": d.get(2, d.get(1, 0)),
            "data": d.get(3, d.get(2, b""))}


def parse_female_health(payload: bytes) -> dict:
    """[SCHEMA] Parse ``Notify.FemaleHealthData`` (cycle config)."""
    d = _fields(payload)
    keys = {1: "status", 2: "number_of_days", 3: "cycle_days", 4: "year", 5: "month", 6: "day",
            7: "reminder_on"}
    return {name: d.get(num, 0) for num, name in keys.items()}


def parse_ecg(payload: bytes) -> dict:
    """[SCHEMA] Parse ``Notify.EcgSyncContent`` header (ECG sample array left as packed bytes)."""
    d = _fields(payload)
    return {"status": d.get(1, 0), "history_count": d.get(2, 0), "current_index": d.get(3, 0),
            "data_length": d.get(4, 0), "current_time": d.get(5, 0), "avg_heart_rate": d.get(6, 0),
            "test_result": d.get(8, 0), "has_next": bool(d.get(10, 0)),
            "not_valid": bool(d.get(11, 0)), "progress": d.get(12, 0)}


def parse_hr_config(payload: bytes) -> dict:
    """[SCHEMA] Parse an HR-config reply ``Notify.HeartRate``."""
    d = _fields(payload)
    keys = {1: "status", 2: "start_hour", 3: "start_minute", 4: "end_hour", 5: "end_minute",
            6: "period", 7: "alarm_threshold", 8: "oxygen_period"}
    return {name: d.get(num, 0) for num, name in keys.items()}


def parse_health_interval(payload: bytes) -> dict:
    """[SCHEMA] Parse ``Notify.HealthInterval`` (type / measure / store interval)."""
    d = _fields(payload)
    return {"type": d.get(1, 0), "measure_interval": d.get(2, 0), "store_interval": d.get(3, 0)}


def parse_customized_hr(payload: bytes) -> dict:
    """[SCHEMA] Parse ``Notify.CustomizedHeartRateData`` (one-shot HR result)."""
    d = _fields(payload)
    return {"status": d.get(1, 0), "return_status": d.get(2, 0), "heart_rate": d.get(3, 0)}


# =========================================================================== registry
# name -> builder callable. Pure (no I/O); consumed by the CLI and B5's smoke test.
COMMANDS = {
    "health-switch-read": build_health_switch_read,
    "health-switch-write": build_health_switch_write,
    "history-sync": build_history_sync,
    "history-status": build_history_status,
    "hr-history": build_hr_history,
    "spo2-history": build_spo2_history,
    "sleep-history": build_sleep_history,
    "activity-history": build_activity_history,
    "workout-history": build_workout_history,
    "realtime-open": build_realtime_open,
    "realtime-measure": build_realtime_measure,
    "hr-config": build_hr_config,
    "health-interval": build_health_interval,
    "female-health-set": build_female_health,
}

PARSERS = {
    "history": parse_history, "health-detail": parse_health_detail,
    "health-open": parse_health_open, "health-switch-state": parse_health_switch_state,
    "realtime-data": parse_realtime_data,
    "health-measure": parse_health_measure, "female-health": parse_female_health, "ecg": parse_ecg,
    "hr-config": parse_hr_config, "health-interval": parse_health_interval,
    "customized-hr": parse_customized_hr,
}

# Sample args so B5's dry-run smoke gate can exercise builders with required params.
SMOKE_ARGS = {
    "history-sync": {"category": 0}, "history-status": {"category": 0},
    "realtime-measure": {"data_type": 0},
    "health-interval": {"metric_type": 0, "measure_interval": 5},
    "female-health-set": {"number_of_days": 5, "cycle_days": 28, "year": 2026, "month": 7, "day": 11},
}

GROUP = "health"


# --------------------------------------------------------------------------- CLI wiring
def register(subparsers, client=None) -> None:
    """Add the health subcommands to ``subparsers``. B5 auto-discovers this.

    ``client`` is a connected ``StarmaxClient``-like object (``next_seq``/``request``/``send_raw``);
    it may be ``None`` at registration time (handlers read it from ``args._client`` if the CLI sets
    it later). Every subcommand supports ``--dry-run`` (print the hex frame, don't transmit).
    """
    def _add(name: str, help_: str) -> argparse.ArgumentParser:
        sp = subparsers.add_parser(name, help=help_)
        sp.add_argument("--dry-run", action="store_true", help="print the hex frame, don't send")
        sp.add_argument("--seq", type=lambda s: int(s, 0), default=0, help="frame seq (default 0)")
        sp.add_argument("--force", action="store_true",
                        help="send even if the wire opcode/payload is unverified (docs/command-audit.md)")
        sp.set_defaults(_client=client)
        return sp

    sp = _add("health-switch-read", "read per-category health-detection switches [CAP]")
    sp.set_defaults(func=_mk_handler(lambda a: build_health_switch_read(seq=a.seq),
                                     OP_HEALTH, expect_reply=True))

    sp = _add("health-switch-write", "write per-category health-detection switches [CAP]")
    sp.add_argument("--value", type=int, default=0, help="switch value (0=capture default, 1=enable)")
    sp.add_argument("--categories", default="0,1,2,3,4,5,7", help="comma list of categories")
    sp.set_defaults(func=_mk_handler(
        lambda a: build_health_switch_write(
            [int(x) for x in a.categories.split(",") if x != ""], value=a.value, seq=a.seq),
        OP_HEALTH))

    sp = _add("history-sync", "request one history record by category [CAP]")
    sp.add_argument("category", type=int, help="health category (0=HR,2=SpO2,5=sleep; 1/7 candidate)")
    sp.add_argument("--status", action="store_true", help="read-status (subop 1) instead of data")
    sp.add_argument("--offset", type=int, default=0)
    sp.set_defaults(func=_mk_handler(
        lambda a: build_history_sync(a.category, subop=(SUBOP_STATUS if a.status else SUBOP_DATA),
                                     offset=a.offset, seq=a.seq),
        OP_HEALTH))

    for metric, builder in (("hr", build_hr_history), ("spo2", build_spo2_history),
                            ("sleep", build_sleep_history), ("activity", build_activity_history),
                            ("workout", build_workout_history)):
        sp = _add(f"{metric}-history", f"{metric} history sync [CAP opcode 0x0e]")
        sp.set_defaults(func=_mk_handler(lambda a, b=builder: b(seq=a.seq), OP_HEALTH))

    sp = _add("realtime-open", "enable realtime streaming (SCHEMA-derived, opcode UNRESOLVED)")
    for m in ("heart-rate", "blood-oxygen", "blood-pressure", "temp", "steps", "gsensor", "blood-sugar"):
        sp.add_argument(f"--{m}", action="store_true")
    sp.set_defaults(func=_mk_handler(
        lambda a: build_realtime_open(
            heart_rate=a.heart_rate, blood_oxygen=a.blood_oxygen, blood_pressure=a.blood_pressure,
            temp=a.temp, steps=a.steps, gsensor=a.gsensor, blood_sugar=a.blood_sugar, seq=a.seq),
        OP_HEALTH))

    sp = _add("realtime-measure", "one-shot realtime measure (SCHEMA-derived, opcode UNRESOLVED)")
    sp.add_argument("data_type", type=int, help="metric data-type id")
    sp.set_defaults(func=_mk_handler(lambda a: build_realtime_measure(a.data_type, seq=a.seq),
                                     OP_HEALTH))

    sp = _add("hr-config", "HR monitoring window/interval/alarm (SCHEMA-derived, opcode UNRESOLVED)")
    sp.add_argument("--start-hour", type=int, default=0)
    sp.add_argument("--end-hour", type=int, default=23)
    sp.add_argument("--period", type=int, default=10, help="measure period, minutes")
    sp.add_argument("--alarm-threshold", type=int, default=0, help="high-HR alarm bpm (0=off)")
    sp.set_defaults(func=_mk_handler(
        lambda a: build_hr_config(start_hour=a.start_hour, end_hour=a.end_hour, period=a.period,
                                  alarm_threshold=a.alarm_threshold, seq=a.seq), OP_HEALTH))

    sp = _add("health-interval", "per-metric auto-measure interval (SCHEMA-derived, opcode UNRESOLVED)")
    sp.add_argument("metric_type", type=int, help="metric type id")
    sp.add_argument("--measure-interval", type=int, default=5, help="minutes")
    sp.add_argument("--store-interval", type=int, default=0)
    sp.set_defaults(func=_mk_handler(
        lambda a: build_health_interval(metric_type=a.metric_type, measure_interval=a.measure_interval,
                                        store_interval=a.store_interval, seq=a.seq), OP_HEALTH))

    sp = _add("female-health-set", "set menstrual-cycle config (SCHEMA-derived, opcode UNRESOLVED)")
    sp.add_argument("--days", type=int, required=True, help="period length, days")
    sp.add_argument("--cycle", type=int, required=True, help="cycle length, days")
    sp.add_argument("--year", type=int, required=True)
    sp.add_argument("--month", type=int, required=True)
    sp.add_argument("--day", type=int, required=True)
    sp.add_argument("--reminder", action="store_true")
    sp.set_defaults(func=_mk_handler(
        lambda a: build_female_health(number_of_days=a.days, cycle_days=a.cycle, year=a.year,
                                      month=a.month, day=a.day, reminder_on=a.reminder, seq=a.seq),
        OP_HEALTH))


def _mk_handler(build, opcode: int, *, expect_reply: bool = False):
    """Wrap a frame-builder into an async CLI handler honouring ``--dry-run``."""
    async def handler(args) -> int:
        frame = build(args)
        if getattr(args, "dry_run", False):
            print(frame.hex())
            return 0
        client = getattr(args, "_client", None)
        if client is None:
            print("no client connected; re-run with --dry-run to preview the frame")
            return 2
        if expect_reply:
            reply = await client.request(frame, opcode, timeout=5.0)
            print(reply.payload.hex() if reply is not None else "(no reply)")
        else:
            await client.send_raw(frame)
            print(f"sent 0x{opcode:02x} ({len(frame)} bytes)")
        return 0
    return handler
