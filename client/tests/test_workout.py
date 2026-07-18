"""Tests for the workout (cat 4) KLV parser.

Committed vectors are SYNTHETIC KLV only. A structure-only test reads the gitignored real fixture
if present and asserts ONLY structure (framing, which keys exist), never real biometric/GPS values.
"""
from __future__ import annotations

import os
import struct

import pytest

from starmax_client import workout
from starmax_client.workout import HEAD_PREAMBLE_LEN, parse_workout_klv


def _block(key: int, value: bytes) -> bytes:
    return bytes([key]) + struct.pack("<I", len(value)) + value          # [key:u8][len:u32 LE][value]


def _pt(lat_e7: int, lng_e7: int) -> bytes:
    return lat_e7.to_bytes(4, "little", signed=True) + lng_e7.to_bytes(4, "little", signed=True)


def _record(*blocks: bytes, preamble: bytes = b"\x00" * HEAD_PREAMBLE_LEN) -> bytes:
    """A synthetic cat-4 record: shape-B header + date@12, then head preamble + KLV blocks."""
    # 04 00 10 04 (shape B, cat 4) + 8 filler -> date@12 = ea 07 07 0c (2026-07-12); data starts @16
    header = bytes([0x04, 0x00, 0x10, 0x04]) + b"\x00" * 8 + bytes([0xEA, 0x07, 0x07, 0x0C])
    return header + preamble + b"".join(blocks)


def test_splits_head_preamble_and_keyed_blocks():
    rec = _record(_block(1, b"\x54\x55\x56\x57"),          # hr, 4 bytes
                  _block(3, b"\x01\x02"),                   # step, 2 bytes
                  _block(0, b""))                           # zero-len head key -> clean padding stop
    w = parse_workout_klv(rec)
    assert len(w.head_preamble) == HEAD_PREAMBLE_LEN
    assert set(w.blocks) == {1, 3}
    assert w.blocks[1] == b"\x54\x55\x56\x57" and w.blocks[3] == b"\x01\x02"
    assert w.clean is True and w.trail == []


def test_trail_key6_decodes_via_gpstrack():
    rec = _record(_block(1, b"\x54\x55"),
                  _block(6, _pt(377749000, -1224194000) + _pt(0, 0)))     # 1 real + 1 null point
    w = parse_workout_klv(rec)
    assert 6 in w.blocks
    assert len(w.trail) == 1                                # (0,0) padding point dropped
    assert abs(w.trail[0].lat - 37.7749) < 1e-9 and w.trail[0].lng < 0


def test_stops_cleanly_on_anomaly_without_crashing():
    # an unknown key (99) after a valid block -> stop, keep what parsed + remainder, clean=False
    rec = _record(_block(1, b"\x54\x55"), bytes([99, 0xFF, 0xFF, 0xFF, 0xFF]) + b"junk")
    w = parse_workout_klv(rec)
    assert w.blocks == {1: b"\x54\x55"} and w.clean is False and w.remainder.startswith(b"\x63")


def test_no_trail_yields_empty():
    w = parse_workout_klv(_record(_block(1, b"\x54\x55"), _block(3, b"\x01")))
    assert 6 not in w.blocks and w.trail == []


def test_block_keys_labels():
    w = parse_workout_klv(_record(_block(1, b"\x54"), _block(3, b"\x01")))
    assert w.block_keys == ["hr", "step"]


# --------------------------------------------------------------------------- real fixture (structure only)
FIXTURE = os.path.join(os.path.dirname(__file__), "..", "..",
                       "scratch", "full-impl", "fixtures", "cat4_workout_real.bin")


@pytest.mark.skipif(not os.path.isfile(FIXTURE), reason="real workout fixture not present")
def test_real_fixture_structure_only():
    """Assert STRUCTURE of the real record only — never real biometric/GPS values (PII)."""
    w = parse_workout_klv(open(FIXTURE, "rb").read())
    assert len(w.head_preamble) == HEAD_PREAMBLE_LEN
    assert 1 in w.blocks, "HR block (key=1) should be present"
    assert 3 in w.blocks, "step block (key=3) should be present"
    assert 6 not in w.blocks, "no GPS trail (GPS never locked on this record)"
    assert w.trail == []
    # the HR block holds plausible-bpm bytes (range check only — do NOT assert/expose the values)
    assert w.blocks[1] and all(40 <= b <= 200 for b in w.blocks[1])
    # SportHead decodes to SANE RANGES (never assert/commit the real values — PII)
    s = w.head
    assert s is not None
    assert s.start_time is not None and s.start_time.year == 2026
    assert 0 < s.duration_s < 86400
    assert 30 <= s.avg_hr <= 220 and 30 <= s.max_hr <= 220 and 30 <= s.min_hr <= 220
    assert s.total_step >= 0 and s.total_calories >= 0 and s.total_distance_m >= 0
    # (cadence deliberately NOT range-checked — reads absurd on the older record; see docstring)


# --------------------------------------------------------------------------- SportHead (synthetic)
def _head_payload(**kw) -> bytes:
    p = bytearray(80)
    struct.pack_into("<H", p, 16, kw.get("year", 2026))
    p[18], p[19], p[20], p[21], p[22] = (kw.get("month", 7), kw.get("day", 12),
                                         kw.get("hour", 8), kw.get("minute", 2), kw.get("second", 17))
    struct.pack_into("<I", p, 23, kw.get("dur", 499))
    p[32], p[33], p[34] = kw.get("avg", 80), kw.get("max", 104), kw.get("min", 58)
    struct.pack_into("<I", p, 55, kw.get("step", 63))
    struct.pack_into("<I", p, 59, kw.get("cal", 27))
    struct.pack_into("<I", p, 63, kw.get("dist", 42))
    struct.pack_into("<H", p, 75, kw.get("cad", 7))
    p[77] = kw.get("stride", 66)
    return bytes(p)


def test_decode_sport_head_pinned_fields():
    import datetime
    h = workout.decode_sport_head(_head_payload())
    assert h.start_time == datetime.datetime(2026, 7, 12, 8, 2, 17)   # packed SportTime, not epoch
    assert (h.duration_s, h.avg_hr, h.max_hr, h.min_hr) == (499, 80, 104, 58)
    assert (h.total_step, h.total_calories, h.total_distance_m) == (63, 27, 42)
    assert (h.cadence_spm, h.stride_cm) == (7, 66)


def test_decode_sport_head_short_returns_none():
    assert workout.decode_sport_head(b"\x00" * 40) is None


def test_decode_sport_head_bad_date_keeps_other_fields():
    p = bytearray(_head_payload()); p[18] = 99          # month 99 -> invalid packed date
    h = workout.decode_sport_head(bytes(p))
    assert h.start_time is None and h.duration_s == 499 and h.total_step == 63
