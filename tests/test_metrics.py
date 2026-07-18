"""Health/device metric readers reuse the GB-parity decoders and never crash on bad reads."""
from __future__ import annotations

import asyncio

from gtx2_bridge import metrics


def test_sensor_manifest_is_well_formed():
    keys = {s["key"] for s in metrics.sensor_manifest()}
    # the GB-parity health surface the design calls for
    assert {"heart_rate", "spo2", "sleep_minutes", "steps", "distance_m", "calories",
            "workout_summary"} <= keys
    for s in metrics.sensor_manifest():
        assert set(s) >= {"key", "name", "source", "binary", "unsupported"}
        assert s["source"] in ("health", "state", "link")


def test_battery_is_marked_unsupported():
    battery = next(s for s in metrics.sensor_manifest() if s["key"] == "battery")
    assert battery["unsupported"] is True     # no battery opcode exists on this firmware


def test_read_health_never_raises_on_empty_replies(make_dispatcher):
    _disp, fake = make_dispatcher()
    out = asyncio.run(metrics.read_health(fake))
    assert isinstance(out, dict)              # empty acks -> no metrics, but no crash
    # it issued one 0x0e read per sync category
    assert sum(1 for _f, op in fake.requests if op == 0x0e) == 7


def test_read_state_reports_connected_and_active_dial(make_dispatcher):
    _disp, fake = make_dispatcher(active="custom_id_25001.bin")
    out = asyncio.run(metrics.read_state(fake))
    assert out["connected"] is True
    assert out.get("active_dial") == "custom_id_25001.bin"
