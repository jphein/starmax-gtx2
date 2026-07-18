"""MQTT payload decoding (the pure routing half — no broker needed)."""
from __future__ import annotations

import json

from gtx2_bridge.mqtt_bridge import decode_command


def test_decode_json_command():
    spec = decode_command(json.dumps(
        {"command": "notify", "mac": "AA:BB:CC:11:22:33",
         "params": {"title": "Hi"}, "confirm": True, "dry_run": True}).encode())
    assert spec["command"] == "notify" and spec["mac"] == "AA:BB:CC:11:22:33"
    assert spec["params"] == {"title": "Hi"}
    assert spec["confirm"] is True and spec["dry_run"] is True


def test_decode_bare_string_is_a_command():
    spec = decode_command(b"find")
    assert spec["command"] == "find" and spec["params"] == {}
    assert spec["confirm"] is False and spec["dry_run"] is False


def test_decode_empty_and_garbage():
    assert decode_command(b"") == {}
    assert decode_command(b"{not json") == {}
    assert decode_command(b"[1,2,3]") == {}          # JSON but not an object


def test_decode_defaults_missing_fields():
    spec = decode_command(b'{"command": "weather"}')
    assert spec["command"] == "weather" and spec["params"] == {}
    assert spec["confirm"] is False
