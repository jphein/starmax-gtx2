"""Tests for commands/health.py (Track B / module B1).

health.py lives at ``starmax_client/commands/health.py``. Until B5 turns ``commands/`` into a
package, it is not importable by the dotted path, so we load it by file path — this keeps B1's
tests green independently of the integration step. Byte-exact vectors are real captured frames
(same provenance as ``tests/fixtures.py`` / protocol-spec §5-§6).
"""
import argparse
import asyncio
import importlib.util
import io
import pathlib
from contextlib import redirect_stdout

from starmax_client import framing

_HP = pathlib.Path(__file__).resolve().parent.parent / "starmax_client" / "commands" / "health.py"
_spec = importlib.util.spec_from_file_location("starmax_health_mod", _HP)
health = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(health)


# ---------------------------------------------------------------- byte-exact vs capture
# 0x0e flag=0 switch read/write, extracted from captures/pairing-*.log (app->watch).
SWITCH_READ_SEQ08 = "c1080101000e0d000000000801"
SWITCH_WRITE_SEQ09 = ("c1090101000e370000000008021204080010001204080110001204080210001204"
                      "0803100012040804100012040805100012040807 1000".replace(" ", ""))
# 0x0e flag=1 history read-data, cat0 seq0x0a / cat5 seq0x12 (== tests/fixtures.py).
HIST_CAT0_SEQ0A = "c10a0101010e1100000000080010001800"
HIST_CAT5_SEQ12 = "c1120101010e1100000000080010051800"


def test_switch_read_byte_exact():
    assert health.build_health_switch_read(seq=0x08).hex() == SWITCH_READ_SEQ08


def test_switch_write_byte_exact():
    # defaults reproduce the captured connect-time write: cats (0,1,2,3,4,5,7), value 0.
    assert health.build_health_switch_write(seq=0x09).hex() == SWITCH_WRITE_SEQ09


def test_switch_write_enable_matches_gb_and_activation_handshake():
    # GB's buildHealthSwitchWrite defaults SWITCH_ON=1 (enable); the client reproduces that ENABLE
    # write when value=1 is passed. The payload must be byte-identical to the hardcoded enable write
    # in cli.py's activation handshake (_ACTIVATE_FINALIZE) — proving the enable behaviour is ported.
    frame = health.build_health_switch_write([0, 1, 2, 3, 4, 5, 7], value=1)
    fr = framing.parse_frame(frame, direction=framing.DIR_APP_TO_WATCH)
    ACTIVATION_ENABLE_PAYLOAD = (
        "0802120408001001120408011001120408021001120408031001"
        "120408041001120408051001120408071001")
    assert fr.payload.hex() == ACTIVATION_ENABLE_PAYLOAD
    # Each per-category entry sets f2 (on) = 1, mirroring GB's SWITCH_ON.
    from starmax_client import protobuf as pb
    entries = [pb.to_dict(v) for f, w, v in pb.parse(fr.payload) if f == 2]
    assert entries == [{1: c, 2: 1} for c in (0, 1, 2, 3, 4, 5, 7)]


def test_history_sync_byte_exact():
    assert health.build_history_sync(0, seq=0x0a).hex() == HIST_CAT0_SEQ0A
    assert health.build_history_sync(5, seq=0x12).hex() == HIST_CAT5_SEQ12


def test_metric_wrappers_match_categories():
    assert health.build_hr_history(seq=0x0a).hex() == HIST_CAT0_SEQ0A
    # sleep is category 3 (CORRECTED — was mislabeled cat 5)
    assert health.build_sleep_history(seq=0x12) == health.build_history_sync(3, seq=0x12)
    assert health.build_spo2_history(seq=0x01) == health.build_history_sync(2, seq=0x01)
    assert health.build_activity_history(seq=0x01) == health.build_history_sync(5, seq=0x01)
    assert health.build_workout_history(seq=0x01) == health.build_history_sync(4, seq=0x01)


def test_history_status_subop():
    fr = framing.parse_frame(health.build_history_status(2, seq=3), direction=framing.DIR_APP_TO_WATCH)
    assert fr.opcode == health.OP_HEALTH and fr.flag == health.FLAG_HISTORY
    # payload f1 = subop 1 (status), f2 = category 2
    d = {f: v for f, _w, v in __import__("starmax_client.protobuf", fromlist=["parse"]).parse(fr.payload)}
    assert d[1] == health.SUBOP_STATUS and d[2] == 2


# ---------------------------------------------------------------- framing round-trips
def test_switch_write_roundtrip_value_and_cats():
    frame = health.build_health_switch_write([0, 2, 5], value=1, seq=7)
    fr = framing.parse_frame(frame, direction=framing.DIR_APP_TO_WATCH)
    assert fr.opcode == 0x0E and fr.flag == 0
    from starmax_client import protobuf as pb
    top = pb.parse(fr.payload)
    assert top[0] == (1, 0, 2)  # f1 = 2 (write)
    entries = [pb.to_dict(v) for f, w, v in top if f == 2]
    assert entries == [{1: 0, 2: 1}, {1: 2, 2: 1}, {1: 5, 2: 1}]


def test_realtime_open_schema_fields():
    frame = health.build_realtime_open(heart_rate=True, blood_oxygen=True, seq=1)
    fr = framing.parse_frame(frame, direction=framing.DIR_APP_TO_WATCH)
    from starmax_client import protobuf as pb
    d = pb.to_dict(fr.payload)
    assert d.get(4) == 1 and d.get(6) == 1   # f4 heartRate, f6 bloodOxygen
    assert d.get(5, 0) == 0                  # bloodPressure not requested


def test_realtime_measure_payload():
    frame = health.build_realtime_measure(9, seq=2)
    fr = framing.parse_frame(frame, direction=framing.DIR_APP_TO_WATCH)
    from starmax_client import protobuf as pb
    assert pb.to_dict(fr.payload)[1] == 9


# ---------------------------------------------------------------- parsers
def test_parse_health_detail():
    from starmax_client.protobuf import ProtobufWriter
    payload = (ProtobufWriter().varint(1, 0).varint(2, 5000).varint(8, 72)
               .varint(11, 98).varint(20, 45).to_bytes())
    d = health.parse_health_detail(payload)
    assert d["total_steps"] == 5000 and d["heart_rate"] == 72
    assert d["blood_oxygen"] == 98 and d["hrv"] == 45


def test_parse_history_points():
    from starmax_client.protobuf import ProtobufWriter
    pt1 = ProtobufWriter().varint(1, 8).varint(2, 30).varint(3, 72)
    pt2 = ProtobufWriter().varint(1, 8).varint(2, 45).varint(3, 75)
    payload = (ProtobufWriter().varint(1, 0).varint(2, 5)
               .varint(3, 2026).varint(4, 7).varint(5, 11).varint(6, 2)
               .message(7, pt1).message(7, pt2).to_bytes())
    d = health.parse_history(payload)
    assert d["year"] == 2026 and d["month"] == 7 and d["day"] == 11
    assert d["points"] == [{"hour": 8, "minute": 30, "value": 72},
                           {"hour": 8, "minute": 45, "value": 75}]


def test_parse_realtime_data():
    from starmax_client.protobuf import ProtobufWriter
    payload = ProtobufWriter().varint(5, 66).varint(8, 97).varint(2, 1234).to_bytes()
    d = health.parse_realtime_data(payload)
    assert d["heart_rate"] == 66 and d["blood_oxygen"] == 97 and d["steps"] == 1234


def test_parse_female_health():
    from starmax_client.protobuf import ProtobufWriter
    payload = ProtobufWriter().varint(1, 0).varint(2, 5).varint(3, 28).varint(7, 1).to_bytes()
    d = health.parse_female_health(payload)
    assert d["number_of_days"] == 5 and d["cycle_days"] == 28 and d["reminder_on"] == 1


# ---------------------------------------------------------------- registry / CLI contract
def test_commands_dict_callable():
    assert set(health.COMMANDS) >= {"health-switch-read", "history-sync", "realtime-open"}
    for name, fn in health.COMMANDS.items():
        assert callable(fn), name


def test_register_adds_subcommands_and_dry_run():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    health.register(sub, client=None)
    # --dry-run prints the exact hex frame and never touches a client
    args = parser.parse_args(["health-switch-read", "--dry-run", "--seq", "0x08"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = asyncio.run(args.func(args))
    assert rc == 0
    assert buf.getvalue().strip() == SWITCH_READ_SEQ08


def test_build_metric_history_rejects_unmapped():
    import pytest
    with pytest.raises(ValueError):
        health.build_metric_history("temp")   # still unmapped (hrv is now cat 7)
    assert health.build_metric_history("hr", seq=0x0a).hex() == HIST_CAT0_SEQ0A


# ---------------------------------------------------------------- config commands [SCHEMA]
def test_hr_config_roundtrip():
    fr = framing.parse_frame(
        health.build_hr_config(start_hour=6, end_hour=22, period=15, alarm_threshold=120, seq=1),
        direction=framing.DIR_APP_TO_WATCH)
    d = health.parse_hr_config(fr.payload)
    assert d["start_hour"] == 6 and d["end_hour"] == 22
    assert d["period"] == 15 and d["alarm_threshold"] == 120


def test_health_interval_roundtrip():
    fr = framing.parse_frame(
        health.build_health_interval(metric_type=2, measure_interval=30, store_interval=5, seq=1),
        direction=framing.DIR_APP_TO_WATCH)
    d = health.parse_health_interval(fr.payload)
    assert d == {"type": 2, "measure_interval": 30, "store_interval": 5}


def test_female_health_roundtrip():
    fr = framing.parse_frame(
        health.build_female_health(number_of_days=5, cycle_days=28, year=2026, month=7, day=11,
                                   reminder_on=True, seq=1),
        direction=framing.DIR_APP_TO_WATCH)
    d = health.parse_female_health(fr.payload)
    assert d["number_of_days"] == 5 and d["cycle_days"] == 28
    assert d["year"] == 2026 and d["reminder_on"] == 1


def test_parse_customized_hr():
    from starmax_client.protobuf import ProtobufWriter
    payload = ProtobufWriter().varint(1, 0).varint(2, 1).varint(3, 68).to_bytes()
    assert health.parse_customized_hr(payload)["heart_rate"] == 68


def test_parse_health_switch_state_category_keyed():
    from starmax_client.protobuf import ProtobufWriter
    # READ reply (spec §5): f1=1, per-category bits at f2..f7,f9. f9 -> category 7 (mirrors GB).
    payload = (ProtobufWriter().varint(1, 1).varint(2, 1).varint(3, 0).varint(4, 1)
               .varint(5, 1).varint(6, 1).varint(7, 1).varint(9, 1).to_bytes())
    state = health.parse_health_switch_state(payload)
    assert state == {0: True, 1: False, 2: True, 3: True, 4: True, 5: True, 7: True}


def test_parse_health_switch_state_write_ack_is_empty():
    from starmax_client.protobuf import ProtobufWriter
    # WRITE ack (f1=2, f2=273) carries no per-category bits -> {} (distinguishes the two replies).
    payload = ProtobufWriter().varint(1, 2).varint(2, 273).to_bytes()
    assert health.parse_health_switch_state(payload) == {}


def test_every_command_builds_valid_c1_frame():
    """Mirror B5's dry-run gate: every COMMANDS builder yields a valid C1 frame (SMOKE_ARGS
    fills required params)."""
    for name, fn in health.COMMANDS.items():
        frame = fn(**health.SMOKE_ARGS.get(name, {}))
        assert frame[0] == framing.SOF, name
        # parseable as an app->watch frame
        framing.parse_frame(frame, direction=framing.DIR_APP_TO_WATCH)
