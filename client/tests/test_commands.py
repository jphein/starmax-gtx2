"""Command-builder tests: byte-exact reproduction of real captured frames."""
import datetime as dt

from starmax_client import commands as C
from starmax_client import framing, protobuf as pb
from starmax_client.records import parse_health_record_header
from tests import fixtures as F


# ---------------------------------------------------------------- byte-exact vs capture
def test_bind_matches_capture():
    assert C.build_bind(seq=0x01).hex() == F.BIND_SEQ01


def _set_time_fields(when):
    """Decode the set-time sub-message into {field: value}."""
    fr = framing.parse_frame(C.build_set_time(when, seq=0x07), direction=framing.DIR_APP_TO_WATCH)
    return pb.to_dict(pb.to_dict(fr.payload)[2])


def test_set_time_pdt_matches_capture_except_derived_tz():
    # The captured instant: local 2026-07-11 00:08:12 at UTC-7. All time fields still reproduce the
    # capture; f9 is now DERIVED (G5, ported from GB) rather than the old hardcoded 1140 constant:
    # -420 min wraps to 1020. (f9 units are UNRESOLVED §10.7; the watch uses f8+f4-6, so low-impact.)
    tz = dt.timezone(dt.timedelta(hours=-7))
    when = dt.datetime(2026, 7, 11, 0, 8, 12, tzinfo=tz)
    t = _set_time_fields(when)
    assert (t[1], t[2], t[3], t[4], t[5], t[6], t[7]) == (2026, 7, 11, 0, 8, 12, 5)  # Sat=5
    assert t[8] == int(when.timestamp())   # epoch still capture-faithful
    assert t[9] == 1020                    # derived tz (was the 1140 capture constant)


def test_set_time_tz_derivation_utc_and_offsets():
    def f9(hours):
        w = dt.datetime(2026, 7, 11, 12, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=hours)))
        return _set_time_fields(w)[9]
    assert f9(0) == 0        # UTC
    assert f9(-7) == 1020    # PDT (supersedes the old 1140 constant)
    assert f9(8) == 480      # +8h
    assert f9(-12) == 720    # -12h wraps non-negative


def test_set_time_requires_aware_datetime():
    import pytest
    with pytest.raises(ValueError):
        C.build_set_time(dt.datetime(2026, 7, 11, 0, 0, 0), seq=1)


# ---------------------------------------------------------------- 0x05 device state (§3.3)
def test_state_query_byte_exact():
    # Byte-exact vs the GB coordinator's buildStateQuery (StarmaxProtocolTest) and the query the
    # activation handshake sends: f1=1,f2=0,f3=0 -> payload 08 01 10 00 18 00.
    assert C.build_state_query(seq=3).hex() == "c103010100051100000000080110001800"


def test_parse_state_reply_mac_and_build_stamp():
    # 0x05 reply: f1=1, f2=2, f3 = MAC(6) + six u16LE = 2024-07-24 19:49:39. Synthetic MAC
    # (aa..ff); the stamp is a firmware build date, not personal data. Mirrors GB parseDeviceState.
    payload = bytes.fromhex("080110021a12aabbccddeeffe80707001800130031002700")
    state = C.parse_state_reply(payload)
    assert state["mac"] == "aa:bb:cc:dd:ee:ff"
    assert state["firmware_build_stamp"] == "2024-07-24 19:49:39"


def test_parse_state_reply_empty_end_marker_is_null():
    # The trailing empty 0x05 frame (f1=1, f2=2, no f3) yields both None (mirrors GB nullable state).
    payload = pb.ProtobufWriter().varint(1, 1).varint(2, 2).to_bytes()
    state = C.parse_state_reply(payload)
    assert state == {"mac": None, "firmware_build_stamp": None}


def test_find_device_on_off_match_capture():
    assert C.build_find_device(True, seq=0x0b).hex() == F.FIND_ON_SEQ0B
    assert C.build_find_device(False, seq=0x0c).hex() == F.FIND_OFF_SEQ0C


def test_alarm_get_matches_capture():
    assert C.build_alarm_get(seq=0x0d).hex() == F.ALARM_GET_SEQ0D


def test_alarm_set_matches_capture():
    frame = C.build_alarm_set([C.Alarm(index=0, hour=0, minute=24, enabled=True)], seq=0x0e)
    assert frame.hex() == F.ALARM_SET_SEQ0E


def test_health_sync_matches_capture():
    assert C.build_health_sync(0, seq=0x0a).hex() == F.HEALTH_CAT0_SEQ0A
    assert C.build_health_sync(5, seq=0x12).hex() == F.HEALTH_CAT5_SEQ12


def test_health_sync_read_status_subop():
    # read-status is subop=1 (f1=1); still flag=1, no CRC.
    frame = C.build_health_sync(0, subop=C.SUBOP_READ_STATUS, seq=0x0b)
    assert frame.hex() == "c10b0101010e1100000000080110001800"


# ---------------------------------------------------------------- structural (PII-free)
def test_notification_detailed_structure():
    # Uses synthetic text; asserts the numeric template + flag + text placement (f6).
    frame = C.build_notification_detailed("Hello", "World", seq=0x2d)
    fr = framing.parse_frame(frame)
    assert fr.opcode == C.OP_NOTIFY_DETAILED and fr.flag == 0
    fields = {f: v for f, _w, v in pb.parse(fr.payload)}
    assert fields[1] == 1 and fields[2] == 2 and fields[3] == 6
    assert fields[4] == 100 and fields[5] == 0
    assert fields[6] == b"Hello" and fields[7] == b"World"


def test_notification_summary_structure():
    frame = C.build_notification_summary("3 messages", seq=0x44)
    fr = framing.parse_frame(frame)
    assert fr.opcode == C.OP_NOTIFY_SUMMARY and fr.flag == 1  # summary uses flag=1
    fields = {f: v for f, _w, v in pb.parse(fr.payload)}
    assert fields[1] == 2 and fields[2] == 0 and fields[3] == 2 and fields[4] == 34
    assert fields[5] == b"3 messages"


def test_alarm_with_weekdays():
    days = bytes([1, 1, 1, 1, 1, 0, 0])  # weekdays only
    frame = C.build_alarm_set([C.Alarm(index=1, hour=7, minute=30, weekdays=days)], seq=1)
    fr = framing.parse_frame(frame)
    entry = pb.get(fr.payload, 3)
    ef = {f: v for f, _w, v in pb.parse(entry)}
    assert ef[1] == 1 and ef[4] == 7 and ef[5] == 30 and ef[7] == days


def test_weather_frames_and_roundtrips():
    w = C.Weather(city="Anytown", month=7, day=11, hour=0, minute=8, condition=6,
                  temp_current=31, temp_max=33, temp_min=22,
                  hourly=[(31, 21)] * 24, daily=[(33, 22, 6)], pressure_hpa=1015.0)
    frame = C.build_weather(w, seq=0x20)
    fr = framing.parse_frame(frame)
    assert fr.opcode == C.OP_WEATHER
    top = pb.to_dict(fr.payload)
    fc = pb.to_dict(top[3])
    assert fc[10].decode() == "Anytown"
    assert fc[6] == 31 and fc[22] == 101500
    assert len(list(pb.iter_fields(top[3], 11))) == 24  # 24 hourly entries


# ---------------------------------------------------------------- health record header
def test_health_record_header_shape_b():
    # Shape B: 04 00 | 10 <cat> | ... | <yr u16LE><mo><dy> | data
    rec = bytes.fromhex("040010022001000000000000") + bytes([0xea, 0x07, 0x07, 0x0b]) + b"\x5a\x5b"
    h = parse_health_record_header(rec)
    assert h.present is True
    assert h.category == 2
    assert (h.year, h.month, h.day) == (2026, 7, 11)


def test_health_record_header_empty():
    h = parse_health_record_header(b"")
    assert h.present is False and h.year is None
