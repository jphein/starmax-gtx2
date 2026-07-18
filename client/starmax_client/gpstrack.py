"""Workout GPS-track decode + GPX export (issue #12).

⚠️ **UNVERIFIED — pending a real GPS-locked workout.** The trail could not be confirmed on real
data: the only real workout record we have (2026-07-11) carries an **EMPTY** trail (GPS never
locked on that short test), and an exhaustive scan across encodings (int32 LE/BE E7/E6,
float32/64) found **zero** coherent coordinates. This module implements the trail layout from
the vendor creek-module decompile (§12.3) and is unit-tested with **SYNTHETIC coordinates ONLY**. Do not
trust decoded coordinates until confirmed against a GPS-locked workout.

Fetch — the RIGHT layer: the workout record is pulled via the ``0x0e`` history-sync at **category
4 (WORKOUT)** — :func:`starmax_client.commands.health.build_workout_history`. NOT the wrong-layer
Java-SDK ``0x61`` (that's the legacy 0xDA protocol, not the creek 0xC1 wire). The SportDataModel
record carries a workout summary plus the GPS trail **inline**.

Trail layout [SCHEMA — creek §12.3]: consecutive **8-byte points**, each = ``int32 LE latitude``
followed by ``int32 LE longitude`` (signed two's-complement). Degrees = ``raw / 1e7``.

PII: coordinates are location data — tests use synthetic coords, a decoded route stays local,
never committed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

# Degrees = raw int32 / LATLNG_SCALE. 1e7 ("E7") is the convention — but **no literal
# 1e7 was found in the creek code**, so this is unconfirmed: sanity-check the magnitude on a real track
# (a lat raw ~3.7e8 => 37.x° at E7; ~3.7e7 => E6) and change if needed.
LATLNG_SCALE = 1e7

POINT_SIZE = 8  # int32 lat + int32 lng, little-endian


@dataclass
class TrackPoint:
    lat: float
    lng: float


def decode_gps_track(trail: bytes, *, scale: float = LATLNG_SCALE,
                     drop_null: bool = True) -> List[TrackPoint]:
    """Decode an inline workout trail (``[int32 LE lat, int32 LE lng]`` × N) into route points.

    ⚠️ UNVERIFIED on real data (see module docstring) — synthetic-tested only. ``signed`` int32 is
    handled natively (western/southern-hemisphere coords are negative). ``drop_null`` skips (0,0)
    padding points (the tail padding the watch emits, and the all-zero content of an empty trail).
    """
    pts: List[TrackPoint] = []
    for i in range(0, len(trail) - (POINT_SIZE - 1), POINT_SIZE):
        lat_raw = int.from_bytes(trail[i:i + 4], "little", signed=True)
        lng_raw = int.from_bytes(trail[i + 4:i + 8], "little", signed=True)
        if drop_null and lat_raw == 0 and lng_raw == 0:
            continue
        pts.append(TrackPoint(lat=lat_raw / scale, lng=lng_raw / scale))
    return pts


def to_gpx(points: List[TrackPoint], *, name: str = "GTX2 workout") -> str:
    """Render decoded points as a GPX 1.1 ``<trk>`` (dependency-free)."""
    esc = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<gpx version="1.1" creator="starmax-client" xmlns="http://www.topografix.com/GPX/1/1">',
           f"  <trk><name>{esc}</name><trkseg>"]
    out += [f'    <trkpt lat="{p.lat:.7f}" lon="{p.lng:.7f}"></trkpt>' for p in points]
    out += ["  </trkseg></trk>", "</gpx>"]
    return "\n".join(out)
