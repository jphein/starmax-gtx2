"""Tests for the [CFW] rotary-crown consumer (starmax_client/crown.py + the CLI crown verb).

The custom firmware does not exist yet, so every vector is SYNTHETIC — built by the module's own
firmware-emulation encoder (``build_crown_data_frame`` / ``build_crown_ack_frame``) to the wire
format in docs/cfw-crown-protocol.md. Offline, no BLE. Covers: control-frame byte layout +
validation, the data-frame decode round-trip through the stock ``framing`` codec (CRC-bearing, no
framing change), rotation + every button action, MTU sizing, drop detection, coexistence/reassembly
(incl. alongside the raw-accel 0xA0 stream), and the CLI verb (dry-run / decode / live-gate).
"""
import argparse
import asyncio
import io
from contextlib import redirect_stdout

import pytest

from starmax_client import cli, framing, protobuf as pb, crown as cr, rawaccel as ra
from tests import fixtures as F


# ============================================================ control frames (flag=0, protobuf)
def test_enable_frame_byte_exact():
    # Locks the enable wire format: op 0xA1, flag 0, app->watch (LEN=total, no CRC),
    # payload f1=START, f2=report_rotation, f3=report_button, f4=coalesce_ms. seq=0x05, all defaults.
    assert cr.build_crown_enable(seq=0x05).hex() == "c105010100a113000000000801100118012000"


def test_enable_frame_structure_and_fields():
    fr = framing.parse_frame(cr.build_crown_enable(report_rotation=True, report_button=False,
                                                   coalesce_ms=250, seq=1),
                             direction=framing.DIR_APP_TO_WATCH)
    assert fr.opcode == cr.OP_CROWN and fr.flag == cr.FLAG_CONTROL
    assert fr.direction == framing.DIR_APP_TO_WATCH
    assert fr.crc_ok is None and fr.length_field == len(fr.raw)   # app->watch: no CRC
    assert pb.to_dict(fr.payload) == {1: cr.CMD_START, 2: 1, 3: 0, 4: 250}


def test_disable_frame_is_stop():
    fr = framing.parse_frame(cr.build_crown_disable(seq=2), direction=framing.DIR_APP_TO_WATCH)
    assert fr.opcode == cr.OP_CROWN and fr.flag == cr.FLAG_CONTROL
    assert pb.to_dict(fr.payload) == {1: cr.CMD_STOP}


def test_enable_rejects_negative_coalesce():
    with pytest.raises(ValueError):
        cr.build_crown_enable(coalesce_ms=-1)


def test_opcode_avoids_stock_sdk_and_rawaccel_namespaces():
    # 0xA1 must not collide with stock (0x01-0x22), Java-SDK REV (0x31-0x3C), realtime (0x70)
    # or the raw-accel extension (0xA0).
    assert cr.OP_CROWN not in range(0x01, 0x23)
    assert cr.OP_CROWN not in range(0x31, 0x3D)
    assert cr.OP_CROWN != 0x70
    assert cr.OP_CROWN != ra.OP_RAW_ACCEL and cr.OP_CROWN == 0xA1


# ============================================================ control ACK round-trip
def test_ack_frame_roundtrip_through_framing():
    frame = cr.build_crown_ack_frame(status=cr.ACK_OK, report_rotation=1, report_button=1,
                                     detents_per_rev=30, base_ts_ms=123456, seq=0x11)
    fr = framing.parse_frame(frame, direction=framing.DIR_WATCH_TO_APP)
    assert fr.opcode == cr.OP_CROWN and fr.flag == cr.FLAG_CONTROL
    assert fr.crc_ok is True                                   # watch->app protobuf: CRC-checked
    a = cr.parse_crown_ack(fr.payload)
    assert a == {"status": 0, "report_rotation": 1, "report_button": 1,
                 "detents_per_rev": 30, "base_ts_ms": 123456}


def test_ack_reports_no_crown():
    a = cr.parse_crown_ack(cr.build_crown_ack_frame(
        status=cr.ACK_NO_CROWN, report_rotation=0, report_button=0)[framing.HEADER_LEN:-2])
    assert a["status"] == cr.ACK_NO_CROWN and a["report_rotation"] == 0


# ============================================================ data frame decode (flag=1, binary+CRC)
def test_data_frame_is_crc_bearing_not_binary_class():
    # The data frame rides the EXISTING watch->app protobuf class (CRC-bearing) with NO framing
    # change: 0xA1 flag=1 is NOT the 0x0e-flag1 binary-record special case, so parse_frame
    # CRC-verifies it and slices payload = header+events untouched.
    frame = cr.build_crown_data_frame([cr.rotate(1)], seq=0x07)
    fr = framing.parse_frame(frame, direction=framing.DIR_WATCH_TO_APP)
    assert fr.opcode == cr.OP_CROWN and fr.flag == cr.FLAG_DATA
    assert fr.is_binary is False and fr.crc_ok is True
    assert fr.length_field == len(frame) - 2                   # LEN = total - 2 (CRC trailer)


def test_rotation_delta_decode_roundtrip():
    events = [cr.rotate(3), cr.rotate(-2), cr.rotate(1)]
    frame = cr.build_crown_data_frame(events, frame_seq=42, base_ts_ms=1000, seq=0x09)
    fr = framing.parse_frame(frame, direction=framing.DIR_WATCH_TO_APP)
    batch = cr.parse_crown_frame(fr.payload)
    assert (batch.version, batch.count, batch.frame_seq, batch.base_ts_ms) == (1, 3, 42, 1000)
    assert batch.clockwise_positive is True
    assert [e.rotation_delta for e in batch.events] == [3, -2, 1]
    assert all(e.is_rotation and not e.is_button for e in batch.events)
    assert batch.net_rotation() == 2                            # 3 - 2 + 1


@pytest.mark.parametrize("action,name", [
    (cr.BTN_DOWN, "down"), (cr.BTN_UP, "up"), (cr.BTN_CLICK, "click"),
    (cr.BTN_LONG, "long"), (cr.BTN_DOUBLE, "double"), (cr.BTN_LONG_UP, "long_up"),
])
def test_every_button_action_decodes(action, name):
    frame = cr.build_crown_data_frame([cr.button(action)], frame_seq=1)
    batch = cr.parse_crown_frame(
        framing.parse_frame(frame, direction=framing.DIR_WATCH_TO_APP).payload)
    ev = batch.events[0]
    assert ev.is_button and not ev.is_rotation
    assert ev.button_action == name and ev.rotation_delta == 0
    assert batch.net_rotation() == 0                            # buttons never move the dimmer


def test_mixed_rotation_and_button_batch():
    events = [cr.rotate(2), cr.button(cr.BTN_CLICK), cr.rotate(-1)]
    batch = cr.parse_crown_frame(cr.build_crown_data_frame(events)[framing.HEADER_LEN:-2])
    assert [(e.ev_type, e.ev_detail, e.value) for e in batch.events] == list(events)
    assert batch.net_rotation() == 1
    assert [e.button_action for e in batch.button_events()] == ["click"]


def test_build_button_rejects_unknown_action():
    with pytest.raises(ValueError):
        cr.button(99)


def test_counter_clockwise_convention_flag():
    frame = cr.build_crown_data_frame([cr.rotate(1)], clockwise_positive=False)
    batch = cr.parse_crown_frame(
        framing.parse_frame(frame, direction=framing.DIR_WATCH_TO_APP).payload)
    assert batch.clockwise_positive is False


def test_data_frame_single_pdu_max():
    # 54 events is the MTU-247 single-PDU max; a real crown frame is far smaller.
    n = cr.max_events_per_frame(244)
    assert n == 54
    frame = cr.build_crown_data_frame([cr.rotate(1)] * n)
    assert len(frame) <= 244                                    # fits one MTU-247 PDU, no C3
    assert framing.frame_to_pdus(frame, mtu=244) == [frame]     # single PDU
    batch = cr.parse_crown_frame(
        framing.parse_frame(frame, direction=framing.DIR_WATCH_TO_APP).payload)
    assert batch.count == n


def test_max_events_at_mtu_floor_is_zero():
    assert cr.max_events_per_frame(20) == 0


# ============================================================ decode error handling
def test_decode_rejects_short_header():
    with pytest.raises(cr.CrownError):
        cr.parse_crown_frame(b"\x01\x00\x00")


def test_decode_rejects_bad_version():
    payload = cr.build_crown_data_frame([cr.rotate(1)], version=9)[framing.HEADER_LEN:-2]
    with pytest.raises(cr.CrownError):
        cr.parse_crown_frame(payload)


def test_decode_rejects_truncated_batch():
    # Header claims 4 events but only 1 event of body is present.
    good = bytearray(cr.build_crown_data_frame([cr.rotate(1)])[framing.HEADER_LEN:-2])
    good[3] = 4                                                 # count byte -> 4
    with pytest.raises(cr.CrownError):
        cr.parse_crown_frame(bytes(good))


# ============================================================ drop detection
def test_detect_drops():
    assert cr.detect_drops([]) == 0
    assert cr.detect_drops([5]) == 0
    assert cr.detect_drops([3, 4, 5]) == 0
    assert cr.detect_drops([3, 4, 6]) == 1                      # missed seq 5
    assert cr.detect_drops([10, 13]) == 2
    assert cr.detect_drops([0xFFFF, 0]) == 0                    # clean wrap
    assert cr.detect_drops([0xFFFE, 1]) == 2                    # wrap with 2 dropped


# ============================================================ codec coexistence / reassembly
def test_reassembler_single_pdu_data_frame():
    r = framing.Reassembler(direction=framing.DIR_WATCH_TO_APP)
    frame = cr.build_crown_data_frame([cr.rotate(1), cr.button(cr.BTN_CLICK)], frame_seq=1)
    out = r.feed(frame)
    assert len(out) == 1 and out[0].opcode == cr.OP_CROWN and out[0].crc_ok is True


def test_coexists_with_stock_and_rawaccel_traffic():
    # Stock 0x22 reply, a raw-accel 0xA0 data frame and a crown 0xA1 data frame all interleave on
    # the same notify pipe; each decodes independently (the consumer routes by opcode).
    r = framing.Reassembler(direction=framing.DIR_WATCH_TO_APP)
    stock = r.feed(bytes.fromhex(F.SETTING_REPLY_SEQ82))
    accel = r.feed(ra.build_rawaccel_data_frame([(9, 9, 9)], frame_seq=2))
    crownd = r.feed(cr.build_crown_data_frame([cr.rotate(4)], frame_seq=3))
    assert len(stock) == 1 and stock[0].opcode == 0x22
    assert len(accel) == 1 and accel[0].opcode == ra.OP_RAW_ACCEL
    assert len(crownd) == 1 and crownd[0].opcode == cr.OP_CROWN
    assert cr.parse_crown_frame(crownd[0].payload).net_rotation() == 4


# ============================================================ CLI verb (crown)
def test_crown_is_registered_core_verb():
    ns = cli.build_parser().parse_args(["crown", "--dry-run"])
    assert ns.func is cli.cmd_crown


def test_crown_dry_run_prints_enable_frame():
    ns = cli.build_parser().parse_args(["crown", "--dry-run", "--coalesce", "100", "--no-button"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = asyncio.run(ns.func(ns))
    assert rc == 0
    fr = framing.parse_frame(bytes.fromhex(buf.getvalue().strip()),
                             direction=framing.DIR_APP_TO_WATCH)
    assert fr.opcode == cr.OP_CROWN
    assert pb.to_dict(fr.payload) == {1: cr.CMD_START, 2: 1, 3: 0, 4: 100}


def test_crown_decode_offline():
    hexframe = cr.build_crown_data_frame(
        [cr.rotate(5), cr.button(cr.BTN_LONG)], frame_seq=7).hex()
    ns = cli.build_parser().parse_args(["crown", "--decode", hexframe])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = asyncio.run(ns.func(ns))
    out = buf.getvalue()
    assert rc == 0 and "frame_seq=7" in out and "rotate +5" in out and "button long" in out


def test_crown_decode_accepts_bare_payload():
    payload_hex = cr.build_crown_data_frame([cr.rotate(1)])[framing.HEADER_LEN:-2].hex()
    ns = cli.build_parser().parse_args(["crown", "--decode", payload_hex])
    with redirect_stdout(io.StringIO()):
        assert asyncio.run(ns.func(ns)) == 0


def test_crown_live_refused_without_force():
    # No --force + not dry-run + not decode -> refuse (return 2), never connect (offline-safe).
    ns = cli.build_parser().parse_args(["crown"])
    with redirect_stdout(io.StringIO()):
        rc = asyncio.run(ns.func(ns))
    assert rc == 2
