"""Tests for workout GPS-track decode + GPX export (issue #12).

UNVERIFIED on real data (the only real workout has an empty trail) — so these lock the decode
MATH only, with SYNTHETIC coordinates (a made-up SF-ish path chosen to exercise negative-lng /
western-hemisphere signed decoding). Never commit a real track (GPS = PII).
"""
from __future__ import annotations

from starmax_client import framing, gpstrack
from starmax_client.commands import health


def _pt(lat_e7: int, lng_e7: int) -> bytes:
    # one inline trail point: int32 LE lat, int32 LE lng (signed)
    return lat_e7.to_bytes(4, "little", signed=True) + lng_e7.to_bytes(4, "little", signed=True)


def test_decode_signed_e7():
    trail = _pt(377749000, -1224194000) + _pt(377750000, -1224195000)   # SF-ish, western hemisphere
    pts = gpstrack.decode_gps_track(trail)
    assert len(pts) == 2
    assert abs(pts[0].lat - 37.7749) < 1e-9 and abs(pts[0].lng - (-122.4194)) < 1e-9
    assert pts[1].lng < 0                                   # negative lng survives (signed int32)


def test_decode_e6_scale_option():
    pts = gpstrack.decode_gps_track(_pt(37774900, -122419400), scale=1e6)
    assert abs(pts[0].lat - 37.7749) < 1e-6 and abs(pts[0].lng - (-122.4194)) < 1e-6


def test_drops_null_points():
    trail = _pt(377749000, -1224194000) + _pt(0, 0)
    assert len(gpstrack.decode_gps_track(trail)) == 1                    # (0,0) padding dropped
    assert len(gpstrack.decode_gps_track(trail, drop_null=False)) == 2


def test_empty_trail_yields_nothing():
    # matches the real record: an empty trail (no bytes / all-zero) decodes to no points, no crash
    assert gpstrack.decode_gps_track(b"") == []
    assert gpstrack.decode_gps_track(bytes(16)) == []                   # two null points, all dropped


def test_to_gpx_is_wellformed():
    pts = gpstrack.decode_gps_track(_pt(377749000, -1224194000) + _pt(377750000, -1224195000))
    gpx = gpstrack.to_gpx(pts, name="run")
    assert gpx.startswith("<?xml") and "<gpx" in gpx and "</gpx>" in gpx
    assert 'lat="37.7749000"' in gpx and 'lon="-122.4194000"' in gpx and gpx.count("<trkpt") == 2
    import xml.dom.minidom as _m
    _m.parseString(gpx)                                                 # parses as XML


def test_workout_fetch_is_cat4_on_0x0e_not_wrong_layer_0x61():
    """The trail is fetched via the [CAP] 0x0e history-sync at category 4 (WORKOUT) — the right
    creek layer — NOT the wrong-layer Java-SDK 0x61."""
    fr = framing.parse_frame(health.build_workout_history(seq=1), direction=framing.DIR_APP_TO_WATCH)
    assert fr.opcode == 0x0E and health.CAT_WORKOUT == 4
