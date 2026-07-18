"""Weather push command group (Track-B standalone, group "weather").

Exposes the capture-verified ``0x12`` weather push (protocol-spec §3.7) as a CLI ``weather``
command. It does NOT re-implement the wire format: it maps argparse args onto the shared
``base.Weather`` dataclass and delegates to ``base.build_weather`` — the single source of truth,
which is byte-parity with the Gadgetbridge ``StarmaxMessages.buildWeather`` field map.

Provenance:
  * [CAP] VERIFIED — the 0x12 envelope (f1=2, f2=1, f3=forecast) and the solid forecast fields
    (month/day/hour/minute, current condition, current temp, hi/lo, city, hourly[24], daily[3],
    pressure) are decoded from real captures (protocol-spec §3.7). Unlabelled sub-fields
    (units/AQI/UV) are UNRESOLVED and intentionally omitted.
  * The condition code is the watch's own 1-based space; only ``6`` (clear/sunny) is
    capture-observed, so it is taken as a raw int with that default (no unverified name table).

Enable prereq: weather DISPLAY is gated on the ``0x04`` feature-enable bitmap (verified live: the
watch's "enable feature in app" message cleared only once the ``0x04`` was sent, then weather
displayed over LE — no classic-BT companion needed, unlike notifications). So ``weather`` sends the
``0x04`` first BY DEFAULT and Just Works whether or not the watch has been ``activate``-d. This is
safe to do unconditionally: it sends only the ``0x04`` (not the ``0x03`` profile bundle), so it does
NOT overwrite the watch's stored profile — the opposite trade-off from notify's opt-in enable, whose
``0x03`` would reset the profile. Pass ``--no-enable`` to send the ``0x12`` weather frame alone.

PII: the location is a free-text label. The default is the synthetic placeholder ``Anytown`` —
never embed a real captured city (the captures held one; keep the client PII-free).
"""
from __future__ import annotations

import argparse
from typing import Dict, List, Tuple

from starmax_client.commands.base import Weather, build_weather, OP_WEATHER

GROUP = "weather"

# Sensible, PII-free defaults so `weather --dry-run` builds a valid frame with no arguments.
DEFAULT_CITY = "Anytown"       # synthetic placeholder — must stay non-real (privacy guard)
DEFAULT_CONDITION = 6          # only value capture-observed (§3.7 f5 = clear/sunny at capture)
DEFAULT_TEMP = 20              # °C
DEFAULT_PRESSURE_HPA = 1013.25


# --------------------------------------------------------------------------- arg parsers
def _parse_hourly(s: str) -> List[Tuple[int, int]]:
    """``"31/21,31/20,…"`` -> ``[(hi, temp), …]`` (§3.7 f11, hi/temp per hour, max 24)."""
    out: List[Tuple[int, int]] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        hi, temp = part.split("/")
        out.append((int(hi), int(temp)))
    return out


def _parse_daily(s: str) -> List[Tuple[int, int, int]]:
    """``"33/22/6,…"`` -> ``[(hi, lo, cond), …]`` (§3.7 f19, per day, max 3)."""
    out: List[Tuple[int, int, int]] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        hi, lo, cond = part.split("/")
        out.append((int(hi), int(lo), int(cond)))
    return out


# --------------------------------------------------------------------------- build
def build_from_args(args) -> bytes:
    """Map parsed CLI args onto :class:`base.Weather` and delegate to :func:`base.build_weather`.

    ``--hi``/``--lo`` default to the current temp when omitted (a single current-conditions push).
    """
    hi = args.hi if args.hi is not None else args.temp
    lo = args.lo if args.lo is not None else args.temp
    weather = Weather(
        city=args.city,
        month=args.month, day=args.day, hour=args.hour, minute=args.minute,
        condition=args.condition,
        temp_current=args.temp, temp_max=hi, temp_min=lo,
        hourly=args.hourly, daily=args.daily,
        pressure_hpa=args.pressure,
    )
    return build_weather(weather, seq=args.seq)


# --------------------------------------------------------------------------- registry
# base.build_weather takes a Weather dataclass; the smoke gate's invoke_builder samples one.
COMMANDS: Dict[str, object] = {
    "weather": build_weather,   # wire 0x12 [CAP §3.7] — reused from base (single source of truth)
}


# --------------------------------------------------------------------------- CLI wiring
def _mk_handler(build, opcode: int):
    """Wrap the arg->frame builder into an async CLI handler (matches notify.py/settings.py).

    By DEFAULT it first sends the ``0x04`` feature-enable bitmap, then the weather frame: weather
    DISPLAY is gated on that enable (verified live — the watch's "enable feature in app" cleared
    only after the ``0x04``), so auto-sending it makes ``weather`` Just Work regardless of whether
    the watch has been ``activate``-d. It is safe to send unconditionally: ``0x04`` ONLY (no
    ``0x03`` profile bundle), so — unlike notify's enable — it does NOT reset the watch's profile.
    Pass ``--no-enable`` to skip it (send the ``0x12`` only) for the rare case where it's unwanted.
    On ``--dry-run`` (or with no wired client) it prints each hex frame; otherwise it sends via
    ``client.send_raw``, which fragments the (large) weather frame into 0xC1/0xC3 PDUs.
    """
    async def _handler(args) -> int:
        frames = []
        if not getattr(args, "no_enable", False):
            from starmax_client.commands.settings import build_feature_bitmap
            frames.append(build_feature_bitmap(seq=args.seq))   # 0x04 feature-enable first (default)
        frames.append(build(args))                              # then the 0x12 weather frame
        if getattr(args, "dry_run", False) or getattr(args, "_client", None) is None:
            for fr in frames:
                print(fr.hex())
            return 0
        for fr in frames:
            await args._client.send_raw(fr)
        print("sent")
        return 0
    return _handler


def register(subparsers, client=None) -> None:
    """Add the ``weather`` subcommand. Auto-discovered by ``commands.register_all``.

    All arguments are optional (sensible PII-free defaults) so ``weather --dry-run`` works alone.
    """
    sp = subparsers.add_parser("weather", help="push current weather (wire 0x12) [CAP §3.7]")
    sp.add_argument("--temp", type=int, default=DEFAULT_TEMP, help="current temperature °C")
    sp.add_argument("--condition", type=int, default=DEFAULT_CONDITION,
                    help="watch condition code (1-based; only 6=clear is capture-observed)")
    sp.add_argument("--hi", type=int, default=None, help="today max °C (default: --temp)")
    sp.add_argument("--lo", type=int, default=None, help="today min °C (default: --temp)")
    sp.add_argument("--city", default=DEFAULT_CITY,
                    help="location label — use a synthetic/PII-free name")
    sp.add_argument("--month", type=int, default=0, help="forecast month (0 = unset)")
    sp.add_argument("--day", type=int, default=0, help="forecast day (0 = unset)")
    sp.add_argument("--hour", type=int, default=0, help="forecast hour")
    sp.add_argument("--minute", type=int, default=0, help="forecast minute")
    sp.add_argument("--pressure", type=float, default=DEFAULT_PRESSURE_HPA,
                    help="sea-level pressure in hPa")
    sp.add_argument("--hourly", type=_parse_hourly, default=(),
                    help="hourly hi/temp pairs, comma-separated, e.g. 31/21,31/20 (max 24)")
    sp.add_argument("--daily", type=_parse_daily, default=(),
                    help="daily hi/lo/cond triples, comma-separated, e.g. 33/22/6 (max 3)")
    sp.add_argument("--no-enable", action="store_true",
                    help="skip the 0x04 feature-enable (send only the 0x12 weather frame). By "
                         "default weather sends the 0x04 first so display works without activate.")
    sp.add_argument("--dry-run", action="store_true", help="print the hex frame(s), don't send")
    sp.add_argument("--seq", type=lambda s: int(s, 0), default=0, help="frame seq (default 0)")
    sp.set_defaults(_client=client, func=_mk_handler(build_from_args, OP_WEATHER))
