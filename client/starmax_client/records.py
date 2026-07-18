"""Decoder for the 0x0e flag=1 binary health records (docs/protocol-spec.md §6.2/§6.3).

These records are NOT protobuf and carry NO CRC. Two header shapes were observed; both embed a
`yr:u16LE mo:u8 dy:u8` date at a fixed offset that anchors the record:

    Shape A (cat 0):        02 00 | 20 <len u24> | <u32 count> | <DATE @off10> | data...
    Shape B (cat 1,2,5,7):  04 00 | 10 <cat> | 20 <u24> | <u32> | <DATE @off12> | data...
    status reply (subop=1): 04 00 08 01 10 <cat>  /  02 00 08 01   (no date, no data)

The structural header + date + category decode reliably. Per-sample biometric extraction
(`extract_heart_rates` / `extract_spo2` / `extract_sleep_samples`) is a **byte-faithful port of the
Gadgetbridge coordinator** (`StarmaxHealthRecord.kt`) and is **PROVISIONAL**: the intraday per-sample
*stride/timing* is UNRESOLVED (spec §6.3, §10.3). Only the *values* are trusted; callers must treat
timing as unknown.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

# Health categories = the watch syncType enum. Single source of truth: commands.base
# (AUTHORITATIVE — sleep is cat 3, cat 5 is activity/steps; see base.py). Re-exported here so
# `records.CAT_*` keeps working, now with the CORRECT values.
from starmax_client.commands.base import (  # noqa: E402,F401
    CAT_ACTIVITY_HR as CAT_DAILY_ACTIVITY, CAT_STRESS, CAT_SPO2, CAT_SLEEP, CAT_ACTIVITY,
    CAT_WORKOUT, CAT_HRV)

_FLAG_SHAPE_A = 0x02   # category 0 summary
_FLAG_SHAPE_B = 0x04   # categories 1,2,5,7 (0x05 = a fuller record variant)
_MARKER_STATUS = 0x08
_MARKER_CATEGORY = 0x10
# Category-0 records carry a `20` head marker at offset 2 (no `10 <cat>`); the summary uses flag
# 0x02, the intraday-HR DETAIL uses flag 0x03. Detecting on the marker (not just flag 0x02) is what
# stops the DETAIL record from being mis-tagged category None (GB fix: "-1 mis-tag", issue #21).
_MARKER_ACTIVITY_HEAD = 0x20

# Date validity window — matches the GB coordinator (StarmaxHealthRecord.parse).
_YEAR_MIN, _YEAR_MAX = 2000, 2100


@dataclass
class HealthRecordHeader:
    present: bool                 # leading flag byte (0x02/0x04/0x05) indicates a record
    category: Optional[int]       # 0 (shape A) or the echoed category (shape B); None if unknown
    year: Optional[int]
    month: Optional[int]
    day: Optional[int]
    data_offset: Optional[int]    # index just past the date marker (start of the data region)
    length: int                   # total record length
    is_status: bool = False       # True for a read-status reply (no date, no samples)
    data: bytes = field(default=b"")  # region after the date marker (empty for status/dateless)


def _u32le(d: bytes, off: int) -> int:
    if off + 4 > len(d):
        return -1
    return d[off] | (d[off + 1] << 8) | (d[off + 2] << 16) | (d[off + 3] << 24)


def _valid_date(year: int, month: int, day: int) -> bool:
    return _YEAR_MIN <= year <= _YEAR_MAX and 1 <= month <= 12 and 1 <= day <= 31


def _read_date(payload: bytes, off: int):
    return (payload[off] | (payload[off + 1] << 8), payload[off + 2], payload[off + 3])


def _scan_date_marker(payload: bytes) -> Optional[int]:
    """FALLBACK date locator (G4): scan for the first plausible `yr mo dy`.

    The fixed per-shape offset (off10 A / off12 B) is the PRIMARY locator and matches the GB
    coordinator; this plausibility scan is used only when the fixed offset yields an invalid date
    (robustness against header-shape variants / short records). It can mis-locate if a leading
    counter byte-pair coincidentally looks like a date, so it is deliberately the fallback.
    """
    for i in range(0, len(payload) - 3):
        year, month, day = _read_date(payload, i)
        if _valid_date(year, month, day):
            return i
    return None


def parse_health_record_header(payload: bytes) -> HealthRecordHeader:
    """Decode a record's structural header (never touches biometric samples).

    Mirrors the GB coordinator's `StarmaxHealthRecord.parse`: shape/category detection, status-reply
    short-circuit, and the date marker at the fixed per-shape offset (G4 primary) with the
    plausibility scan as fallback.
    """
    if not payload:
        return HealthRecordHeader(False, None, None, None, None, None, 0)

    flag = payload[0]
    # flag 0x03 = the cat-0 intraday-HR DETAIL record (GB fix); 0x05 = a fuller shape-B variant.
    present = flag in (_FLAG_SHAPE_A, 0x03, _FLAG_SHAPE_B, 0x05)
    shape_a = flag == _FLAG_SHAPE_A
    marker = payload[2] if len(payload) > 2 else -1
    is_status = marker == _MARKER_STATUS

    # Category: shape A is always cat 0; the flag-0x03 DETAIL record carries a `20` head marker and
    # is ALSO cat 0 (mirrors GB — without this it was mis-tagged category None); shape B echoes the
    # category as `10 <cat>`; status shape B is `04 00 08 01 10 <cat>`.
    if shape_a:
        category: Optional[int] = CAT_DAILY_ACTIVITY
    elif marker == _MARKER_ACTIVITY_HEAD:
        category = CAT_DAILY_ACTIVITY
    elif marker == _MARKER_CATEGORY and len(payload) > 3:
        category = payload[3]
    elif is_status and len(payload) > 5 and payload[4] == _MARKER_CATEGORY:
        category = payload[5]
    else:
        category = None

    if is_status:
        return HealthRecordHeader(present, category, None, None, None, None, len(payload),
                                  is_status=True, data=b"")

    # G4: fixed offset per shape is PRIMARY (off10 A / off12 B); scan only as fallback.
    date_off = 10 if shape_a else 12
    marker_off: Optional[int] = None
    if len(payload) >= date_off + 4:
        y, m, d = _read_date(payload, date_off)
        if _valid_date(y, m, d):
            marker_off = date_off
    if marker_off is None:
        marker_off = _scan_date_marker(payload)

    if marker_off is None:
        return HealthRecordHeader(present, category, None, None, None, None, len(payload),
                                  is_status=False, data=b"")

    year, month, day = _read_date(payload, marker_off)
    data = payload[marker_off + 4:]
    return HealthRecordHeader(present, category, year, month, day, marker_off + 4, len(payload),
                              is_status=False, data=data)


# --------------------------------------------------------------------------- biometric extraction
# Byte-faithful ports of Gadgetbridge's StarmaxHealthRecord.{extractHeartRates,extractSpo2,
# extractSleepSamples}. All operate on the record's `data` region (payload past the date marker,
# i.e. HealthRecordHeader.data). PROVISIONAL — see module docstring: values are trusted, intraday
# timing is UNRESOLVED.
def _index_of_subheader(d: bytes) -> int:
    """Index of the first `02 00` sub-header in `d`, or -1."""
    for i in range(len(d) - 1):
        if d[i] == 0x02 and d[i + 1] == 0x00:
            return i
    return -1


def extract_heart_rates(data: bytes, min_bpm: int = 30, max_bpm: int = 220) -> List[int]:
    """G1: best-effort intraday HR from a category-0 record's data region (spec §6.3).

    HR lives in the record TAIL: a run of `0xff` no-sample markers, then one byte per sample in bpm.
    We skip to the first `0xff` that is part of a RUN — another `0xff` within a 4-byte window — so a
    lone `0xff` inside the leading step/cal/dist counters cannot false-trigger the tail; then keep
    bytes in a plausible bpm range (which also drops the `0xff`/`0x00` markers).

    PROVISIONAL: the per-sample stride/timing is UNRESOLVED (spec §6.3); only the bpm *values* are
    confirmed. Callers must treat timing as unknown.
    """
    tail_start = -1
    for i in range(len(data)):
        if data[i] != 0xFF:
            continue
        window_end = min(i + 4, len(data))
        if any(data[j] == 0xFF for j in range(i + 1, window_end)):
            tail_start = i
            break
    if tail_start < 0:
        return []
    return [b for b in data[tail_start:] if min_bpm <= b <= max_bpm]


@dataclass
class DailyHrSample:
    """One intraday HR reading with its slot position within the day's fixed-cadence array."""
    slot_index: int
    bpm: int


@dataclass
class DailyHrCurve:
    """Intraday HR array from a cat-0 DETAIL record: total slot count + the non-empty samples."""
    total_slots: int
    samples: List[DailyHrSample]


def extract_daily_heart_rates(data: bytes, min_bpm: int = 30,
                              max_bpm: int = 220) -> Optional[DailyHrCurve]:
    """Decode the intraday HR array from a category-0 DETAIL record (the flag-0x03 variant), or
    None if there is no array. Byte-faithful port of GB ``extractDailyHeartRates``.

    Layout (confirmed on a live capture): after a ``02 00 <u32 nsamp>`` sub-header the record is a
    fixed array of 2-byte slots ``[marker:u8][bpm:u8]``; an empty (unlogged) slot is ``ff 00``
    (marker 0xff). The **second** byte is the bpm (first byte varies wildly and is NOT HR). We
    return one sample per non-empty, physiological slot, tagged with its slot index.

    SPARSE output is EXPECTED — the watch logs HR infrequently (most slots are ``ff 00``).
    PROVISIONAL: the per-slot cadence is assumed uniform across the day (timing placeholder); the
    bpm VALUES are confirmed. The summary (flag-0x02) cat-0 record has an empty array and yields
    None. Caller ensures ``data`` is a cat-0 record's data region (see :func:`extract_heart_rates`).
    """
    hdr = _index_of_subheader(data)
    if hdr < 0 or hdr + 6 > len(data):
        return None
    start = hdr + 6
    total_slots = (len(data) - start) // 2
    if total_slots <= 0:
        return None
    samples: List[DailyHrSample] = []
    for i in range(total_slots):
        o = start + i * 2
        marker = data[o]
        bpm = data[o + 1]
        if marker != 0xFF and min_bpm <= bpm <= max_bpm:
            samples.append(DailyHrSample(i, bpm))
    return DailyHrCurve(total_slots, samples)


def extract_spo2(data: bytes, min_pct: int = 70, max_pct: int = 100) -> List[int]:
    """G2: best-effort SpO2 from a category-2 record's data region (spec §6.3, cat 2 RESOLVED).

    Samples follow a `02 00 <u32 nsamp LE>` sub-header; we locate it and read EXACTLY nsamp bytes so
    the count is never mistaken for a reading, keeping only physiological values. If the sub-header is
    absent we decline rather than guess. Intraday timing remains PROVISIONAL.
    """
    hdr = _index_of_subheader(data)
    if hdr < 0 or hdr + 6 > len(data):
        return []
    nsamp = _u32le(data, hdr + 2)
    start = hdr + 6
    if nsamp <= 0 or start >= len(data):
        return []
    end = min(start + nsamp, len(data))
    return [b for b in data[start:end] if min_pct <= b <= max_pct]


def extract_sleep_samples(data: bytes) -> List[int]:
    """G3: raw sleep samples from a category-3 record's data region (spec §6.3).

    (NB: sleep is category 3, not 5 — the earlier "near-empty cat-5 sleep stub" was actually the
    *activity* record, cat 5; see :func:`extract_activity`.)

    Like SpO2, samples follow a `02 00 <u32 nsamp>` sub-header; returns the nsamp bytes after it.

    PROVISIONAL — deliberately NOT persisted (mirrors the GB coordinator): across every capture the
    cat-5 record is a near-empty stub (the watch was never worn for a real sleep session), so the
    stage-code -> stage mapping, per-sample cadence/timestamp, and durations cannot be derived from
    bytes. Guessing a layout would violate the capture-derived (clean-room) rule. Returns the raw
    bytes for logging and for finishing the decode once a populated sleep capture exists.
    """
    hdr = _index_of_subheader(data)
    if hdr < 0 or hdr + 6 > len(data):
        return []
    nsamp = _u32le(data, hdr + 2)
    start = hdr + 6
    if nsamp <= 0 or start >= len(data):
        return []
    end = min(start + nsamp, len(data))
    return list(data[start:end])


@dataclass
class DatedValue:
    """One dated summary value from a per-day sub-record (stress cat 1, HRV cat 7)."""
    year: int
    month: int
    day: int
    value: int


def extract_dated_values(payload: bytes, value_delta: int, min_value: int,
                         max_value: int) -> List[DatedValue]:
    """Decode one summary value per dated sub-record — used by stress (cat 1) and HRV (cat 7),
    which pack several sub-records, one per day. Byte-faithful port of GB ``extractDatedValues``.

    Each sub-record is ``year:u16 month:u8 day:u8 <packed time> value:u32``, with the value
    ``value_delta`` bytes past the date marker (8 for both stress and HRV, confirmed on the
    2026-07-12 capture). We scan the WHOLE record for EVERY date marker and read the value, keeping
    only those in ``min_value..max_value`` so a stray byte-run that looks like a date can't inject
    garbage. Operates on the full record ``payload`` (GB's ``raw``), not just the data region.

    SINGLE-CAPTURE CAVEAT (mirrors GB): the field offset is confirmed, but the value scaling/units
    are not (stress assumed 0-100 score; HRV assumed ms) — callers must flag this, not treat as
    exact.
    """
    out: List[DatedValue] = []
    o = 0
    n = len(payload)
    while o <= n - 4:
        year, month, day = _read_date(payload, o)
        if _valid_date(year, month, day):
            vo = o + value_delta
            if vo + 4 <= n:
                v = _u32le(payload, vo)
                if min_value <= v <= max_value:
                    out.append(DatedValue(year, month, day, v))
            o += 4  # past this date marker; the next sub-record's marker is further on
        else:
            o += 1
    return out


@dataclass
class ActivityData:
    """A category-5 ActivityDataModel readout: daily totals."""
    steps: int
    distance_m: int
    calories: int
    raw_u32: List[int]  # the full decoded u32 array (for the unmapped fields / debugging)


def extract_activity(payload: bytes) -> Optional[ActivityData]:
    """[LIVE-CONFIRMED 2026-07-12] Decode a category-5 activity record → steps / distance / calories.

    Field map (empirical, confirmed against the watch face): after the date marker
    (``ea 07 <mm> <dd>``) there is a u16, then a little-endian u32 array
    ``[totalStep, ?, active?, calories, distance_m, ?]``. Live check: steps=162 (matched the
    face), distance_m=115 (~0.71 m/step), calories≈397 (ticked up over a poll). Polling ~1.5 s
    gives an effectively-live step readout.

    Returns ``None`` for a status reply or a record too short to reach ``distance_m`` (the watch
    emits a near-empty activity record before any steps accrue).
    """
    h = parse_health_record_header(payload)
    d = h.data
    navail = (len(d) - 2) // 4 if len(d) >= 2 else 0
    if h.is_status or navail < 5:            # need through distance_m (u32 index 4)
        return None
    n = min(navail, 6)
    u32 = [_u32le(d, 2 + 4 * i) for i in range(n)]
    return ActivityData(steps=u32[0], distance_m=u32[4], calories=u32[3], raw_u32=u32)
