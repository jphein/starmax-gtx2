"""The command catalog — the authoritative, tiered map of every command the gateway exposes.

This is the single source of truth for (a) what the bridge can do, (b) how safe each command is,
and (c) what HA should surface. It is emitted verbatim as the machine-readable **manifest**
(``python -m gtx2_bridge manifest``) that the dashboard binds entities/services against, so
naming reconciliation is trivial.

Safety tiers
------------
* **green**  — [CAP] capture-verified opcode AND payload. Safe to tap, no ``--force``, no confirm.
* **yellow** — [SCHEMA/INFERRED] but non-destructive (display / reminder / dial settings). Tappable,
               but sent with ``force=True`` (the library gates these) — a wrong guess just no-ops.
* **red**    — destructive or wrong-layer/unverified-with-side-effects (firmware flash, the
               classic-BT call/music/camera stubs, DnD). **NOT** on the default dashboard; requires
               an explicit ``confirm`` AND ``force``. This is the accidental-brick guard.

Tiers are cross-checked against the library's own audit sets
(:data:`starmax_client.commands.CAP_OPCODES` / :data:`GATED_COMMANDS`) by
``tests/test_catalog.py`` so this file cannot silently drift from the protocol audit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from starmax_client.commands import base, files
from starmax_client.commands.base import (build_alarm_get, build_alarm_set,
                                          build_find_device, build_set_time, build_weather)
from starmax_client.commands.settings import (build_aod, build_date_format, build_device_state,
                                              build_dnd, build_drink_water, build_event_reminders,
                                              build_feature_bitmap, build_sedentary,
                                              build_setting_query, build_sport_goals,
                                              build_user_profile, build_world_clock,
                                              build_wrist_raise)
from starmax_client.commands.notify import (build_camera_control, build_incoming_call,
                                            build_music_state)
from starmax_client.commands.files import build_dial_list_request, build_dial_switch

GREEN, YELLOW, RED = "green", "yellow", "red"

# Commands that MUST be red regardless of the library's gate (destructive or wrong-layer with
# side effects). The design brief pins these explicitly.
RED_COMMANDS = frozenset({"flash-firmware", "dnd", "music", "camera", "call"})


@dataclass
class Command:
    """One exposed command. ``builder`` is the pure library frame builder (None for specials
    with a bespoke driver, e.g. buzz/notify/weather/activate/sync-health)."""
    name: str
    group: str
    tier: str
    kind: str                 # buzz|notify|set-time|weather|activate|sync-health|dial-list|
                              #  dial-push|request|send|flash
    summary: str
    builder: Optional[Callable] = None
    opcode: Optional[int] = None
    ha_expose: bool = True     # surfaced in the SAFE (default) dashboard set
    needs_confirm: bool = False
    needs_force: bool = False
    # post node-migration ownership (docs/gtx2-bridge-reduced-role.md): node|render|classic-notify|host
    role: str = "host"
    # node-delegated: the bridge runs it ONLY when no node currently holds the watch
    fallback_only: bool = False
    params: Dict[str, str] = field(default_factory=dict)

    def as_manifest(self) -> dict:
        return {"name": self.name, "group": self.group, "tier": self.tier, "kind": self.kind,
                "summary": self.summary, "opcode": self.opcode, "ha_expose": self.ha_expose,
                "needs_confirm": self.needs_confirm, "needs_force": self.needs_force,
                "role": self.role, "fallback_only": self.fallback_only,
                "params": self.params}


def _c(*a, **k) -> Command:
    c = Command(*a, **k)
    if c.tier == RED:
        c.needs_confirm = True
        c.needs_force = True
        c.ha_expose = False
    elif c.tier == YELLOW:
        c.needs_force = True
    return c


# ---------------------------------------------------------------------------- the catalog
CATALOG: Dict[str, Command] = {c.name: c for c in [
    # ---------- GREEN: headline / [CAP] ----------
    _c("find", "core", GREEN, "buzz", "Ring/buzz the watch to find it (0x18)",
       opcode=base.OP_FIND_DEVICE, params={"duration": "buzz seconds (default 5)"}),
    _c("notify", "core", GREEN, "notify",
       "Render text onto a minimal watch-face and push it (dial-push) — the notification system",
       params={"title": "headline", "body": "message", "footer": "small bottom line (e.g. time)",
               "icon": "optional PNG path", "bg": "#RRGGBB background", "fg": "#RRGGBB text",
               "accent": "#RRGGBB title", "dial_id": "custom dial id (default 25001)",
               "confirm_push": "confirm via dial-list read (default true)"}),
    _c("set-time", "core", GREEN, "set-time", "Sync the watch clock to now (0x02)",
       opcode=base.OP_SET_TIME),
    _c("weather", "weather", GREEN, "weather", "Push weather from an HA entity (0x04 enable + 0x12)",
       opcode=base.OP_WEATHER,
       params={"temp": "current C", "hi": "today max C", "lo": "today min C",
               "condition": "watch code (6=clear observed)", "city": "label (PII-free)",
               "month": "", "day": "", "hour": "", "minute": "", "pressure": "hPa",
               "enable": "send 0x04 first (default true)"}),
    _c("activate", "core", GREEN, "activate",
       "Full setup handshake — take a fresh watch off its pairing screen (0x22/05/16→time→0e/04/03)"),
    _c("sync-health", "health", GREEN, "sync-health",
       "Pull health/history record dates + sizes (0x0e read)",
       params={"category": "one syncType 0/1/2/3/4/5/7 (default: all)"}),
    _c("dial-list", "files", GREEN, "dial-list", "Read installed dials + the active face (0x16)",
       builder=build_dial_list_request, opcode=files.OP_DIAL_LIST),
    _c("dial-push", "dials", GREEN, "dial-push",
       "Install a dial .bin (ZIP or native blob) from a file on the bridge host — advanced",
       params={"file": "path to a dial .bin/blob on the bridge host", "dial_id": "custom id"}),
    _c("feature-bitmap", "settings", GREEN, "send", "Enable notifications/features bitmap (0x04)",
       builder=build_feature_bitmap, opcode=0x04),
    _c("user-profile", "settings", GREEN, "user-profile",
       "Set user profile + step/distance goals (0x03)", opcode=0x03,
       params={"height": "cm", "weight": "kg", "birth_year": "", "sex": "1=male/0=female",
               "step_goal": "", "distance_goal": "m"}),
    _c("alarm-set", "base", GREEN, "alarm-set", "Set one alarm (0x07)", opcode=base.OP_ALARM,
       params={"index": "", "hour": "", "minute": "", "enabled": "default true"}),
    _c("alarm-get", "base", GREEN, "request", "Read alarms (0x07)",
       builder=build_alarm_get, opcode=base.OP_ALARM),
    _c("setting-query", "settings", GREEN, "request", "Read a generic setting value (0x22)",
       builder=build_setting_query, opcode=0x22, params={"key": "setting key (default 1)"}),

    # ---------- YELLOW: schema/inferred, non-destructive, tap-with-force ----------
    _c("dial-switch", "files", YELLOW, "send", "[inferred] switch active face to an installed id",
       builder=build_dial_switch, opcode=files.OP_DIAL_SET, params={"dial_id": "installed id"}),
    _c("aod", "settings", YELLOW, "send", "[schema] always-on-display schedule",
       builder=build_aod, params={"on": "bool", "style": "int", "start_h": "", "start_m": "",
                                  "end_h": "", "end_m": ""}),
    _c("world-clock", "settings", YELLOW, "send", "[schema] set world-clock cities",
       builder=build_world_clock, params={"city_ids": "list[int] of city ids"}),
    _c("date-format", "settings", YELLOW, "send", "[schema] set date format",
       builder=build_date_format, params={"date_format": "date-format code (int)"}),
    _c("sport-goals", "settings", YELLOW, "send", "[schema] set daily sport goals",
       builder=build_sport_goals, params={"steps": "int", "calories_kcal": "int",
                                          "distance_km": "float"}),
    _c("device-state", "settings", YELLOW, "send",
       "[schema] time/unit/temp format, language, backlight, wrist-raise", builder=build_device_state,
       params={"time_format": "0/1", "unit_format": "0/1", "temp_format": "0/1",
               "language": "int", "backlight_seconds": "int", "wrist_raise": "bool"}),
    _c("wrist-raise", "settings", YELLOW, "send", "[schema] toggle raise-to-wake",
       builder=build_wrist_raise, params={"on": "bool"}),
    _c("sedentary", "settings", YELLOW, "send", "[schema] sedentary/long-sit reminder",
       builder=build_sedentary, params={"on": "bool", "start_h": "", "start_m": "",
                                        "end_h": "", "end_m": "", "interval_min": ""}),
    _c("drink-water", "settings", YELLOW, "send", "[schema] drink-water reminder",
       builder=build_drink_water, params={"on": "bool", "start_h": "", "start_m": "",
                                          "end_h": "", "end_m": "", "interval_min": ""}),
    _c("event-reminders", "settings", YELLOW, "send", "[schema] set one event reminder",
       builder=build_event_reminders, params={"date": "YYYY-MM-DD", "time": "HH:MM",
                                              "content": ""}),

    # ---------- RED: DANGER — not tappable, confirm + force required ----------
    _c("dnd", "settings", RED, "send", "[schema/wrong-layer] do-not-disturb / quiet hours",
       builder=build_dnd),
    _c("call", "notify", RED, "send", "[unverified] incoming-call notification (classic-BT layer)",
       builder=build_incoming_call, params={"caller": ""}),
    _c("music", "notify", RED, "send", "[unverified] music-state control (classic-BT layer)",
       builder=build_music_state),
    _c("camera", "notify", RED, "send", "[unverified] camera-shutter control (classic-BT layer)",
       builder=build_camera_control),
    _c("flash-firmware", "files", RED, "flash",
       "DESTRUCTIVE — stream an OTA firmware image. Brick risk. Never a one-tap button.",
       ha_expose=False, params={"file": "OTA image path on the bridge host"}),
]}


# --------------------------------------------------------------------------- role labels
# The bridge is demoted (docs/gtx2-bridge-reduced-role.md, #20): node-capable commands route to the
# ESP node holding the watch; the bridge runs them only as a labeled last-resort. Its permanent jobs
# are the off-node render (`render`) and the LE-impossible classic-BT text-notify (`classic-notify`).
# Node-capable commands route to the node holding the watch (#35 wrappers). Two sub-classes,
# matching what the deployed wrappers actually do (#21 ruling b):
#  * NODE + host-bridge last-resort: the wrapper carries an `fb_script`, so the bridge runs the
#    command when no node holds the watch (fallback_only=True).
#  * NODE-ONLY: the wrapper has no `fb_script` -> it no-ops with a warning when no node holds the
#    watch (fallback_only=False). Accepted by design: alarm/dial ops aren't time-critical and we
#    are retiring host-bridge scripts, not adding them.
_NODE_WITH_FALLBACK = frozenset({"find", "activate", "set-time", "weather", "sync-health"})
_NODE_ONLY = frozenset({"alarm-set", "dial-switch", "dial-list"})   # node-only by design (#21)
_RENDER = frozenset({"notify"})     # render half is permanent; the node does the D-plane push (#37)


def _apply_roles() -> None:
    for name, c in CATALOG.items():
        if name in _NODE_WITH_FALLBACK:
            c.role, c.fallback_only = "node", True
        elif name in _NODE_ONLY:
            c.role, c.fallback_only = "node", False   # node-only: no host-bridge last-resort
        elif name in _RENDER:
            c.role, c.fallback_only = "render", False
        else:
            c.role, c.fallback_only = "host", False


_apply_roles()


def node_delegated() -> List[str]:
    """Commands a node holding the watch owns; the bridge is only their last-resort."""
    return sorted(n for n, c in CATALOG.items() if c.role == "node")


def bridge_permanent() -> List[str]:
    """The bridge's irreducible jobs a C3 node physically cannot do (render / classic-notify)."""
    return sorted(n for n, c in CATALOG.items() if c.role in ("render", "classic-notify"))


def host_commands() -> List[str]:
    """Bridge-only commands with no node method yet (advanced/file ops + danger tier)."""
    return sorted(n for n, c in CATALOG.items() if c.role == "host")


def fallback_only_commands() -> List[str]:
    """Node-delegated commands with a host-bridge last-resort (bridge runs them only when no node
    holds the watch)."""
    return sorted(n for n, c in CATALOG.items() if c.role == "node" and c.fallback_only)


def node_only_commands() -> List[str]:
    """Node-delegated commands with NO host-bridge fallback (no-op + warn when no node holds it)."""
    return sorted(n for n, c in CATALOG.items() if c.role == "node" and not c.fallback_only)


def get(name: str) -> Command:
    if name not in CATALOG:
        raise KeyError(f"unknown command {name!r} (see `manifest`)")
    return CATALOG[name]


def by_tier(tier: str) -> List[str]:
    return sorted(n for n, c in CATALOG.items() if c.tier == tier)


def safe_commands() -> List[str]:
    """Commands the default (tappable) dashboard exposes — green + yellow, ha_expose True."""
    return sorted(n for n, c in CATALOG.items() if c.ha_expose and c.tier in (GREEN, YELLOW))


def danger_commands() -> List[str]:
    return sorted(n for n, c in CATALOG.items() if c.tier == RED)


def manifest(topics: Optional[Dict[str, str]] = None) -> dict:
    """The machine-readable manifest for the dashboard (commands + tiers + topic layout)."""
    return {
        "schema": 1,
        "tiers": {GREEN: by_tier(GREEN), YELLOW: by_tier(YELLOW), RED: by_tier(RED)},
        "safe_dashboard": safe_commands(),
        "danger": danger_commands(),
        "roles": {"node": node_delegated(), "bridge_permanent": bridge_permanent(),
                  "host": host_commands()},
        "fallback_only": fallback_only_commands(),
        "node_only": node_only_commands(),
        "topics": topics or {},
        "commands": [CATALOG[n].as_manifest() for n in sorted(CATALOG)],
    }
