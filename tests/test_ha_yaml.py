"""Structural checks on the HA package yaml (skips if PyYAML isn't installed).

An authoritative load is also run with system python (which has PyYAML) during verification;
this locks the safety-critical structure into the suite wherever yaml is available.
"""
from __future__ import annotations

import os

import pytest

yaml = pytest.importorskip("yaml")

PKG = os.path.join(os.path.dirname(__file__), "..", "packages", "gtx2_watch.yaml")


@pytest.fixture(scope="module")
def doc():
    with open(PKG) as fh:
        return yaml.safe_load(fh)


def test_yaml_loads(doc):
    assert isinstance(doc, dict)
    # NB: no "automation" — the bridge's periodic time/weather automation was retired in the #21
    # cutover (see test_time_weather_periodic_sync_retired); the bridge package is now command-only.
    assert {"mqtt", "script", "input_text", "input_boolean"} <= set(doc)


def test_safe_command_scripts_exist(doc):
    scripts = doc["script"]
    for s in ("gtx2_find", "gtx2_notify", "gtx2_set_time", "gtx2_sync_health",
              "gtx2_send", "gtx2_activate", "gtx2_dial_list"):
        assert s in scripts, f"missing safe script {s}"


def test_danger_script_is_arm_gated_and_confirms(doc):
    danger = doc["script"]["gtx2_danger_send"]
    seq = danger["sequence"]
    # first step is a state condition on the arm boolean
    cond = seq[0]
    assert cond.get("condition") == "state"
    assert cond.get("entity_id") == "input_boolean.gtx2_arm_danger"
    assert cond.get("state") == "on"
    # the publish carries confirm:true
    assert any("confirm" in str(step.get("data", {}).get("payload", "")) for step in seq)


def test_no_safe_script_sends_confirm_true(doc):
    """Safe/tappable scripts must never carry confirm:true (that's danger-only)."""
    for name, s in doc["script"].items():
        if name == "gtx2_danger_send":
            continue
        blob = str(s)
        assert '"confirm": true' not in blob and "confirm': true" not in blob


def test_registry_and_health_sensors_present(doc):
    sensors = doc["mqtt"]["sensor"]
    uids = {s.get("unique_id") for s in sensors}
    assert "gtx2_registry" in uids
    assert {"gtx2_primary_heart_rate", "gtx2_primary_steps", "gtx2_primary_spo2"} <= uids
    # bridge availability binary_sensor
    bins = doc["mqtt"]["binary_sensor"]
    assert any(b.get("unique_id") == "gtx2_bridge_online" for b in bins)


def test_time_weather_periodic_sync_retired(doc):
    """#21 behavioural cutover: the bridge no longer DRIVES the periodic time/weather sync — that
    moved to the node-first wrappers (gtx2_periodic_sync.yaml → script.gtx2_<watch>_sync_time /
    _push_weather) to avoid DOUBLE-SENDING. So the bridge package must NOT carry a
    gtx2_time_weather_sync automation; the host-bridge stays an on-demand fallback only.
    (Guards against the automation being reintroduced and double-sending.)"""
    autos = doc.get("automation", [])
    ids = {a.get("id") for a in autos}
    assert "gtx2_time_weather_sync" not in ids
