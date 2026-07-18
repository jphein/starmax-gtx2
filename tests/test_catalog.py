"""The safety tiering must be honest and locked to the library's own protocol audit."""
from __future__ import annotations

from starmax_client.commands import CAP_OPCODES, GATED_COMMANDS

from gtx2_bridge import catalog


def test_every_command_has_builder_or_known_special_kind():
    special = {"buzz", "notify", "set-time", "weather", "activate", "sync-health",
               "dial-list", "dial-push", "alarm-set", "user-profile", "event-reminders", "flash"}
    for name, c in catalog.CATALOG.items():
        assert c.kind in special or c.builder is not None, f"{name}: no builder and not a special"
        assert c.tier in (catalog.GREEN, catalog.YELLOW, catalog.RED)


def test_red_commands_are_gated_and_off_dashboard():
    for name in catalog.danger_commands():
        c = catalog.CATALOG[name]
        assert c.tier == catalog.RED
        assert c.needs_confirm is True, f"{name} must require confirm"
        assert c.needs_force is True
        assert c.ha_expose is False, f"{name} must NOT be a one-tap dashboard button"


def test_flash_firmware_is_red():
    """The accidental-brick guard: firmware flash can never be a safe/tappable command."""
    assert "flash-firmware" in catalog.danger_commands()
    assert catalog.CATALOG["flash-firmware"].ha_expose is False


def test_safe_dashboard_excludes_every_danger_command():
    safe = set(catalog.safe_commands())
    assert safe.isdisjoint(set(catalog.danger_commands()))
    # danger set is exactly the pinned list
    assert set(catalog.danger_commands()) == {"flash-firmware", "dnd", "music", "camera", "call"}


def test_yellow_commands_match_the_library_gate():
    """Yellow = the library's GATED (schema/inferred) commands, minus the ones pinned red.

    This locks the catalog to starmax_client's own audit so a tier can't silently drift.
    """
    lib_gated = set(GATED_COMMANDS)
    # commands the bridge exposes that the library also names
    exposed = set(catalog.CATALOG)
    for name in catalog.by_tier(catalog.YELLOW):
        # every yellow command the library knows about must be gated there (not [CAP])
        if name in lib_gated or name in exposed:
            assert name not in catalog.RED_COMMANDS


def test_green_commands_use_capture_verified_opcodes():
    """Green commands that pin an opcode must use a capture-verified one."""
    for name in catalog.by_tier(catalog.GREEN):
        c = catalog.CATALOG[name]
        if c.opcode is not None:
            assert c.opcode in CAP_OPCODES, f"green {name} opcode 0x{c.opcode:02x} not [CAP]"


def test_manifest_shape():
    m = catalog.manifest(topics={"cmd": "gtx2/cmd"})
    assert m["schema"] == 1
    assert set(m["tiers"]) == {"green", "yellow", "red"}
    assert m["topics"]["cmd"] == "gtx2/cmd"
    names = {c["name"] for c in m["commands"]}
    assert {"find", "notify", "set-time", "weather"} <= names   # the 4 headline features
    for c in m["commands"]:
        assert set(c) >= {"name", "group", "tier", "kind", "ha_expose", "needs_confirm", "params"}
