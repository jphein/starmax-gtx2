"""Tests for records.py biometric extraction (G1/G2/G3) + date-marker logic (G4).

Vectors mirror the Gadgetbridge coordinator's StarmaxProtocolTest.kt byte-for-byte so the two
implementations decode identically. Sample bytes are synthetic (no real biometrics).
"""
from starmax_client import records as R


def _rec(hexstr: str) -> bytes:
    return bytes.fromhex(hexstr)


# ---------------------------------------------------------------- G2 SpO2 (mirrors GB)
def test_spo2_shapeB_date_category_and_values():
    # Shape B cat 2: 04 00 | 10 02 | <8 filler> | ea07 07 0b (2026-07-11) | 02 00 <nsamp=4> 5f 60 61 5f
    h = R.parse_health_record_header(_rec("040010020000000000000000ea07070b0200040000005f60615f"))
    assert h.category == R.CAT_SPO2
    assert (h.year, h.month, h.day) == (2026, 7, 11)
    assert R.extract_spo2(h.data) == [95, 96, 97, 95]


def test_spo2_does_not_emit_count_byte_as_phantom():
    # nsamp = 0x5f = 95 (in-range). Skipping the sub-header yields only the 2 real samples 96, 97.
    h = R.parse_health_record_header(_rec("040010020000000000000000ea07070b02005f0000006061"))
    assert R.extract_spo2(h.data) == [96, 97]


def test_spo2_absent_subheader_declines():
    assert R.extract_spo2(b"\x5f\x60\x61") == []  # no 02 00 sub-header -> decline, don't guess


# ---------------------------------------------------------------- G1 HR (mirrors GB)
def test_hr_requires_run_of_ff_not_lone_ff():
    # Shape A cat 0. data = ff 4b 4c 4d ff ff 55 5a: the lone leading 0xff (followed by in-range
    # counters 4b/4c/4d) must NOT anchor the tail — only the ff-ff run does -> [85, 90].
    h = R.parse_health_record_header(_rec("02000000000000000000ea07070bff4b4c4dffff555a"))
    assert h.category == R.CAT_DAILY_ACTIVITY
    assert h.year == 2026
    assert R.extract_heart_rates(h.data) == [85, 90]


def test_hr_no_ff_run_yields_nothing():
    # A lone 0xff with no second within the window -> no tail anchor -> [].
    assert R.extract_heart_rates(bytes([0x50, 0xFF, 0x51, 0x52, 0x53])) == []


def test_hr_filters_out_of_range():
    # ff-run then bytes: 0x00 (0), 0x64 (100), 0xff (255), 0x4b (75) -> keep 100, 75.
    assert R.extract_heart_rates(bytes([0xFF, 0xFF, 0x00, 0x64, 0xFF, 0x4B])) == [100, 75]


# ---------------------------------------------------------------- G3 sleep (mirrors GB)
def test_sleep_decodes_subheader_samples():
    # Shape B cat 3 (sleep — CORRECTED from 5), date@12, then 02 00 <nsamp=3> 0d 0e 03.
    h = R.parse_health_record_header(_rec("040010030000000000000000ea07070b0200030000000d0e03"))
    assert h.category == R.CAT_SLEEP == 3
    assert h.year == 2026
    assert R.extract_sleep_samples(h.data) == [13, 14, 3]  # raw, unfiltered (provisional)


# ---------------------------------------------------------------- status + date (G4)
def test_status_reply_has_no_date():
    # read-status reply: 04 00 08 01 10 05
    h = R.parse_health_record_header(_rec("040008011005"))
    assert h.is_status is True
    assert h.category == 5
    assert h.year is None and h.data == b""


def test_g4_fixed_offset_is_primary():
    # Shape A: date is at the FIXED off10, even though earlier bytes could scan-match a plausible
    # date. Here bytes 0-9 are zero, off10 = ea07 07 0b -> 2026-07-11 via the fixed path.
    h = R.parse_health_record_header(_rec("02000000000000000000ea07070b"))
    assert h.data_offset == 14  # off10 + 4
    assert (h.year, h.month, h.day) == (2026, 7, 11)


def test_g4_scan_fallback_when_fixed_invalid():
    # Shape B but the fixed off12 is NOT a valid date (all zero there); a valid date sits later,
    # so the plausibility-scan fallback finds it.
    #   04 00 10 02 | 8 filler | 00 00 00 00 (off12 invalid) | ea07 07 0b (valid, later)
    h = R.parse_health_record_header(_rec("04001002000000000000000000000000ea07070b"))
    assert (h.year, h.month, h.day) == (2026, 7, 11)  # located via fallback scan


def test_empty_and_short():
    assert R.parse_health_record_header(b"").present is False
    assert R.extract_spo2(b"") == [] and R.extract_sleep_samples(b"") == []
    assert R.extract_heart_rates(b"") == []


# ---------------------------------------------------------------- activity (cat 5) — live field map
def test_extract_activity_field_map():
    """Reproduce the LIVE-CONFIRMED cat-5 ActivityDataModel map with synthetic totals: after the
    date marker, u16 then u32[] = [steps, ?, active?, calories, distance_m, ?]."""
    # shape B (04 00 10 05) + 8 filler -> date@12 = ea 07 07 0c (2026-07-12) -> u16 + 6xu32
    header = "040010050000000000000000" + "ea07070c"
    u16 = "0100"
    u32s = "a2000000" + "00000000" + "00000000" + "8d010000" + "73000000" + "00000000"
    h = R.parse_health_record_header(_rec(header + u16 + u32s))
    assert h.category == R.CAT_ACTIVITY == 5 and h.year == 2026 and h.month == 7 and h.day == 12
    act = R.extract_activity(_rec(header + u16 + u32s))
    assert act is not None
    assert act.steps == 162 and act.calories == 397 and act.distance_m == 115


def test_extract_activity_none_on_empty():
    # a near-empty activity record (no data region) -> None, not a crash
    assert R.extract_activity(_rec("04001005080110")) is None       # status-ish / too short


# --------------------------------------------------- cat-0 DETAIL sparse HR (mirrors GB, issue #21)
def test_cat0_detail_flag03_marker20_is_category0():
    # The intraday-HR DETAIL record is flag 0x03 with a `20` head marker at offset 2 (no `10 <cat>`).
    # It must be recognised as category 0, not None (the mis-tag GB fixed). date @11.
    h = R.parse_health_record_header(_rec("0300208204010028000000" + "ea07070c" + "020000000000"))
    assert h.present is True
    assert h.category == R.CAT_DAILY_ACTIVITY == 0
    assert (h.year, h.month, h.day) == (2026, 7, 12)


def test_cat0_detail_extracts_sparse_hr_from_second_byte():
    # cat-0 DETAIL HR array: `02 00 <nsamp>` then 2-byte slots [marker][bpm]; `ff 00` = empty,
    # bpm is the SECOND byte (0x59=89, 0x42=66). Sparse output with slot indices (mirrors GB).
    #   header(11) | date@11 | 02 00 <nsamp=4> | ff00 | bc59 | ff00 | 6142
    h = R.parse_health_record_header(
        _rec("0300208204010028000000" + "ea07070c" + "020004000000" + "ff00bc59ff006142"))
    assert h.category == R.CAT_DAILY_ACTIVITY
    curve = R.extract_daily_heart_rates(h.data)
    assert curve is not None
    assert curve.total_slots == 4
    assert curve.samples == [R.DailyHrSample(1, 89), R.DailyHrSample(3, 66)]


def test_daily_hr_none_without_subheader():
    assert R.extract_daily_heart_rates(b"\x59\x42\x60") is None  # no 02 00 sub-header -> decline


# --------------------------------------------------- stress (cat 1) / HRV (cat 7) dated values (GB)
def test_stress_cat1_per_day_sub_records():
    # cat 1 stress: two dated sub-records, each with the daily value as a u32 at date+8 (values 8, 10).
    # The head has no valid date, so the scan anchors on the two real markers.
    rec = _rec("040010012000000000000000" + "ea07070c0000000008000000" + "ea07070b000000000a000000")
    h = R.parse_health_record_header(rec)
    assert h.category == R.CAT_STRESS == 1
    values = R.extract_dated_values(rec, 8, 0, 100)
    assert len(values) == 2
    assert (values[0].year, values[0].day, values[0].value) == (2026, 12, 8)
    assert (values[1].day, values[1].value) == (11, 10)


def test_hrv_cat7_per_day_sub_records():
    # cat 7 HRV: same shape; value u32 at date+8 (values 79, 112).
    rec = _rec("040010072000000000000000" + "ea07070c000000004f000000" + "ea07070b0000000070000000")
    h = R.parse_health_record_header(rec)
    assert h.category == R.CAT_HRV == 7
    values = R.extract_dated_values(rec, 8, 0, 255)
    assert [v.value for v in values] == [79, 112]
    assert [v.day for v in values] == [12, 11]


def test_dated_values_range_filter_rejects_false_markers():
    # A value outside the sane range is dropped rather than injected. date+8 = 0x1388 = 5000,
    # out of the 0..100 stress range.
    rec = _rec("040010012000000000000000" + "ea07070c0000000088130000")
    assert R.extract_dated_values(rec, 8, 0, 100) == []
