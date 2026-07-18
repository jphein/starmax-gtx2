"""Dispatcher: dry-run frame planning, the safety gate, and live driving via a fake client."""
from __future__ import annotations

import asyncio

import pytest

from gtx2_bridge import catalog
from gtx2_bridge.config import BridgeConfig

MAC = "AA:BB:CC:11:22:33"


# --------------------------------------------------------------------------- dry-run
def test_dryrun_find_plans_start_and_stop(make_dispatcher):
    disp, _ = make_dispatcher()
    r = asyncio.run(disp.handle("find", params={"duration": 10}, dry_run=True))
    assert r["ok"] and r["count"] == 2 and r["tier"] == "green"
    assert r["frames"][0].startswith("aa") or len(r["frames"][0]) > 0  # a C1 frame


def test_dryrun_notify_plans_dplane_push(make_dispatcher):
    disp, _ = make_dispatcher()
    r = asyncio.run(disp.handle("notify", params={"title": "Hi", "body": "there"}, dry_run=True))
    assert r["ok"] and r["count"] > 4         # D3 + D1 + D2* + D4
    assert "custom_id_25001.bin" in r["summary"]


def test_notify_requires_title(make_dispatcher):
    disp, _ = make_dispatcher()
    r = asyncio.run(disp.handle("notify", params={"body": "no title"}, dry_run=True))
    assert not r["ok"] and "title" in r["error"]


def test_dryrun_every_catalog_command_builds(make_dispatcher, tmp_path):
    """Every exposed command must at least dry-run without raising (frames or a summary)."""
    from gtx2_bridge import faces
    dial_file = tmp_path / "face.blob"
    dial_file.write_bytes(faces.build_notification_blob("x"))   # a valid dial for dial-push
    disp, _ = make_dispatcher()
    sample = {"file": str(dial_file), "hour": 7, "minute": 30, "height": 170, "weight": 65,
              "birth_year": 1990, "date": "2026-07-14", "time": "08:00", "title": "x"}
    for name in catalog.CATALOG:
        r = asyncio.run(disp.handle(name, params=dict(sample), dry_run=True))
        assert r["ok"], f"dry-run {name} failed: {r.get('error')}"


def test_unknown_command_errors(make_dispatcher):
    disp, _ = make_dispatcher()
    r = asyncio.run(disp.handle("nope", dry_run=True))
    assert not r["ok"] and "unknown command" in r["error"]


# --------------------------------------------------------------------------- safety gate
def test_red_command_refused_live_without_confirm(make_dispatcher):
    disp, fake = make_dispatcher()
    r = asyncio.run(disp.handle("dnd", mac=MAC, dry_run=False, confirm=False))
    assert not r["ok"] and "DANGER" in r["error"]
    assert fake.sent == []                    # never connected/sent


def test_red_command_dryrun_allowed(make_dispatcher):
    disp, _ = make_dispatcher()
    r = asyncio.run(disp.handle("dnd", dry_run=True))
    assert r["ok"] and r["tier"] == "red"     # preview is safe; only live is gated


def test_red_command_runs_live_with_confirm(make_dispatcher):
    disp, fake = make_dispatcher()
    r = asyncio.run(disp.handle("dnd", mac=MAC, dry_run=False, confirm=True))
    assert r["ok"] and fake.sent, "confirmed danger command should send"


# --------------------------------------------------------------------------- live drivers
def test_live_find_sends_two_frames_and_disconnects(make_dispatcher):
    disp, fake = make_dispatcher()
    r = asyncio.run(disp.handle("find", mac=MAC, params={"duration": 0}, dry_run=False))
    assert r["ok"] and len(fake.sent) == 2 and fake.disconnected


def test_live_notify_streams_dplane(make_dispatcher):
    disp, fake = make_dispatcher()
    r = asyncio.run(disp.handle("notify", mac=MAC, params={"title": "Hi"}, dry_run=False))
    assert r["ok"]
    assert fake.sent[0][0] == 0xD3 and fake.sent[-1][0] == 0xD4   # dial-push bulk plane
    assert r["result"]["confirmed"] is True


def test_live_weather_sends_enable_then_weather(make_dispatcher):
    disp, fake = make_dispatcher()
    r = asyncio.run(disp.handle("weather", mac=MAC,
                                params={"temp": 21, "hi": 24, "lo": 17, "city": "Anytown"},
                                dry_run=False))
    assert r["ok"] and len(fake.sent) == 2      # 0x04 enable + 0x12 weather


def test_live_set_time_requests_ack(make_dispatcher):
    disp, fake = make_dispatcher()
    r = asyncio.run(disp.handle("set-time", mac=MAC, dry_run=False))
    assert r["ok"] and r["result"]["acked"] is True
    assert any(op == 0x02 for _f, op in fake.requests)


def test_live_dial_list_parses_active(make_dispatcher):
    disp, _ = make_dispatcher(active="custom_id_42.bin")
    r = asyncio.run(disp.handle("dial-list", mac=MAC, dry_run=False))
    assert r["ok"] and r["result"]["active_dial"] == "custom_id_42.bin"


def test_live_generic_send(make_dispatcher):
    disp, fake = make_dispatcher()
    r = asyncio.run(disp.handle("feature-bitmap", mac=MAC, dry_run=False))
    assert r["ok"] and len(fake.sent) == 1


def test_placeholder_mac_refused_live(make_dispatcher):
    # config default MAC is the placeholder; no explicit mac -> refuse live
    disp, fake = make_dispatcher(config=BridgeConfig())
    r = asyncio.run(disp.handle("find", dry_run=False))
    assert not r["ok"] and "MAC" in r["error"] and fake.sent == []
