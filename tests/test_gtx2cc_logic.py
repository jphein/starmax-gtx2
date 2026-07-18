"""Pure decision logic for the gtx2 custom component (Task 2).

These tests import the component's `logic` module WITHOUT homeassistant (repo
convention). The import shim in conftest.py registers `custom_components.gtx2`
with an explicit __path__ so submodules load without running the HA-heavy
package __init__.
"""
import base64
import sys
from pathlib import Path

import pytest

from custom_components.gtx2 import logic
from custom_components.gtx2 import render as gtx2_render
from custom_components.gtx2.const import COND_MAP, NODE_ROOMS

try:
    from gtx2_bridge import faces as _faces
except Exception:  # noqa: BLE001 — canonical renderer optional in the pure-pytest env
    _faces = None

_STATIC_WATTS = [0, 14, 900, 1158, 11999, 12800]


def _is_on(on_set):
    return lambda eid: eid in on_set


def test_holder_last_match_wins():
    on = {"binary_sensor.gtx2_bedroom_daily_connected",
          "binary_sensor.gtx2_studio_daily_connected"}
    assert logic.resolve_holder("daily", _is_on(on), list(NODE_ROOMS)) == "gtx2_studio"


def test_holder_none():
    assert logic.resolve_holder("daily", _is_on(set()), list(NODE_ROOMS)) is None


def test_room_for_matches_presence_yaml():
    on = {"binary_sensor.gtx2_office_spare_connected"}
    assert logic.room_for("spare", _is_on(on), NODE_ROOMS) == "Office / Laundry"
    assert logic.room_for("spare", _is_on(set()), NODE_ROOMS) == "Away"


def test_metric_source_holds_when_away():
    # room known -> holder's metric entity id; room Away -> None (caller keeps last value)
    assert (logic.metric_source("daily", "Bedroom", "heart_rate", NODE_ROOMS)
            == "sensor.gtx2_bedroom_daily_heart_rate")
    assert logic.metric_source("daily", "Away", "heart_rate", NODE_ROOMS) is None


def test_weather_frame_converts_f_to_c_and_maps_condition():
    f = logic.weather_frame(state="rainy", temp_f=76.0, hi_f=93.0, lo_f=73.0)
    assert f == {"temp_current": 24, "temp_max": 34, "temp_min": 23,
                 "condition": 6, "city": "Home"}


def test_weather_frame_default_condition_is_clear_not_rain():
    assert logic.weather_frame("some-new-state", 50, 50, 50)["condition"] == 8


def test_weather_frame_unavailable_returns_none():
    for s in ("unknown", "unavailable", "none", None):
        assert logic.weather_frame(s, 76, 93, 73) is None


def test_chunk_blob_roundtrip():
    blob = bytes(range(256)) * 100          # 25600 bytes
    chunks = logic.chunk_blob(blob, raw_chunk=6144)
    assert [c["seq"] for c in chunks] == [0, 1, 2, 3, 4]
    assert all(c["total_len"] == len(blob) for c in chunks)
    assert b"".join(base64.b64decode(c["b64"]) for c in chunks) == blob


def test_gridwatts_deadband():
    # pushes when |Δ| >= 100 W AND >= 90 s since last push, or 120 s heartbeat elapsed
    assert logic.gridwatts_should_push(last_w=1000, new_w=1150, last_push_ts=0, now_ts=95)
    assert not logic.gridwatts_should_push(1000, 1050, 0, 95)          # inside deadband
    assert not logic.gridwatts_should_push(1000, 1300, 0, 60)          # too soon
    assert logic.gridwatts_should_push(1000, 1000, 0, 121)             # heartbeat


def test_node_suffix_parity():
    from custom_components.gtx2.const import NODE_SUFFIX
    # Engine parity with gtx2_command_routing.yaml — sync_time drives the node's set_time service.
    assert NODE_SUFFIX["sync_time"] == "set_time"
    for a in ("buzz", "stop_buzz", "set_time_custom", "read_health", "read_state", "release_link",
              "push_weather", "activate", "set_alarm", "switch_dial", "push_face"):
        assert NODE_SUFFIX[a] == a


def test_all_services_is_stable_set():
    from custom_components.gtx2.const import ALL_SERVICES
    assert len(ALL_SERVICES) == len(set(ALL_SERVICES)) == 19
    for s in ("buzz", "sync_time", "set_time_custom", "push_weather", "set_alarm", "switch_dial",
              "push_text", "push_text_label", "push_notification", "push_face",
              "media_play_pause", "media_next", "media_prev", "find_my_phone"):
        assert s in ALL_SERVICES


def test_fallback_payloads_match_bridge_contract():
    assert logic.fallback_payload("buzz", "AA:BB", {}) == \
        {"command": "find", "mac": "AA:BB", "params": {"duration": 5}}
    assert logic.fallback_payload("sync_time", "AA:BB", {}) == \
        {"command": "set-time", "mac": "AA:BB"}
    assert logic.fallback_payload("read_health", "AA:BB", {}) == \
        {"command": "sync-health", "mac": "AA:BB"}
    assert logic.fallback_payload("activate", "AA:BB", {}) == \
        {"command": "activate", "mac": "AA:BB"}
    wf = {"temp_current": 24, "temp_max": 34, "temp_min": 23, "condition": 6, "city": "Home"}
    assert logic.fallback_payload("push_weather", "AA:BB", wf) == \
        {"command": "weather", "mac": "AA:BB",
         "params": {"city": "Home", "temp": 24, "hi": 34, "lo": 23, "condition": 6}}
    assert logic.fallback_payload("stop_buzz", "AA:BB", {}) is None    # node-only


# --- Task 7: vendored static renderer -------------------------------------------------------
# The watch-truth invariant (pixel-parity ruling 2026-07-15): identical DECODED pixels + LZ4 match
# length <= GRID_STATIC_MAX_MATCH (the GTX2's minimal decoder garbles longer matches — HW-proven).
# Byte-parity vs the canonical is the additional vendor-drift guard, gated on both sides being
# capped (it holds where both use lz4.block; the cramjam seam is covered in
# test_gtx2cc_render_seam.py, whose preview stream may legally byte-differ).
def _max_match_len(block: bytes) -> int:
    i, mx, n = 0, 0, len(block)
    while i < n:
        token = block[i]; i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = block[i]; i += 1; lit += b
                if b != 255:
                    break
        i += lit
        if i >= n:
            break
        i += 2
        m = (token & 0x0F) + 4
        if (token & 0x0F) == 15:
            while True:
                b = block[i]; i += 1; m += b
                if b != 255:
                    break
        mx = max(mx, m)
    return mx


@pytest.mark.parametrize("w", _STATIC_WATTS)
def test_static_render_max_match_capped(w):
    # Every image asset's LZ4 block must respect the HW-proven cap or the watch renders garbage.
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / "starmax-client"))
    from starmax_client import dialfmt
    blob = gtx2_render.build_grid_static_blob(w)
    for a in dialfmt.parse_blob(blob).assets:
        if a.name.endswith((".bmp", ".png")):
            mm = _max_match_len(a.data[4:])
            assert mm <= gtx2_render.GRID_STATIC_MAX_MATCH, \
                f"{w} W {a.name}: max match {mm} > cap {gtx2_render.GRID_STATIC_MAX_MATCH}"


@pytest.mark.skipif(_faces is None or not hasattr(_faces, "build_grid_static_blob"),
                    reason="canonical faces.build_grid_static_blob not present in this tree yet")
@pytest.mark.parametrize("w", _STATIC_WATTS)
def test_static_render_decode_equality(w):
    # Decoded pixels + asset structure identical to the canonical (the invariant the watch sees).
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / "starmax-client"))
    from starmax_client import dialfmt, dialtranscode
    A = dialfmt.parse_blob(gtx2_render.build_grid_static_blob(w)).assets
    B = dialfmt.parse_blob(_faces.build_grid_static_blob(w)).assets
    assert [a.name for a in A] == [b.name for b in B]
    for a, b in zip(A, B):
        if a.name.endswith((".bmp", ".png")):
            assert dialtranscode.decode_image(a.data) == dialtranscode.decode_image(b.data), \
                f"{w} W {a.name}: decoded pixels diverge from canonical"
        else:
            assert a.data == b.data, f"{w} W {a.name}: manifest asset diverges from canonical"


@pytest.mark.skipif(_faces is None or not hasattr(_faces, "cap_lz4_matches"),
                    reason="canonical faces not match-capped yet — byte-parity gated until it is")
@pytest.mark.parametrize("w", _STATIC_WATTS)
def test_static_render_byte_parity(w):
    # Vendor-drift guard: byte-identical to the canonical when both sides are capped (lz4 env).
    assert gtx2_render.build_grid_static_blob(w) == _faces.build_grid_static_blob(w)


@pytest.mark.parametrize("w", _STATIC_WATTS)
def test_static_render_within_chunk_ceiling(w):
    # At the HW-safe LZ4 cap=512 the gauge is ~9-10 KB — OVER the old 8 KB /local/ OOM ceiling, which
    # is exactly why delivery is the CHUNKED D-plane path (facepush), not the /local/ url path. This is
    # now just an upper sanity bound (not the delivery ceiling): a well-formed capped face is a few KB,
    # never OOM-huge. See GRID_STATIC_MAX_MATCH + facepush's generous chunked sanity cap.
    blob = gtx2_render.build_grid_static_blob(w)
    assert len(blob) <= 16384, f"{w} W -> {len(blob)} B — unexpectedly large for a capped static face"


# --- per-node entities (options-driven "auto-add nodes") ------------------------------------
def test_node_short_strips_prefix():
    from custom_components.gtx2.const import node_short
    assert node_short("gtx2_bedroom") == "bedroom"
    assert node_short("gtx2_office") == "office"
    assert node_short("weird") == "weird"          # no gtx2_ prefix -> unchanged


def test_node_entity_ids_derivable_from_nodes():
    from custom_components.gtx2.const import NODE_ENTITY_IDS, node_entity_ids, NODE_ROOMS
    ids = node_entity_ids(NODE_ROOMS)
    assert "binary_sensor.gtx2_node_bedroom_online" in ids
    assert "binary_sensor.gtx2_node_office_online" in ids
    assert len(ids) == len(NODE_ROOMS) == 5
    assert NODE_ENTITY_IDS == ids                  # static contract list == derived for the defaults
    # arbitrary node set stays derivable (auto-add: a new node -> a new entity id)
    assert node_entity_ids({"gtx2_garage": "Garage"}) == ["binary_sensor.gtx2_node_garage_online"]


def _state_of(states):
    return lambda eid: states.get(eid)


def test_node_online_true_when_any_source_present_and_available():
    from custom_components.gtx2 import logic
    # a deployed node's connected source exists and is 'off' (linked to no watch) -> ONLINE
    st = _state_of({"binary_sensor.gtx2_bedroom_daily_connected": "off"})
    assert logic.node_online("gtx2_bedroom", ["daily", "spare", "watch3"], st) is True
    # 'on' also online
    st = _state_of({"binary_sensor.gtx2_office_watch3_connected": "on"})
    assert logic.node_online("gtx2_office", ["daily", "spare", "watch3"], st) is True


def test_node_online_false_when_sources_absent_or_unavailable():
    from custom_components.gtx2 import logic
    # never-deployed node: sources absent
    assert logic.node_online("gtx2_studio", ["daily", "spare"], _state_of({})) is False
    # offline node: sources present but 'unavailable'
    st = _state_of({"binary_sensor.gtx2_studio_daily_connected": "unavailable",
                    "binary_sensor.gtx2_studio_spare_connected": "unavailable"})
    assert logic.node_online("gtx2_studio", ["daily", "spare"], st) is False


def test_node_holding_lists_watches_held_by_that_node():
    from custom_components.gtx2 import logic
    holder = {"daily": "gtx2_office", "spare": "gtx2_office", "watch3": None}
    holder_of = lambda w: holder[w]
    assert logic.node_holding("gtx2_office", ["daily", "spare", "watch3"], holder_of) == ["daily", "spare"]
    assert logic.node_holding("gtx2_studio", ["daily", "spare", "watch3"], holder_of) == []


# --- install-confirm gate (chunked-push verification contract) ------------------------------
def test_parse_install_status():
    from custom_components.gtx2 import logic
    assert logic.parse_install_status("25041:ok:b735") == (25041, "ok")
    assert logic.parse_install_status("25041:fail:0000") == (25041, "fail")
    assert logic.parse_install_status("25041:OK:B735") == (25041, "ok")   # status case-insensitive
    assert logic.parse_install_status("garbage") == (None, None)
    assert logic.parse_install_status("") == (None, None)
    assert logic.parse_install_status(None) == (None, None)
    assert logic.parse_install_status("x:ok:1") == (None, None)           # non-int dial


def test_classify_install_absent_vs_pending_vs_ok_vs_fail():
    from custom_components.gtx2 import logic
    # absent: entity not present (None) -> pre-reflash, caller must NOT retry
    assert logic.classify_install(None, 25041) == logic.INSTALL_ABSENT
    # transient entity states -> pending (keep waiting, distinct from absent)
    assert logic.classify_install("unavailable", 25041) == logic.INSTALL_PENDING
    assert logic.classify_install("unknown", 25041) == logic.INSTALL_PENDING
    # matching dial + status
    assert logic.classify_install("25041:ok:b735", 25041) == logic.INSTALL_OK
    assert logic.classify_install("25041:fail:0000", 25041) == logic.INSTALL_FAIL
    # a result for a DIFFERENT dial is not our confirmation
    assert logic.classify_install("25023:ok:aaaa", 25041) == logic.INSTALL_PENDING
    assert logic.classify_install("25041:ok:b735", 25023) == logic.INSTALL_PENDING
    # present but unparseable -> pending (wait; never misfire ok/fail)
    assert logic.classify_install("garbage", 25041) == logic.INSTALL_PENDING


# --- per-node push serialization (radio-contention fix) -------------------------------------
def test_get_or_create_idempotent():
    from custom_components.gtx2 import logic
    reg = {}
    calls = []
    def factory():
        calls.append(1)
        return object()
    a = logic.get_or_create(reg, "x", factory)
    b = logic.get_or_create(reg, "x", factory)
    c = logic.get_or_create(reg, "y", factory)
    assert a is b            # same key -> same object (one lock per node)
    assert a is not c        # different key -> different object (distinct radios)
    assert len(calls) == 2   # factory runs once per distinct key


def test_per_node_lock_serializes_same_node_but_not_across_nodes():
    import asyncio
    from custom_components.gtx2 import logic
    reg = {}
    def nlock(n):
        return logic.get_or_create(reg, n, asyncio.Lock)

    async def scenario():
        active = {}
        overlaps = {"n": 0}

        async def work(node):
            async with nlock(node):
                active[node] = active.get(node, 0) + 1
                if active[node] > 1:
                    overlaps["n"] += 1          # two holders of the SAME node lock at once = bug
                await asyncio.sleep(0.01)
                active[node] -= 1

        # same node: the two tasks must NOT overlap (serialized)
        await asyncio.gather(work("A"), work("A"))
        same_overlap = overlaps["n"]

        # different nodes: both run (independent locks) — record order-independent
        ran = []
        async def w2(node):
            async with nlock(node):
                ran.append(node)
                await asyncio.sleep(0.01)
        await asyncio.gather(w2("B"), w2("C"))
        return same_overlap, sorted(ran)

    same_overlap, ran = asyncio.run(scenario())
    assert same_overlap == 0                     # same-node work never overlapped
    assert ran == ["B", "C"]                     # different nodes both ran
    assert nlock("A") is nlock("A")              # stable per-node lock
    assert nlock("A") is not nlock("B")          # distinct across nodes


# --- live-kW RTC ownership gate (scheduler must not clobber day=kW with the real date) --------
def test_gridkw_owns_rtc():
    from custom_components.gtx2 import logic
    # enabled + matching target -> OWNED: scheduler skips sync_time / push_weather / gauge for it
    assert logic.gridkw_owns_rtc("daily", True, "daily") is True
    # feature OFF -> not owned (normal periodic sync resumes)
    assert logic.gridkw_owns_rtc("daily", False, "daily") is False
    # different watch -> not owned (only the target watch's RTC is protected)
    assert logic.gridkw_owns_rtc("spare", True, "daily") is False
    # no target configured -> not owned
    assert logic.gridkw_owns_rtc("daily", True, None) is False
    # single-string path: exact match only (no substring false-positive)
    assert logic.gridkw_owns_rtc("d", True, "daily") is False


def test_gridkw_owns_rtc_multi_target():
    from custom_components.gtx2 import logic
    # MULTI-TARGET (watch3 mirrors daily): a collection protects EVERY member
    targets = {"daily", "watch3"}
    assert logic.gridkw_owns_rtc("daily", True, targets) is True
    assert logic.gridkw_owns_rtc("watch3", True, targets) is True   # watch3 now protected too
    assert logic.gridkw_owns_rtc("spare", True, targets) is False   # non-target untouched
    assert logic.gridkw_owns_rtc("daily", False, targets) is False  # gate off -> normal sync
    assert logic.gridkw_owns_rtc("daily", True, ()) is False        # empty collection -> nobody owned
    assert logic.gridkw_owns_rtc("watch3", True, ["daily", "watch3"]) is True  # list works too
