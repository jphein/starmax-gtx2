"""``starmax_client.commands`` — command builders, split into per-domain modules.

Layout
------
* ``base``   — the original flat builders (bind / set-time / notify / find / health-sync /
  alarm / weather), decoded from real captures. Re-exported here so legacy imports
  (``from starmax_client import commands as C``; ``C.build_bind`` …) keep working.
* ``health`` / ``settings`` / ``notify`` / ``files`` — per-domain command groups. Each exposes
  a ``COMMANDS`` dict ``{name: builder}`` and ``register(subparsers, client=None)`` for the CLI.

Discovery
---------
The CLI/integration layer (module B5) enumerates the group modules via
:func:`iter_command_modules` rather than importing each by name — drop a new
``commands/<group>.py`` that defines ``register`` and it is picked up automatically.

Clean-room: everything under this package is the STANDALONE lane (Track B) and MAY use the
APK schema. It must never inform the Gadgetbridge PR.
"""
from __future__ import annotations

# Backward-compatible re-export of the legacy flat builders (keeps cli.py + existing tests green).
from starmax_client.commands.base import *  # noqa: F401,F403
from starmax_client.commands import base  # noqa: F401  (also reachable as commands.base)


def iter_command_modules():
    """Yield each per-domain command-group module that defines ``register()``.

    Lazily imports the sibling modules (``base`` excluded) so a broken/half-written group
    module cannot break ``import starmax_client.commands`` for everyone else. Used by B5's
    CLI wiring and its dry-run smoke test.
    """
    import importlib
    import pkgutil

    for info in pkgutil.iter_modules(__path__):
        if info.name in ("base", "__init__"):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{info.name}")
        except Exception:  # noqa: BLE001 — a broken sibling shouldn't kill discovery
            continue
        if hasattr(mod, "register") and hasattr(mod, "COMMANDS"):
            yield mod


# =========================================================================== B5 integration API
# Built on iter_command_modules() above. These are what the CLI wiring and the dry-run smoke
# gate consume; keeping them here means a group module only ever imports the shared core.

def command_catalog():
    """``{group: {command_name: builder}}`` across every discovered group module.

    ``group`` is the module's ``GROUP`` attribute (falling back to the bare module name).
    """
    cat = {}
    for mod in iter_command_modules():
        group = getattr(mod, "GROUP", mod.__name__.rsplit(".", 1)[-1])
        cmds = getattr(mod, "COMMANDS", {})
        if isinstance(cmds, dict) and cmds:
            cat.setdefault(group, {}).update(cmds)
    return cat


def register_all(subparsers, client=None):
    """Wire every discovered group's subcommands into ``subparsers``.

    Returns a list of human-readable problems (a group whose ``register()`` raised). A raising
    group is skipped, never fatal — the rest of the CLI still builds.
    """
    problems = []
    for mod in iter_command_modules():
        try:
            mod.register(subparsers, client)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{mod.__name__}: register() raised {e!r}")
    return problems


def discovery_errors():
    """``[(module_name, error), ...]`` for sibling group modules that fail to import or violate
    the contract (missing/invalid ``COMMANDS``, missing ``register``, non-callable entries).

    ``iter_command_modules`` deliberately *skips* broken modules so one can't break the CLI;
    this is the counterpart the smoke gate asserts empty, so a broken group fails the gate
    loudly instead of vanishing.
    """
    import importlib
    import pkgutil

    errors = []
    for info in pkgutil.iter_modules(__path__):
        if info.name in ("base", "__init__") or info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{info.name}")
        except Exception as e:  # noqa: BLE001
            errors.append((info.name, f"import failed: {e!r}"))
            continue
        cmds = getattr(mod, "COMMANDS", None)
        if not isinstance(cmds, dict) or not cmds:
            errors.append((info.name, "missing/empty COMMANDS dict"))
        elif not callable(getattr(mod, "register", None)):
            errors.append((info.name, "missing register()"))
        else:
            bad = [k for k, v in cmds.items() if not callable(v)]
            if bad:
                errors.append((info.name, f"non-callable COMMANDS entries: {bad}"))
    return errors


_INT_ARG_NAMES = {"category", "data_type", "offset", "seq", "index", "subop", "value", "count",
                  "opcode", "app_id", "duration", "day", "hour", "minute", "month", "year", "level"}
_STR_ARG_NAMES = {"metric", "text", "title", "body", "name", "city", "app", "label"}


def _sample_value(pname, ann):
    """A synthetic, PII-free sample for a required builder parameter of (resolved) type ``ann``.

    Handles scalars, sequences (``[]``), and dataclasses (constructed with sampled fields).
    ``ann`` may be a real type or, under ``from __future__ import annotations``, a string.
    """
    import collections.abc as _abc
    import dataclasses as _dc
    import typing as _t

    origin = _t.get_origin(ann)
    if origin is not None and origin not in (str, bytes):
        try:
            if issubclass(origin, (_abc.Sequence, _abc.Set)):
                return []
        except TypeError:
            pass
    if ann in (list, tuple, set, frozenset):
        return []
    if _dc.is_dataclass(ann):
        return _sample_dataclass(ann)
    if ann in (int,) or ann == "int":
        return 0
    if ann in (bool,) or ann == "bool":
        return False
    if ann in (bytes, bytearray) or ann == "bytes":
        return b""
    if ann in (str,) or ann == "str":
        return "hr" if pname == "metric" else "x"
    if isinstance(ann, str) and ann.lower().split("[")[0] in (
            "sequence", "list", "tuple", "iterable", "set"):
        return []
    if pname in _INT_ARG_NAMES:
        return 0
    if pname in _STR_ARG_NAMES:
        return "hr" if pname == "metric" else "x"
    if pname.endswith("_ids") or pname.endswith("s"):  # plural name -> assume a sequence
        return []
    return 0


def _sample_dataclass(dc):
    """Instantiate a dataclass parameter, sampling values for its required fields."""
    import dataclasses as _dc

    kwargs = {}
    for f in _dc.fields(dc):
        has_default = (f.default is not _dc.MISSING) or (f.default_factory is not _dc.MISSING)
        if has_default:
            continue
        kwargs[f.name] = _sample_value(f.name, f.type)
    return dc(**kwargs)


def invoke_builder(fn, override=None):
    """Call a builder with sample args for any required parameter (offline, no I/O).

    Keyword args that already have defaults are left untouched. Required parameters are filled
    from ``override`` (a per-command ``SMOKE_ARGS`` entry) or a synthetic sample resolved from
    the parameter's type hint — scalars, sequences (``[]``), and dataclasses (constructed with
    sampled fields) are all handled. Lets the smoke gate and ``commands --list`` exercise
    builders such as ``build_history_sync(category)`` or ``build_user_profile(UserProfile)``
    without hardware.
    """
    import inspect
    import typing

    override = dict(override or {})
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(**override)
    try:
        hints = typing.get_type_hints(fn)
    except Exception:  # noqa: BLE001 - fall back to raw (possibly string) annotations
        hints = {}
    kwargs = {}
    for pname, p in sig.parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if pname in override:
            kwargs[pname] = override[pname]
        elif p.default is not inspect.Parameter.empty:
            continue
        else:
            kwargs[pname] = _sample_value(pname, hints.get(pname, p.annotation))
    return fn(**kwargs)


# =========================================================================== live-send safety
# Wire opcodes reproduced against a real capture (docs/command-audit.md). A command is
# "live-safe" only if BOTH its opcode AND payload are capture-verified.
CAP_OPCODES = frozenset({0x01, 0x02, 0x03, 0x04, 0x05, 0x07, 0x0E,
                         0x10, 0x11, 0x12, 0x13, 0x16, 0x18, 0x22})

# Group commands whose wire opcode and/or payload are NOT capture-verified — [SCHEMA] (SDK REV
# opcode), [INFERRED] (placeholder), or a real opcode with an unverified payload. Firing these
# LIVE requires --force (see cli._run); --dry-run is always allowed. Kept in sync with
# docs/command-audit.md and locked by tests/test_command_audit.py. Notably realtime-open/-measure
# ride 0x0e (the health-SWITCH opcode) but the real realtime-enable opcode is UNRESOLVED, so they
# only ACK — they are gated, not live-safe.
GATED_COMMANDS = frozenset({
    # notify — SDK guesses / re-opcoded placeholder
    "call", "music", "camera",
    # files / dials — placeholder or inferred-payload opcodes
    "sport-control", "nfc-list", "dial-switch", "dial-activate",
    # settings — SDK REV opcodes (0x82/0x8a/0xb4-0xbb) + unverified 0x22 KV payloads
    "device-state", "wrist-raise", "sport-goals", "dnd", "sedentary", "drink-water",
    "event-reminders", "aod", "world-clock", "date-format",
    # health — schema payloads on 0x0e / unresolved realtime opcode
    "realtime-open", "realtime-measure", "hr-config", "female-health-set", "health-interval",
})


def requires_force(command_name: str) -> bool:
    """True if ``command_name`` is not capture-verified and needs --force for a live send."""
    return command_name in GATED_COMMANDS
