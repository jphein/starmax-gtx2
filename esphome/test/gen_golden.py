#!/usr/bin/env python3
"""Generate golden_vectors.h from the verified starmax_client builders.

Every frame the C++ ``gtx2_protocol`` layer builds must be BYTE-IDENTICAL to the Python
reference in ``starmax-client`` (the same guarantee crown_ble.c makes vs crown.py). This script
runs the Python builders with fixed, PII-free inputs and emits the expected bytes as a C header
that ``test_gtx2_protocol.cpp`` asserts against. The C++ test hardcodes the SAME inputs.

Run from anywhere:  python3 gen_golden.py > golden_vectors.h
"""
from __future__ import annotations

import datetime as _dt
import os
import struct
import sys

# Locate the starmax-client package (../../.. /starmax-client from this file).
_HERE = os.path.dirname(os.path.abspath(__file__))
_SMARTWATCH = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(_SMARTWATCH, "starmax-client"))

from starmax_client import framing, records  # noqa: E402
from starmax_client.commands import base, files, dials  # noqa: E402
from starmax_client.commands.base import Alarm, Weather  # noqa: E402
from starmax_client.commands.settings import build_feature_bitmap  # noqa: E402
from starmax_client.protobuf import ProtobufWriter  # noqa: E402

# ---- fixed, deterministic inputs (mirrored verbatim in the C++ test) ----
SEQ = 7
# A fixed tz-aware instant: 2026-07-14 09:30:15 at UTC-07:00 (PDT). The C++ test passes the
# component-computed fields (below) so set-time parity does not depend on the host clock/tz.
DT = _dt.datetime(2026, 7, 14, 9, 30, 15, tzinfo=_dt.timezone(_dt.timedelta(hours=-7)))
WEATHER = Weather(city="Anytown", month=7, day=14, hour=9, minute=30,
                  condition=6, temp_current=21, temp_max=27, temp_min=15,
                  pressure_hpa=1013.25)
ALARM = Alarm(index=0, hour=7, minute=30, enabled=True)
DIAL_ID = 25001
HEALTH_CAT = 5
# dial-push (D-plane) fixture: a deterministic 500-byte "container" (mirrored in the C++ test).
DIAL_PUSH_ID = 25022
DIAL_BLOB = bytes(((i * 7 + 3) & 0xFF) for i in range(500))


def _watch_frame(opcode: int, payload: bytes, *, flag: int = 0, seq: int = SEQ) -> bytes:
    """Craft a watch->app frame (dir=0x00, LEN=total-2, CRC-16/CCITT-FALSE LE tail) — the shape
    the node RECEIVES. Mirrors framing's watch->app rule so the C++ parser/CRC is tested too."""
    len_field = framing.HEADER_LEN + len(payload)
    header = bytes([framing.SOF, (seq & 0xFF) | framing.SEQ_HIGH_BIT, framing.DIR_WATCH_TO_APP,
                    framing.PROTO_VER, flag & 0xFF, opcode & 0xFF,
                    len_field & 0xFF, (len_field >> 8) & 0xFF, 0, 0, 0])
    frame = header + payload
    crc = framing.crc16_ccitt_false(frame)
    return frame + struct.pack("<H", crc)


def _carr(name: str, data: bytes) -> str:
    body = ", ".join(f"0x{b:02x}" for b in data)
    return f"static const uint8_t GV_{name}[] = {{{body}}};"


VECS: list[tuple[str, bytes]] = [
    ("bind", base.build_bind(seq=SEQ)),
    ("find_on", base.build_find_device(True, seq=SEQ)),
    ("find_off", base.build_find_device(False, seq=SEQ)),
    ("set_time", base.build_set_time(DT, seq=SEQ)),
    ("feature_bitmap", build_feature_bitmap(seq=SEQ)),
    ("weather", base.build_weather(WEATHER, seq=SEQ)),
    ("alarm_set", base.build_alarm_set([ALARM], seq=SEQ)),
    ("alarm_get", base.build_alarm_get(seq=SEQ)),
    ("state_query", base.build_state_query(seq=SEQ)),
    ("dial_list_req", files.build_dial_list_request(seq=SEQ)),
    ("dial_switch", files.build_dial_switch(DIAL_ID, seq=SEQ)),
    # [FW] dial-delete: 0x16 operate {f1=DELETE(2), f2=dial_name} — delete a custom face by filename
    ("dial_delete", files.build_dial_delete(dials.dial_wire_filename(DIAL_PUSH_ID), seq=SEQ)),
    ("health_sync", base.build_health_sync(HEALTH_CAT, seq=SEQ)),
    # bulk-plane dial-push: the full D3->D1->D2*->D4 byte stream (write-with-response paced on-wire).
    ("dial_plan", b"".join(dials.plan_dial_push(DIAL_BLOB, DIAL_PUSH_ID))),
    # watch->app inbound frames the node must parse + route (CRC-checked):
    ("in_music_play", _watch_frame(0x10, bytes([0x08, 0x01]))),
    ("in_music_prev", _watch_frame(0x10, bytes([0x08, 0x01, 0x10, 0x02]))),
    ("in_music_next", _watch_frame(0x10, bytes([0x08, 0x01, 0x10, 0x03]))),
    ("in_find_phone", _watch_frame(0x10, bytes([0x08, 0x02]))),
    ("in_find_phone4", _watch_frame(0x10, bytes([0x08, 0x04]))),
    ("in_records", _watch_frame(0x10, bytes([0x08, 0x03, 0x10, 0x05]))),
]

# ---- synthetic health records (both sides decode the SAME bytes) ----
_DATE = bytes([0xEA, 0x07, 0x07, 0x0E])  # 2026-07-14 date marker (yr u16LE, mo, dy)


def _u32(v: int) -> bytes:
    return v.to_bytes(4, "little")


# cat-5 activity (shape B, flag 0x04): date @off12, then 2-byte head + u32[steps,?,active,cal,dist,?]
_ACT_DATA = (bytes([0x00, 0x00]) + _u32(162) + _u32(0) + _u32(50) + _u32(397) + _u32(115) + _u32(0))
REC_ACTIVITY = bytes([0x04, 0x00, 0x10, 0x05]) + bytes(8) + _DATE + _ACT_DATA
_act = records.extract_activity(REC_ACTIVITY)
assert _act is not None, "activity fixture must decode"

# cat-0 HR (shape A, flag 0x02): date @off10, then a 0xff-run tail + bpm bytes
_HR_DATA = bytes([0xFF, 0xFF, 72, 80, 75])
REC_HR = bytes([0x02, 0x00, 0x20]) + bytes(7) + _DATE + _HR_DATA
_hr = records.extract_heart_rates(REC_HR[10 + 4:])  # data region = payload past date @off10
_hr_last = _hr[-1] if _hr else 0

# cat-2 SpO2 (shape B, flag 0x04): date @off12, then `02 00 <u32 nsamp>` + pct bytes
_SPO2_DATA = bytes([0x02, 0x00]) + _u32(3) + bytes([98, 97, 96])
REC_SPO2 = bytes([0x04, 0x00, 0x10, 0x02]) + bytes(8) + _DATE + _SPO2_DATA
_spo2 = records.extract_spo2(REC_SPO2[12 + 4:])
_spo2_last = _spo2[-1] if _spo2 else 0

# cat-0/2/5 records decode; now two protobuf REPLIES the node parses for text sensors.
# 0x05 device-state reply: f3 = MAC(6) + six u16LE words (firmware build stamp). PII: MAC is a
# placeholder; parse_state_reply drops it from what we surface (we keep only the stamp).
_STATE_F3 = bytes([0x00, 0x11, 0x22, 0x33, 0x44, 0x55]) + b"".join(
    struct.pack("<H", w) for w in (2026, 7, 14, 9, 30, 15))
REC_STATE = (ProtobufWriter().varint(1, 1).varint(2, 2).bytes(3, _STATE_F3).to_bytes())
_state = base.parse_state_reply(REC_STATE)
_fw_stamp = _state["firmware_build_stamp"]

# 0x16 dial-list reply: top-level f14 = active-face filename (ascii). One entry (f10) too.
_ACTIVE = "custom_id_25001.bin"
_entry = (ProtobufWriter().varint(1, 1).varint(2, 3).varint(3, 231424)
          .string(4, "watchface_a.bin").to_bytes())
REC_DIALLIST = (ProtobufWriter().message(10, _entry).string(14, _ACTIVE)
                .varint(11, 4194304).varint(12, 462848).to_bytes())
_dl = files.parse_dial_list_reply(REC_DIALLIST)
assert _dl["active_dial"] == _ACTIVE, _dl

# set-time field breakdown the C++ side recomputes from its own time source:
_epoch = int(DT.timestamp())
_tz = base._tz_field(DT)

print("// AUTO-GENERATED by gen_golden.py from the verified starmax_client builders.")
print("// Do NOT edit by hand — regenerate:  python3 gen_golden.py > golden_vectors.h")
print("#pragma once")
print("#include <stdint.h>")
print("#include <stddef.h>")
print()
print(f"#define GV_SEQ {SEQ}")
print("// set-time inputs (the node supplies these from its time component):")
print(f"#define GV_ST_YEAR {DT.year}")
print(f"#define GV_ST_MONTH {DT.month}")
print(f"#define GV_ST_DAY {DT.day}")
print(f"#define GV_ST_HOUR {DT.hour}")
print(f"#define GV_ST_MIN {DT.minute}")
print(f"#define GV_ST_SEC {DT.second}")
print(f"#define GV_ST_WDAY {DT.weekday()}")
print(f"#define GV_ST_EPOCH {_epoch}ULL")
print(f"#define GV_ST_TZ {_tz}")
print("// weather inputs:")
print(f"#define GV_W_MONTH {WEATHER.month}")
print(f"#define GV_W_DAY {WEATHER.day}")
print(f"#define GV_W_HOUR {WEATHER.hour}")
print(f"#define GV_W_MINUTE {WEATHER.minute}")
print(f"#define GV_W_COND {WEATHER.condition}")
print(f"#define GV_W_CUR {WEATHER.temp_current}")
print(f"#define GV_W_TMAX {WEATHER.temp_max}")
print(f"#define GV_W_TMIN {WEATHER.temp_min}")
print(f'#define GV_W_CITY "{WEATHER.city}"')
print(f"#define GV_W_PRESSURE_CPA {int(round(WEATHER.pressure_hpa * 100))}  // hPa*100")
print(f"#define GV_AL_HOUR {ALARM.hour}")
print(f"#define GV_AL_MIN {ALARM.minute}")
print(f"#define GV_DIAL_ID {DIAL_ID}")
print(f'#define GV_DIAL_DELETE_NAME "{dials.dial_wire_filename(DIAL_PUSH_ID)}"')
print(f"#define GV_HEALTH_CAT {HEALTH_CAT}")
print("// health-decode expectations (both sides decode the same synthetic records):")
print(f"#define GV_ACT_STEPS {_act.steps}")
print(f"#define GV_ACT_DIST {_act.distance_m}")
print(f"#define GV_ACT_CAL {_act.calories}")
print(f"#define GV_HR_LAST {_hr_last}")
print(f"#define GV_SPO2_LAST {_spo2_last}")
print(f'#define GV_FW_STAMP "{_fw_stamp}"')
print(f'#define GV_ACTIVE_DIAL "{_ACTIVE}"')
print()
print(_carr("rec_activity", REC_ACTIVITY))
print(_carr("rec_hr", REC_HR))
print(_carr("rec_spo2", REC_SPO2))
print(_carr("rec_state", REC_STATE))
print(_carr("rec_diallist", REC_DIALLIST))
print()
for name, data in VECS:
    print(_carr(name, data))
print()
print("struct GoldenVec { const char *name; const uint8_t *bytes; size_t len; };")
print("static const GoldenVec GOLDEN[] = {")
for name, _ in VECS:
    print(f'  {{"{name}", GV_{name}, sizeof(GV_{name})}},')
print("};")
print(f"static const size_t GOLDEN_N = {len(VECS)};")
