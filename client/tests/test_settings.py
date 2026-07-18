"""Tests for the settings/reminders command group (Track B / module B2).

Byte-exact vs real captures where one exists (the reused 0x07 alarm builders); structural
round-trips (build -> parse_frame -> protobuf fields) for the schema-derived commands.
"""
import argparse
import asyncio

from starmax_client import framing, protobuf as pb
from starmax_client.commands import settings as S
from starmax_client.commands.base import Alarm
from tests import fixtures as F


def _payload(frame: bytes):
    fr = framing.parse_frame(frame)
    return fr, {f: v for f, _w, v in pb.parse(fr.payload)}


# ---------------------------------------------------------------- byte-exact vs capture
def test_alarm_reuse_is_byte_exact():
    # settings reuses base's capture-verified 0x07 builders; prove the bytes still match.
    assert S.build_alarm_get(seq=0x0d).hex() == F.ALARM_GET_SEQ0D
    assert S.build_alarm_set([Alarm(index=0, hour=0, minute=24, enabled=True)],
                             seq=0x0e).hex() == F.ALARM_SET_SEQ0E


# ---------------------------------------------------------------- user profile (0x03) [CAP shape]
def test_user_profile_bundle_shape():
    p = S.UserProfile(height_cm=175, weight_kg=70.0, birth_year=1990, sex=1,
                      step_goal=10000, distance_goal_m=6000)
    frame = S.build_user_profile(p, seq=0x03)
    fr, top = _payload(frame)
    assert fr.opcode == S.OP_SETTINGS_BUNDLE and fr.flag == 0
    assert top[1] == 2
    prof = pb.to_dict(top[2])
    assert prof[1] == 175 and prof[2] == 700 and prof[4] == 1990 and prof[5] == 1  # weight *10
    goals = pb.to_dict(top[4])
    assert goals[4] == 10000 and goals[5] == 6000
    # round-trip parser
    got = S.parse_user_profile(fr.payload)
    assert got["height_cm"] == 175 and got["weight_kg"] == 70.0
    assert got["birth_year"] == 1990 and got["step_goal"] == 10000 and got["distance_goal_m"] == 6000


# ---------------------------------------------------------------- device state [SCHEMA]
def test_device_state_omits_unset_fields():
    frame = S.build_device_state(time_format=S.TIME_24H, unit_format=S.UNIT_METRIC,
                                 wrist_raise=True, seq=1)
    fr, fields = _payload(frame)
    assert fr.opcode == S.SDK_OP_DEVICE_STATE
    assert fields == {2: 1, 3: 0, 8: 1}          # only the set fields present
    got = S.parse_device_state(fr.payload)
    assert got["time_format"] == 1 and got["unit_format"] == 0 and got["wrist_raise"] is True
    assert got["temp_format"] is None and got["language"] is None


def test_wrist_raise_convenience():
    _, fields = _payload(S.build_wrist_raise(False, seq=2))
    assert fields == {8: 0}


# ---------------------------------------------------------------- generic setting (0x22) [CAP]
def test_setting_query_and_reply():
    fr, fields = _payload(S.build_setting_query(1, seq=5))
    assert fr.opcode == S.OP_SETTING_KV and fields[1] == 1
    # reply parser against the published golden frame's payload (f1=1, f2=244).
    reply = framing.parse_frame(bytes.fromhex(F.SETTING_REPLY_SEQ82))
    assert S.parse_setting_reply(reply.payload) == {"key": 1, "value": 244}


# ---------------------------------------------------------------- reminders [SCHEMA]
def test_dnd_shape_and_parse():
    fr, fields = _payload(S.build_dnd(True, 22, 30, 7, 15, all_day=False, seq=1))
    assert fr.opcode == S.SDK_OP_DND
    assert fields == {2: 0, 3: 1, 4: 22, 5: 30, 6: 7, 7: 15}
    assert S.parse_dnd(fr.payload) == {"all_day": False, "on": True,
                                       "start": (22, 30), "end": (7, 15)}


def test_sedentary_and_drink_share_window_shape():
    fr, sed = _payload(S.build_sedentary(True, 9, 0, 18, 0, interval_min=45, seq=1))
    assert fr.opcode == S.SDK_OP_SEDENTARY
    assert sed == {2: 1, 3: 9, 4: 0, 5: 18, 6: 0, 7: 45}
    fr2, drink = _payload(S.build_drink_water(False, 8, 30, 20, 0, interval_min=90, seq=1))
    assert fr2.opcode == S.SDK_OP_DRINK
    assert drink == {2: 0, 3: 8, 4: 30, 5: 20, 6: 0, 7: 90}
    assert S.parse_interval_reminder(fr.payload)["interval_min"] == 45


def test_event_reminder_roundtrip():
    ev = S.EventReminder(2026, 12, 25, 9, 30, "Standup", repeats=[0x7F], remind_type=1)
    frame = S.build_event_reminders([ev], seq=1)
    fr = framing.parse_frame(frame)
    assert fr.opcode == S.SDK_OP_EVENT
    parsed = S.parse_event_reminders(fr.payload)
    assert parsed == [{"date": (2026, 12, 25), "time": (9, 30),
                       "content": "Standup", "remind_type": 1, "repeat_type": 0}]


def test_world_clock_packed_uint64():
    fr, fields = _payload(S.build_world_clock([1, 5, 300], seq=1))
    assert fr.opcode == S.OP_SETTING_KV
    # field 2 body is packed varints; decode them back.
    body = fields[2]
    vals, pos = [], 0
    while pos < len(body):
        v, pos = pb.decode_varint(body, pos)
        vals.append(v)
    assert vals == [1, 5, 300]


def test_aod_and_date_format_build():
    fr, aod = _payload(S.build_aod(True, style=2, start_h=8, start_m=0, end_h=22, end_m=0, seq=1))
    assert aod == {2: 2, 3: 1, 4: 8, 5: 0, 6: 22, 7: 0}
    _, df = _payload(S.build_date_format(3, seq=1))
    assert df == {2: 3}


def test_sport_goals_build():
    fr, g = _payload(S.build_sport_goals(8000, 300, 5.0, seq=1))
    assert fr.opcode == S.SDK_OP_SPORT_GOAL
    assert g == {2: 8000, 3: 300, 4: 5000}       # distance km -> m


# ---------------------------------------------------------------- registry / discovery / CLI
def test_every_command_builds_a_c1_frame():
    # Minimal-arg invocation of each pure builder produces a well-formed frame.
    samples = {
        "feature-bitmap": lambda b: b(),
        "user-profile": lambda b: b(S.UserProfile(170, 65, 1990)),
        "sport-goals": lambda b: b(8000, 300, 5.0),
        "device-state": lambda b: b(time_format=1),
        "wrist-raise": lambda b: b(True),
        "setting-query": lambda b: b(1),
        "alarm-get": lambda b: b(),
        "alarm-set": lambda b: b([Alarm(0, 7, 0)]),
        "dnd": lambda b: b(True),
        "sedentary": lambda b: b(True),
        "drink-water": lambda b: b(True),
        "event-reminders": lambda b: b([S.EventReminder(2026, 1, 1, 8, 0, "x")]),
        "world-clock": lambda b: b([1, 2]),
        "aod": lambda b: b(True),
        "date-format": lambda b: b(0),
    }
    assert set(samples) == set(S.COMMANDS), "sample coverage must match COMMANDS"
    for name, invoke in samples.items():
        frame = invoke(S.COMMANDS[name])
        assert frame[0] == framing.SOF and len(frame) >= framing.HEADER_LEN
        framing.parse_frame(frame)  # must parse without error


def test_register_adds_all_subcommands_and_dry_run(capsys):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    S.register(sub, client=None)
    # dry-run a representative command: it must print the exact hex frame and not need a client.
    args = parser.parse_args(["dnd", "--start", "23:00", "--end", "06:30", "--dry-run", "--seq", "1"])
    rc = asyncio.run(args.func(args))
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == S.build_dnd(True, 23, 0, 6, 30, seq=1).hex()


def test_module_is_discovered_by_package():
    import starmax_client.commands as pkg
    names = [m.__name__.rsplit(".", 1)[-1] for m in pkg.iter_command_modules()]
    assert "settings" in names
