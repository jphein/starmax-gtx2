"""B5 integration coverage gate — dry-run every registered command, offline.

No BLE, no ``bleak``. This discovers every command-group module through
``starmax_client.commands`` and asserts that (a) no group module failed discovery or violates
the contract, and (b) every builder in every group's ``COMMANDS`` produces a structurally
valid app->watch frame. This is the gate CI runs to confirm the B1-B4 modules
plug in uniformly. It grows automatically as more groups land — no per-module edits here.
"""
from __future__ import annotations

import pytest

from starmax_client import commands, framing

_ERRORS = commands.discovery_errors()
_MODULES = list(commands.iter_command_modules())


def _group_of(mod) -> str:
    return getattr(mod, "GROUP", mod.__name__.rsplit(".", 1)[-1])


_CASES = [(mod, name) for mod in _MODULES for name in getattr(mod, "COMMANDS", {})]
_IDS = [f"{_group_of(mod)}:{name}" for mod, name in _CASES]


def _frames(result):
    if isinstance(result, (bytes, bytearray)):
        return [bytes(result)]
    assert isinstance(result, (list, tuple)) and result, "builder returned empty/invalid result"
    return [bytes(f) for f in result]


def test_no_group_module_failed_discovery():
    """A broken or non-conforming group module fails the gate loudly (not silently skipped)."""
    assert not _ERRORS, f"command group modules with problems: {_ERRORS}"


def test_at_least_the_landed_groups_are_discovered():
    """Sanity: discovery finds the group modules present on disk (health has landed)."""
    groups = {_group_of(m) for m in _MODULES}
    # Don't hard-require a specific set (B2-B4 land asynchronously); just prove discovery works
    # whenever any module is present.
    import pkgutil
    on_disk = {i.name for i in pkgutil.iter_modules(commands.__path__)
               if i.name not in ("base", "__init__") and not i.name.startswith("_")}
    if on_disk:
        assert groups, f"group files exist {on_disk} but none were discovered"


@pytest.mark.parametrize("mod,name", _CASES, ids=_IDS)
def test_every_registered_command_builds_a_valid_frame(mod, name):
    override = getattr(mod, "SMOKE_ARGS", {}).get(name, {})
    frames = _frames(commands.invoke_builder(mod.COMMANDS[name], override))
    for raw in frames:
        assert raw and raw[0] == framing.SOF, f"{_group_of(mod)}:{name} is not a 0xC1 frame"
        fr = framing.parse_frame(raw, direction=framing.DIR_APP_TO_WATCH)
        # app->watch invariant: the LEN field equals the whole-frame length and there is no CRC.
        assert fr.length_field == len(raw), \
            f"{_group_of(mod)}:{name} LEN={fr.length_field} != frame len {len(raw)}"
        assert fr.direction == framing.DIR_APP_TO_WATCH
        assert 0 <= fr.opcode <= 0xFF


def test_cli_parser_builds_and_exposes_commands_verb():
    from starmax_client.cli import build_parser

    p = build_parser()
    ns = p.parse_args(["commands", "--list"])
    assert getattr(ns, "func", None) is not None


def test_cli_dry_run_path_prints_a_frame_without_a_client():
    """End-to-end CLI dry-run for one no-positional command per group (proves --dry-run works)."""
    import asyncio
    import io
    from contextlib import redirect_stdout

    from starmax_client.cli import build_parser

    exercised = 0
    for mod in _MODULES:
        for name in sorted(getattr(mod, "COMMANDS", {})):
            p = build_parser()
            try:
                ns = p.parse_args([name, "--dry-run"])
            except SystemExit:
                continue  # needs a positional arg; covered by the builder gate above
            if getattr(ns, "func", None) is None:
                continue
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = asyncio.run(ns.func(ns))
            assert rc == 0, f"{name} --dry-run returned {rc}"
            assert buf.getvalue().strip(), f"{name} --dry-run printed nothing"
            exercised += 1
            break  # one per group is enough to prove the wiring
    if _MODULES:
        assert exercised, "no group exposed a dry-runnable no-arg subcommand"
