"""Regression lock for the CLI command audit (issue #6, see docs/command-audit.md).

Pins every catalog command to its wire opcode and asserts each `--dry-run` yields a structurally
valid app->watch 0xC1 frame. If a command's opcode drifts, a new command lands, or a wrong-layer
opcode is added/fixed, these tests fail loudly so the audit doc is kept honest. Offline, no BLE.
"""
from __future__ import annotations

import pytest

from starmax_client import commands, framing

# --- ground truth: capture-confirmed wire opcodes (docs/command-audit.md) --------------------
CAP_OPCODES = {0x01, 0x02, 0x03, 0x04, 0x05, 0x07, 0x0E, 0x10, 0x11, 0x12, 0x13, 0x16, 0x18, 0x22}

# --- pinned command -> wire opcode (as audited 2026-07) --------------------------------------
EXPECTED_OPCODE = {
    ("dials", "dial-activate"): 0x16,
    ("files", "dial-list"): 0x16,
    ("files", "dial-switch"): 0x16,
    ("files", "nfc-list"): 0x1C,
    ("files", "sport-control"): 0x1A,
    ("health", "female-health-set"): 0x0E,
    ("health", "health-interval"): 0x0E,
    ("health", "health-switch-read"): 0x0E,
    ("health", "health-switch-write"): 0x0E,
    ("health", "history-status"): 0x0E,
    ("health", "history-sync"): 0x0E,
    ("health", "hr-config"): 0x0E,
    ("health", "hr-history"): 0x0E,
    ("health", "realtime-measure"): 0x0E,
    ("health", "realtime-open"): 0x0E,
    ("health", "sleep-history"): 0x0E,
    ("health", "activity-history"): 0x0E,
    ("health", "workout-history"): 0x0E,
    ("health", "spo2-history"): 0x0E,
    ("notify", "call"): 0x14,
    ("notify", "camera"): 0x1D,  # was 0x04 (feature-bitmap collision) — re-opcoded to a placeholder
    ("notify", "music"): 0x15,
    ("notify", "notify-detailed"): 0x11,
    ("notify", "notify-summary"): 0x13,
    ("settings", "alarm-get"): 0x07,
    ("settings", "alarm-set"): 0x07,
    ("settings", "aod"): 0x22,
    ("settings", "date-format"): 0x22,
    ("settings", "device-state"): 0x82,
    ("settings", "dnd"): 0xB4,
    ("settings", "drink-water"): 0xB7,
    ("settings", "event-reminders"): 0xBB,
    ("settings", "feature-bitmap"): 0x04,  # [CAP] notification/feature enable (PR #4)
    ("settings", "sedentary"): 0xB6,
    ("settings", "setting-query"): 0x22,
    ("settings", "sport-goals"): 0x8A,
    ("settings", "user-profile"): 0x03,
    ("settings", "world-clock"): 0x22,
    ("settings", "wrist-raise"): 0x82,
    ("weather", "weather"): 0x12,
}

# Commands whose WIRE opcode is NOT capture-confirmed (SDK REV / placeholder / guess). Kept
# explicit so adding a new one — or hardening an existing one off a bad opcode — fails CI and
# forces a docs/command-audit.md update.
KNOWN_NON_CAP = {
    ("files", "nfc-list"), ("files", "sport-control"),
    ("notify", "call"), ("notify", "music"), ("notify", "camera"),  # camera re-opcoded 0x04->0x1d
    ("settings", "device-state"), ("settings", "wrist-raise"), ("settings", "sport-goals"),
    ("settings", "dnd"), ("settings", "sedentary"), ("settings", "drink-water"),
    ("settings", "event-reminders"),
}


def _catalog():
    cat = commands.command_catalog()
    return [(g, n, b) for g, cmds in cat.items() for n, b in cmds.items()]


def _build(group, name, builder):
    import importlib
    mod = importlib.import_module(f"starmax_client.commands.{group}")
    override = getattr(mod, "SMOKE_ARGS", {}).get(name, {})
    res = commands.invoke_builder(builder, override)
    frames = res if isinstance(res, (list, tuple)) else [res]
    return bytes(frames[0])


@pytest.mark.parametrize("group,name,builder", _catalog(),
                         ids=[f"{g}:{n}" for g, n, _ in _catalog()])
def test_dry_run_yields_valid_c1_frame(group, name, builder):
    raw = _build(group, name, builder)
    assert raw and raw[0] == framing.SOF, f"{group}:{name} not a 0xC1 frame"
    fr = framing.parse_frame(raw, direction=framing.DIR_APP_TO_WATCH)
    assert fr.length_field == len(raw), f"{group}:{name} LEN {fr.length_field} != {len(raw)}"
    assert 0 <= fr.opcode <= 0xFF


def test_opcodes_match_pinned_audit():
    """Every command's wire opcode matches the audited value; new/renamed commands must be added."""
    live = {(g, n): framing.parse_frame(_build(g, n, b),
                                        direction=framing.DIR_APP_TO_WATCH).opcode
            for g, n, b in _catalog()}
    assert live == EXPECTED_OPCODE, (
        "command->opcode drift; reconcile with docs/command-audit.md. "
        f"added/changed={set(live) ^ set(EXPECTED_OPCODE) or {k for k in live if live[k]!=EXPECTED_OPCODE.get(k)}}")


def test_non_cap_command_set_is_locked():
    """The set of commands on non-[CAP] wire opcodes is exactly the known wrong-layer set."""
    live_non_cap = {(g, n) for g, n, b in _catalog()
                    if framing.parse_frame(_build(g, n, b),
                                           direction=framing.DIR_APP_TO_WATCH).opcode not in CAP_OPCODES}
    assert live_non_cap == KNOWN_NON_CAP, (
        "wrong-layer command set changed — update KNOWN_NON_CAP + docs/command-audit.md")


def test_camera_no_longer_collides_with_feature_bitmap():
    """FIXED (issue #9): notify `camera` must NOT emit the real [CAP] feature-bitmap opcode 0x04
    — otherwise a live `camera` would silently write a feature bitmap. It is re-opcoded to a
    non-colliding, non-[CAP] placeholder and is --force-gated."""
    from starmax_client.commands.settings import OP_FEATURE_BITMAP
    from starmax_client.commands.notify import UNVERIFIED_OP_CAMERA_CONTROL
    assert UNVERIFIED_OP_CAMERA_CONTROL != OP_FEATURE_BITMAP
    assert UNVERIFIED_OP_CAMERA_CONTROL not in CAP_OPCODES


# ---- live-send safety guard (issue #9) -------------------------------------------------------
def test_gated_commands_require_force():
    """Every wrong-layer / unverified command is gated (requires --force for a live send);
    every capture-verified command is not."""
    from starmax_client import commands as C
    # realtime rides 0x0e (health-SWITCH opcode), not a real realtime opcode -> must be gated
    for name in ("camera", "realtime-open", "realtime-measure", "device-state", "dnd",
                 "sport-control", "nfc-list", "dial-switch", "dial-activate", "aod"):
        assert C.requires_force(name), f"{name} should be --force-gated"
    for name in ("notify-detailed", "notify-summary", "user-profile", "dial-list",
                 "health-switch-read", "alarm-get", "setting-query"):
        assert not C.requires_force(name), f"{name} is [CAP] and must NOT be gated"


def test_run_refuses_gated_command_live_without_force():
    """cli._run blocks a live gated command unless --force; --dry-run and --force both pass."""
    import argparse
    import asyncio
    from starmax_client import cli

    calls = []

    async def _func(args):
        calls.append(args.command)
        return 0

    def _ns(**kw):
        base = dict(command="camera", func=_func, _client=None, dry_run=False, force=False)
        base.update(kw)
        return argparse.Namespace(**base)

    # live, no force -> refused (return 2), handler NOT called, no connect attempted
    assert asyncio.run(cli._run(_ns())) == 2 and not calls
    # dry-run -> allowed (needs_client False), handler runs
    assert asyncio.run(cli._run(_ns(dry_run=True))) == 0 and calls == ["camera"]
    # a [CAP] command live would connect; just assert the guard doesn't fire for it
    from starmax_client import commands as C
    assert not C.requires_force("notify-detailed")
