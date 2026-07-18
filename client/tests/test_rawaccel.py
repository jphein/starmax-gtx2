"""Tests for the [CFW] raw-accel consumer (starmax_client/rawaccel.py + the CLI raw-accel verb).

The custom firmware does not exist yet, so every vector is SYNTHETIC — built by the module's own
firmware-emulation encoder (``build_rawaccel_data_frame`` / ``build_rawaccel_ack_frame``) to the
wire format in docs/cfw-rawaccel-protocol.md. Offline, no BLE. Covers: control-frame byte layout +
validation, the data-frame decode round-trip through the stock ``framing`` codec (CRC-bearing, no
framing change), LIS2DH12 g-conversion against the datasheet, MTU sizing, drop detection,
coexistence/reassembly, and the CLI verb (dry-run / decode / live-gate).
"""
import argparse
import asyncio
import io
from contextlib import redirect_stdout

import pytest

from starmax_client import cli, framing, protobuf as pb, rawaccel as ra
from tests import fixtures as F


# ============================================================ control frames (flag=0, protobuf)
def test_enable_frame_byte_exact():
    # Locks the enable wire format: op 0xA0, flag 0, app->watch (LEN=total, no CRC),
    # payload f1=START, f2=rate, f3=range, f4=res. seq=0x05, rate=50, range=8, res=12.
    assert ra.build_rawaccel_enable(rate_hz=50, range_g=8, res_bits=12, seq=0x05).hex() == (
        "c105010100a01300000000080110321808200c")


def test_enable_frame_structure_and_fields():
    fr = framing.parse_frame(ra.build_rawaccel_enable(rate_hz=100, range_g=16, res_bits=10, seq=1),
                             direction=framing.DIR_APP_TO_WATCH)
    assert fr.opcode == ra.OP_RAW_ACCEL and fr.flag == ra.FLAG_CONTROL
    assert fr.direction == framing.DIR_APP_TO_WATCH
    assert fr.crc_ok is None and fr.length_field == len(fr.raw)   # app->watch: no CRC
    assert pb.to_dict(fr.payload) == {1: ra.CMD_START, 2: 100, 3: 16, 4: 10}


def test_disable_frame_is_stop():
    fr = framing.parse_frame(ra.build_rawaccel_disable(seq=2), direction=framing.DIR_APP_TO_WATCH)
    assert fr.opcode == ra.OP_RAW_ACCEL and fr.flag == ra.FLAG_CONTROL
    assert pb.to_dict(fr.payload) == {1: ra.CMD_STOP}


@pytest.mark.parametrize("kw", [{"rate_hz": 37}, {"range_g": 3}, {"res_bits": 11}])
def test_enable_rejects_unsupported(kw):
    with pytest.raises(ValueError):
        ra.build_rawaccel_enable(**kw)


def test_opcode_avoids_stock_and_sdk_namespaces():
    # 0xA0 must not collide with stock (0x01-0x22), Java-SDK REV (0x31-0x3C) or realtime (0x70).
    assert ra.OP_RAW_ACCEL not in range(0x01, 0x23)
    assert ra.OP_RAW_ACCEL not in range(0x31, 0x3D)
    assert ra.OP_RAW_ACCEL != 0x70


# ============================================================ control ACK round-trip
def test_ack_frame_roundtrip_through_framing():
    frame = ra.build_rawaccel_ack_frame(status=ra.ACK_OK, rate_hz=50, range_g=8, res_bits=12,
                                        base_ts_ms=123456, seq=0x11)
    fr = framing.parse_frame(frame, direction=framing.DIR_WATCH_TO_APP)
    assert fr.opcode == ra.OP_RAW_ACCEL and fr.flag == ra.FLAG_CONTROL
    assert fr.crc_ok is True                                   # watch->app protobuf: CRC-checked
    a = ra.parse_rawaccel_ack(fr.payload)
    assert a == {"status": 0, "rate_hz": 50, "range_g": 8, "res_bits": 12, "base_ts_ms": 123456}


def test_ack_reports_clamped_values():
    a = ra.parse_rawaccel_ack(ra.build_rawaccel_ack_frame(
        status=ra.ACK_UNSUPPORTED_RATE, rate_hz=200, range_g=4, res_bits=8)[framing.HEADER_LEN:-2])
    assert a["status"] == ra.ACK_UNSUPPORTED_RATE and a["rate_hz"] == 200 and a["res_bits"] == 8


# ============================================================ data frame decode (flag=1, binary+CRC)
def test_data_frame_is_crc_bearing_not_binary_class():
    # The data frame rides the EXISTING watch->app protobuf class (CRC-bearing) with NO framing
    # change: 0xA0 flag=1 is NOT the 0x0e-flag1 binary-record special case, so parse_frame
    # CRC-verifies it and slices payload = header+samples untouched.
    frame = ra.build_rawaccel_data_frame([(1, 2, 3)], seq=0x07)
    fr = framing.parse_frame(frame, direction=framing.DIR_WATCH_TO_APP)
    assert fr.opcode == ra.OP_RAW_ACCEL and fr.flag == ra.FLAG_DATA
    assert fr.is_binary is False and fr.crc_ok is True
    assert fr.length_field == len(frame) - 2                   # LEN = total - 2 (CRC trailer)


def test_data_frame_full_decode_roundtrip():
    samples = [(16000, -16000, 0), (100, 200, 300), (-1, -2, -3)]
    frame = ra.build_rawaccel_data_frame(samples, rate_hz=100, range_g=2, res_bits=12,
                                         frame_seq=42, base_ts_ms=1000, seq=0x09)
    fr = framing.parse_frame(frame, direction=framing.DIR_WATCH_TO_APP)
    batch = ra.parse_rawaccel_frame(fr.payload)
    assert (batch.version, batch.range_g, batch.rate_hz, batch.res_bits) == (1, 2, 100, 12)
    assert batch.count == 3 and batch.frame_seq == 42 and batch.base_ts_ms == 1000
    assert [(s.x, s.y, s.z) for s in batch.samples] == samples
    # high-res +/-2g, 1 mg/digit, shift 4: 16000 -> 1.0 g, -16000 -> -1.0 g.
    assert batch.samples[0].gx == pytest.approx(1.0)
    assert batch.samples[0].gy == pytest.approx(-1.0)
    assert batch.samples[0].gz == pytest.approx(0.0)


def test_data_frame_batched_samples_within_one_pdu():
    # 36 samples is the MTU-247 single-PDU max; the frame must not exceed one 244-byte ATT PDU.
    n = ra.max_samples_per_frame(244)
    assert n == 36
    frame = ra.build_rawaccel_data_frame([(i, i, i) for i in range(n)])
    assert len(frame) <= 244                                   # fits one MTU-247 PDU, no C3
    assert framing.frame_to_pdus(frame, mtu=244) == [frame]     # single PDU
    batch = ra.parse_rawaccel_frame(
        framing.parse_frame(frame, direction=framing.DIR_WATCH_TO_APP).payload)
    assert batch.count == n


def test_max_samples_at_mtu_floor_is_zero():
    # At the 20-byte floor a batch cannot fit -> firmware must fragment or shrink (spec §4.2).
    assert ra.max_samples_per_frame(20) == 0


# ============================================================ LIS2DH12 g-conversion (datasheet)
@pytest.mark.parametrize("raw,range_g,res,expected_g", [
    (16000, 2, 12, 1.0),      # high-res +/-2g  : 1 mg/digit,  shift 4 -> 1000 digits
    (1600, 16, 12, 1.2),      # high-res +/-16g : 12 mg/digit, shift 4 -> 100 digits
    (16000, 2, 10, 1.0),      # normal  +/-2g   : 4 mg/digit,  shift 6 -> 250 digits
    (16384, 2, 8, 1.024),     # low-pwr +/-2g   : 16 mg/digit, shift 8 -> 64 digits
    (-16000, 2, 12, -1.0),    # sign preserved (arithmetic shift)
    (0, 8, 12, 0.0),
])
def test_to_g_matches_datasheet(raw, range_g, res, expected_g):
    assert ra.to_g(raw, range_g, res) == pytest.approx(expected_g)


def test_full_scale_is_physically_sensible():
    # A near-full-scale +/-2g high-res reading should sit just above 2 g, never wildly off.
    assert ra.to_g(32767, 2, 12) == pytest.approx(2.047, abs=1e-3)


@pytest.mark.parametrize("range_g", sorted(ra.RANGE_TO_CODE))
@pytest.mark.parametrize("res", sorted(ra.RES_TO_CODE))
def test_every_range_res_combo_roundtrips(range_g, res):
    frame = ra.build_rawaccel_data_frame([(1234, -5678, 4096)], range_g=range_g, res_bits=res)
    batch = ra.parse_rawaccel_frame(
        framing.parse_frame(frame, direction=framing.DIR_WATCH_TO_APP).payload)
    assert batch.range_g == range_g and batch.res_bits == res
    s = batch.samples[0]
    assert s.gx == pytest.approx(ra.to_g(1234, range_g, res))
    assert s.gz == pytest.approx(ra.to_g(4096, range_g, res))


# ============================================================ decode error handling
def test_decode_rejects_short_header():
    with pytest.raises(ra.RawAccelError):
        ra.parse_rawaccel_frame(b"\x01\x00\x00")


def test_decode_rejects_bad_version():
    payload = ra.build_rawaccel_data_frame([(1, 1, 1)], version=9)[framing.HEADER_LEN:-2]
    with pytest.raises(ra.RawAccelError):
        ra.parse_rawaccel_frame(payload)


def test_decode_rejects_truncated_batch():
    # Header claims 4 samples but only 1 sample of body is present.
    good = ra.build_rawaccel_data_frame([(1, 1, 1)], frame_seq=0)[framing.HEADER_LEN:-2]
    tampered = bytearray(good)
    tampered[3] = 4                                            # count byte -> 4
    with pytest.raises(ra.RawAccelError):
        ra.parse_rawaccel_frame(bytes(tampered))


# ============================================================ timing + drop detection
def test_timestamps_and_period():
    batch = ra.parse_rawaccel_frame(ra.build_rawaccel_data_frame(
        [(0, 0, 0)] * 4, rate_hz=50, base_ts_ms=1000)[framing.HEADER_LEN:-2])
    assert batch.sample_period_ms() == pytest.approx(20.0)     # 50 Hz -> 20 ms
    assert batch.timestamps_ms() == pytest.approx([1000, 1020, 1040, 1060])


def test_detect_drops():
    assert ra.detect_drops([]) == 0
    assert ra.detect_drops([5]) == 0
    assert ra.detect_drops([3, 4, 5]) == 0
    assert ra.detect_drops([3, 4, 6]) == 1                     # missed seq 5
    assert ra.detect_drops([10, 13]) == 2
    assert ra.detect_drops([0xFFFF, 0]) == 0                   # clean wrap
    assert ra.detect_drops([0xFFFE, 1]) == 2                   # wrap with 2 dropped


# ============================================================ codec coexistence / reassembly
def test_reassembler_single_pdu_data_frame():
    r = framing.Reassembler(direction=framing.DIR_WATCH_TO_APP)
    frame = ra.build_rawaccel_data_frame([(1, 2, 3), (4, 5, 6)], frame_seq=1)
    out = r.feed(frame)
    assert len(out) == 1 and out[0].opcode == ra.OP_RAW_ACCEL and out[0].crc_ok is True


def test_reassembler_fragmented_large_batch():
    # Not the recommended firmware path (data frames should be single-PDU), but the generic codec
    # must still rejoin a C1+C3 split of an oversized batch.
    frame = ra.build_rawaccel_data_frame([(i, -i, i * 2) for i in range(200)], frame_seq=7)
    pdus = framing.frame_to_pdus(frame, mtu=100)
    assert len(pdus) > 1
    r = framing.Reassembler(direction=framing.DIR_WATCH_TO_APP)
    out = []
    for p in pdus:
        out += r.feed(p)
    assert len(out) == 1 and out[0].raw == frame
    batch = ra.parse_rawaccel_frame(out[0].payload)
    assert batch.count == 200


def test_coexists_with_stock_c1_traffic():
    # A stock 0x22 setting reply and a raw-accel data frame interleave on the same notify pipe;
    # both decode independently (different opcode -> the consumer routes by opcode).
    r = framing.Reassembler(direction=framing.DIR_WATCH_TO_APP)
    stock = r.feed(bytes.fromhex(F.SETTING_REPLY_SEQ82))
    data = r.feed(ra.build_rawaccel_data_frame([(9, 9, 9)], frame_seq=2))
    assert len(stock) == 1 and stock[0].opcode == 0x22
    assert len(data) == 1 and data[0].opcode == ra.OP_RAW_ACCEL


# ============================================================ CLI verb (raw-accel)
def test_rawaccel_is_registered_core_verb():
    ns = cli.build_parser().parse_args(["raw-accel", "--dry-run"])
    assert ns.func is cli.cmd_rawaccel


def test_rawaccel_dry_run_prints_enable_frame():
    ns = cli.build_parser().parse_args(["raw-accel", "--dry-run", "--rate", "200", "--range", "4"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = asyncio.run(ns.func(ns))
    assert rc == 0
    fr = framing.parse_frame(bytes.fromhex(buf.getvalue().strip()),
                             direction=framing.DIR_APP_TO_WATCH)
    assert fr.opcode == ra.OP_RAW_ACCEL
    assert pb.to_dict(fr.payload) == {1: ra.CMD_START, 2: 200, 3: 4, 4: 12}


def test_rawaccel_decode_offline():
    hexframe = ra.build_rawaccel_data_frame([(16000, 0, 0)], rate_hz=50, range_g=2, res_bits=12,
                                            frame_seq=5).hex()
    ns = cli.build_parser().parse_args(["raw-accel", "--decode", hexframe])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = asyncio.run(ns.func(ns))
    out = buf.getvalue()
    assert rc == 0 and "frame_seq=5" in out and "+1.000" in out   # gx = 1.0 g


def test_rawaccel_decode_accepts_bare_payload():
    payload_hex = ra.build_rawaccel_data_frame([(1, 2, 3)])[framing.HEADER_LEN:-2].hex()
    ns = cli.build_parser().parse_args(["raw-accel", "--decode", payload_hex])
    with redirect_stdout(io.StringIO()):
        assert asyncio.run(ns.func(ns)) == 0


def test_rawaccel_live_refused_without_force():
    # No --force + not dry-run + not decode -> refuse (return 2), never connect (offline-safe).
    ns = cli.build_parser().parse_args(["raw-accel"])
    buf = io.StringIO()
    with redirect_stdout(io.StringIO()):
        rc = asyncio.run(ns.func(ns))
    assert rc == 2
