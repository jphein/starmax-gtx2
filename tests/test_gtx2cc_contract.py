"""Dashboard ↔ integration contract test (Task 11).

The two Lovelace dashboards must bind ONLY to the `gtx2` integration's contract — no leftover
references to the retired `script.gtx2_*` / `input_*.gtx2_*` package helpers, every `gtx2_*` entity
id one the integration actually provides, and every `gtx2.*` service one it registers.

Strategy: `yaml.safe_load` proves each file still parses (an edit can't silently break structure),
then a raw-text regex scan enforces the three contract rules. Text-scanning (rather than walking the
parsed tree) is deliberate — the references that must NOT survive live inside Jinja template strings
(`states('input_text.gtx2_…')`) and inside the header doc-comments, both of which a structural walk
would miss. So the scan also forces the comment blocks to reflect the post-migration contract.

Pure-text + PyYAML only — no homeassistant. `const` is imported via the conftest package shim.
"""
from __future__ import annotations

import os
import re

import pytest

yaml = pytest.importorskip("yaml")

from custom_components.gtx2.const import ALL_SERVICES, METRICS, NODE_ENTITY_IDS, WATCHES

_TESTS_DIR = os.path.dirname(__file__)


def _repo(*parts):
    return os.path.join(_TESTS_DIR, "..", *parts)


# YAML files get the structural parse; the served card joins them for the text-scan checks so a
# stale ref in the card (which drives the dashboard actions) can't slip past CI (a review finding).
YAML_FILES = [_repo("dashboards", "gtx2-dashboard.yaml"), _repo("dashboards", "gtx2-dashboard-core.yaml")]
CARD_FILE = _repo("custom_components", "gtx2", "www", "gtx2-watch-card.js")
SCAN_FILES = YAML_FILES + [CARD_FILE]


def _ids(paths):
    return [os.path.basename(p) for p in paths]


# ---- contract sets, built from the integration's const tables (single source of truth) ----
def _hub_entities() -> set[str]:
    return {
        "sensor.gtx2_detected_watches", "sensor.gtx2_last_result",
        "binary_sensor.gtx2_any_present", "binary_sensor.gtx2_bridge_online",
        "switch.gtx2_gridwatts_face", "switch.gtx2_weatherface_live", "switch.gtx2_alarm_enabled",
        "text.gtx2_notify_title", "text.gtx2_notify_body", "text.gtx2_push_text",
        "select.gtx2_push_target", "select.gtx2_dial",
        "number.gtx2_alarm_index", "time.gtx2_alarm_time",
    }


def _watch_entities(w: str) -> set[str]:
    ents = {
        f"sensor.gtx2_{w}_room", f"sensor.gtx2_{w}_holder",
        f"binary_sensor.gtx2_{w}_connected", f"binary_sensor.gtx2_{w}_present",
        f"switch.gtx2_{w}_screen_break",
    }
    ents |= {f"sensor.gtx2_{w}_{m}" for m in METRICS}
    return ents


CONTRACT_ENTITIES = _hub_entities().union(*(_watch_entities(w) for w in WATCHES), set(NODE_ENTITY_IDS))
CONTRACT_SERVICES = set(ALL_SERVICES)

# Per-node online entities are an OPEN, options-driven set (a node added in the integration options
# creates binary_sensor.gtx2_node_<short>_online with no dashboard edit — the primary uses an
# auto-entities wildcard). So any `binary_sensor.gtx2_node_*` ref is contract-valid, not just the 5
# defaults; this prefix also absorbs the wildcard's partial-token match.
NODE_PREFIX = "binary_sensor.gtx2_node_"

# Refs that LOOK retired but are intentionally kept — they belong to lanes OUT OF SCOPE for this
# migration and SURVIVE package retirement, so the dashboards may reference them. Mirrors
# contract_diff.py's OUT_OF_SCOPE_HELPERS plus the two CFW-flash-handoff scripts (their package
# gtx2_spare_flash.yaml is not retired). Keep in sync if either grows.
PROTECTED_REFS = frozenset({
    # CFW/flash helpers (contract_diff.OUT_OF_SCOPE_HELPERS)
    "input_boolean.gtx2_spare_flash_window",
    "input_number.gtx2_spare_flash_interval",
    "input_number.gtx2_spare_flash_window_secs",
    "button.gtx2_bedroom_safe_mode",
    "button.gtx2_office_safe_mode",
    # CFW flash-handoff card scripts (gtx2_spare_flash.yaml — survives retirement)
    "script.gtx2_spare_free_for_flash",
    "script.gtx2_spare_free_for_flash_stop",
    # Live-kW feed helpers — DEFINED and owned by packages/gtx2_gridkw_live.yaml (a shipped
    # feature package, NOT retired). The dashboard's Live grid-kW card binds them legitimately.
    "input_boolean.gtx2_gridkw_live",
    "input_datetime.gtx2_gridkw_last_push",
})

# Retired package surface — must not appear ANYWHERE (bindings, Jinja, or comments).
RETIRED_RE = re.compile(
    r"\b(?:script|input_text|input_boolean|input_number|input_select|input_datetime)\.gtx2_[a-z0-9_]+")
# gtx2_* entity ids (integration-provided domains only). The `[a-z0-9_]+` tail means a Jinja regex
# literal like `binary_sensor.gtx2_.*_connected` is NOT captured (the `.` breaks the token).
ENTITY_RE = re.compile(
    r"\b(?:sensor|binary_sensor|switch|text|select|number|time)\.gtx2_[a-z0-9_]+")
# gtx2.* service calls in tap_action / card-action bindings.
SERVICE_RE = re.compile(r"(?:service|perform_action):\s*[\"']?gtx2\.([a-z_]+)")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@pytest.mark.parametrize("path", YAML_FILES, ids=_ids(YAML_FILES))
def test_dashboard_parses(path):
    doc = yaml.safe_load(_read(path))
    assert isinstance(doc, dict) and "views" in doc


@pytest.mark.parametrize("path", SCAN_FILES, ids=_ids(SCAN_FILES))
def test_no_retired_helper_or_script_refs(path):
    hits = sorted(set(RETIRED_RE.findall(_read(path))) - PROTECTED_REFS)
    assert not hits, f"{os.path.basename(path)} still references retired scripts/helpers: {hits}"


@pytest.mark.parametrize("path", SCAN_FILES, ids=_ids(SCAN_FILES))
def test_entity_refs_are_in_contract(path):
    refs = sorted(set(ENTITY_RE.findall(_read(path))))
    unknown = [e for e in refs if e not in CONTRACT_ENTITIES and not e.startswith(NODE_PREFIX)]
    assert not unknown, f"{os.path.basename(path)} binds gtx2_* entities not in contract: {unknown}"


@pytest.mark.parametrize("path", SCAN_FILES, ids=_ids(SCAN_FILES))
def test_service_refs_are_registered(path):
    refs = sorted(set(SERVICE_RE.findall(_read(path))))
    assert refs, f"{os.path.basename(path)} references no gtx2.* services — the rebinding is missing"
    unknown = [s for s in refs if s not in CONTRACT_SERVICES]
    assert not unknown, f"{os.path.basename(path)} calls gtx2.* services not in ALL_SERVICES: {unknown}"


def test_primary_has_node_autoentities():
    """Primary Status view auto-adds nodes via an auto-entities wildcard — new nodes appear with no
    dashboard edit. Guards the zero-edit behavior from silently regressing to a static list."""
    txt = _read(_repo("dashboards", "gtx2-dashboard.yaml"))
    assert "custom:auto-entities" in txt
    assert "binary_sensor.gtx2_node_*_online" in txt


def test_core_lists_default_node_entities():
    """-core (zero-custom-JS) has no auto-entities, so its static Nodes card must list exactly the
    integration's default node entities (const.NODE_ENTITY_IDS) — kept in sync as defaults change."""
    txt = _read(_repo("dashboards", "gtx2-dashboard-core.yaml"))
    for nid in NODE_ENTITY_IDS:
        assert nid in txt, f"-core Nodes card missing default node entity {nid}"


def test_compat_shim_routes_release_link():
    """BREAK-1: ha-packages/gtx2_compat.yaml must keep `script.gtx2_spare_release_link` alive (the
    out-of-scope CFW package gtx2_spare_flash.yaml calls it), routed to gtx2.release_link {watch:
    spare}. Guards against the shim's target silently drifting away from a registered service."""
    doc = yaml.safe_load(_read(_repo("packages", "gtx2_compat.yaml")))
    shim = doc["script"]["gtx2_spare_release_link"]
    step = shim["sequence"][0]
    assert step["action"] == "gtx2.release_link", step
    assert step["data"] == {"watch": "spare"}, step
    assert step["action"].split(".", 1)[1] in CONTRACT_SERVICES  # target is a registered gtx2.* service
