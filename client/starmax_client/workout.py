"""Workout (cat 4) record parser — a fixed head preamble + a KLV block stream.

Block framing **CRACKED** from a real workout record (issue #12 follow-up):

    data[0 : HEAD_PREAMBLE_LEN]   fixed head preamble (summary; NOT a keyed block)
    then repeated:  [key:u8] [length:u32 LE] [value]          (5-byte block header)

Proven exactly by the HR block: ``01 1a000000`` -> key=1(hr), len=26, value = the 26 bpm bytes.
Keys (SyncSportKey): 0=head, 1=hr, 2=kmSpeed, 3=step, 4=kmPace, 5=stepStride, 6=trailData,
7=imSpeed, 8=imPace, 9=elevation, 10=speedPace.

⚠️ TODO — pending ONE fresh GPS-locked workout (NOT guessed; see docs/workout-gps.md):
  (a) **head-field byte offsets** — the initial "all int32-LE" reading is partly wrong: avg/max/min HR read
      as consecutive *bytes* (95/105/84) in the real record, not int32. Pin by value-correlation
      against a real workout (JP reads exact stats off the watch).
  (b) **post-step tail framing** — the region after the ``step`` block does not tile as clean
      ``[u8][u32]`` blocks; resolve with a second record.
  (c) **trailData point decode** (:func:`starmax_client.gpstrack.decode_gps_track`) — UNVERIFIED,
      no real GPS lock captured yet.

So this parser splits the confirmed structure (head preamble + keyed blocks up to the first
anomaly) and hands ``key=6`` to gpstrack; it does NOT decode head fields. PII: biometrics/location
— never commit real values; the fixture test asserts structure only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import datetime as _dt
import struct

from starmax_client import gpstrack
from starmax_client.records import parse_health_record_header

# Fixed head-preamble length (empirical, from the real record: the first keyed block — HR — begins
# at data[0x64]). Confirm/generalize against more records.
HEAD_PREAMBLE_LEN = 0x64

# SportHead summary fields are at PAYLOAD-ABSOLUTE offsets (into the whole record), PINNED against
# JP's live watch readout on the 2026-07-12 workout — every field ground-truth-confirmed. The exact
# offsets are in decode_sport_head(); startTime is a PACKED SportTime (y/m/d/h/m/s), NOT an epoch.
_HEAD_MIN_LEN = 78  # need through stride_cm @77

SPORT_KEYS = {0: "head", 1: "hr", 2: "kmSpeed", 3: "step", 4: "kmPace", 5: "stepStride",
              6: "trailData", 7: "imSpeed", 8: "imPace", 9: "elevation", 10: "speedPace"}
KEY_TRAIL = 6
_BLOCK_HDR = 5  # [key:u8][length:u32 LE]


@dataclass
class SportHead:
    """Workout summary from the head preamble. All fields PINNED against a live watch readout
    (2026-07-12). ``sport_type`` and ``mets`` are NOT decoded (see decode_sport_head)."""
    start_time: Optional[_dt.datetime]  # packed SportTime (NOT epoch)
    duration_s: int
    avg_hr: int
    max_hr: int
    min_hr: int
    total_step: int
    total_calories: int
    total_distance_m: int
    cadence_spm: int
    stride_cm: int


def decode_sport_head(payload: bytes) -> Optional[SportHead]:
    """Decode the workout summary from the raw record ``payload`` (PAYLOAD-ABSOLUTE offsets).

    Field map ground-truth-confirmed on the 2026-07-12 record (see docs/workout-gps.md). Returns
    ``None`` if the record is too short. ``start_time`` is a packed SportTime -> datetime (naive
    local; ``None`` if the packed value isn't a valid date). NOTE — NOT decoded (offsets not
    ground-truth-pinned; do not guess): ``sportType`` (a candidate constant byte@9=106 across our
    two walks, unconfirmed) and ``mets`` (JP=3, but no plain int at any offset — scaled/float, TODO).
    Also: ``cadence_spm@75`` matched exactly on 2026-07-12 but reads absurd on the older 2026-07-11
    record (possible older-format head variance) — trust it only where sane.
    """
    if len(payload) < _HEAD_MIN_LEN:
        return None
    u16 = lambda o: struct.unpack_from("<H", payload, o)[0]   # noqa: E731
    u32 = lambda o: struct.unpack_from("<I", payload, o)[0]   # noqa: E731
    try:
        start = _dt.datetime(u16(16), payload[18], payload[19], payload[20], payload[21], payload[22])
    except ValueError:
        start = None
    return SportHead(start_time=start, duration_s=u32(23),
                     avg_hr=payload[32], max_hr=payload[33], min_hr=payload[34],
                     total_step=u32(55), total_calories=u32(59), total_distance_m=u32(63),
                     cadence_spm=u16(75), stride_cm=payload[77])


@dataclass
class WorkoutRecord:
    head_preamble: bytes                    # data[0:HEAD_PREAMBLE_LEN] — raw summary region
    head: Optional[SportHead] = None        # decoded summary (pinned fields); None if too short
    blocks: Dict[int, bytes] = field(default_factory=dict)  # key -> value (parsed KLV blocks)
    trail: List["gpstrack.TrackPoint"] = field(default_factory=list)  # key=6 decoded (UNVERIFIED)
    clean: bool = True                      # False if the walk stopped on an anomaly (not padding)
    remainder: bytes = b""                  # unparsed tail from the stop point (for tail-framing TODO(b))

    @property
    def block_keys(self) -> List[str]:
        return [SPORT_KEYS.get(k, f"?{k}") for k in self.blocks]


def parse_workout_klv(payload: bytes) -> WorkoutRecord:
    """Split a cat-4 workout record into its head preamble + KLV blocks (confirmed framing).

    Walks ``[key:u8][length:u32 LE][value]`` from ``HEAD_PREAMBLE_LEN``, collecting blocks until a
    clean stop (zero-padding ``00`` + ``len 0``, or EOF) or an anomaly (unknown key / length
    overrun). Never raises on a short/odd record — returns whatever parsed plus ``remainder`` and
    ``clean``. ``key=6`` (trailData) is decoded via gpstrack (empty-safe, UNVERIFIED).
    """
    data = parse_health_record_header(payload).data
    head = data[:HEAD_PREAMBLE_LEN]
    blocks: Dict[int, bytes] = {}
    i = HEAD_PREAMBLE_LEN
    clean = True
    while i + _BLOCK_HDR <= len(data):
        key = data[i]
        ln = int.from_bytes(data[i + 1:i + _BLOCK_HDR], "little")
        v = i + _BLOCK_HDR
        if key == 0 and ln == 0:                 # zero padding -> clean stop
            break
        if key not in SPORT_KEYS or v + ln > len(data):
            clean = False                        # anomaly (tail framing TODO(b)) -> stop, keep tail
            break
        blocks[key] = data[v:v + ln]             # last-wins if a key repeats
        i = v + ln
    trail = gpstrack.decode_gps_track(blocks[KEY_TRAIL]) if KEY_TRAIL in blocks else []
    return WorkoutRecord(head_preamble=head, head=decode_sport_head(payload), blocks=blocks,
                         trail=trail, clean=clean, remainder=data[i:])
