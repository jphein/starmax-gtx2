"""Tests for the weather command group (0x12 weather push, protocol-spec §3.7).

The wire builder (``base.build_weather`` / ``base.Weather``) is capture-verified and already
covered by ``test_commands.py::test_weather_frames_and_roundtrips``. THIS module covers the CLI
command wrapper: the group contract (discovery/COMMANDS/register), that argparse args map onto
the §3.7 field layout, the ``--dry-run`` path, PII-free defaults, and that the command's output
is a byte-for-byte pass-through to the single-source-of-truth base builder.

Everything here is synthetic/PII-free — no real location, ever (the captures held a real city;
a regression that re-embeds one must fail this suite).
"""
import argparse
import asyncio

from starmax_client import commands, framing
from starmax_client import protobuf as pb
from starmax_client.commands import base
from starmax_client.commands import weather as W


# --------------------------------------------------------------------------- helpers
def _forecast_fields(frame: bytes) -> dict:
    """Parse a built weather frame → the inner f3 forecast's {field: value} map (§3.7)."""
    fr = framing.parse_frame(frame)
    top = {k: v for k, _w, v in pb.parse(fr.payload)}
    return {k: v for k, _w, v in pb.parse(top[3])}, fr, top


def _parse_weather_args(argv):
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    W.register(sub, client=None)
    return p.parse_args(argv)


# --------------------------------------------------------------------------- group contract
def test_group_attribute():
    assert W.GROUP == "weather"


def test_commands_dict_is_callable_builder():
    assert set(W.COMMANDS) == {"weather"}
    assert all(callable(fn) for fn in W.COMMANDS.values())


def test_module_is_discovered_without_errors():
    """The smoke gate (test_integration_smoke) requires zero discovery errors; assert weather
    is discovered and contributes no error of its own."""
    assert "weather" not in dict(commands.discovery_errors())
    assert "weather" in commands.command_catalog()
    assert "weather" in commands.command_catalog()["weather"]


# --------------------------------------------------------------------------- frame structure (§3.7)
def test_weather_frame_structure_and_envelope():
    """Opcode 0x12, app->watch (LEN==len, no CRC), envelope f1=2/f2=1/f3=forecast (§3.7)."""
    frame = W.build_from_args(_parse_weather_args(
        ["weather", "--temp", "21", "--condition", "6", "--city", "Anytown"]))
    forecast, fr, top = _forecast_fields(frame)
    assert fr.opcode == base.OP_WEATHER == 0x12
    assert fr.flag == 0
    assert fr.crc_ok is None and fr.length_field == len(frame)   # app->watch: no CRC (§1.1)
    assert top[1] == 2 and top[2] == 1                            # envelope constants
    assert forecast[6] == 21 and forecast[5] == 6                 # current temp, condition
    assert forecast[10] == b"Anytown"                             # location (f10)


def test_cli_args_map_onto_field_layout():
    """--temp/--condition/--hi/--lo/--city/--month/--day map to §3.7 f6/f5/f8/f9/f10/f1/f2."""
    ns = _parse_weather_args(
        ["weather", "--temp", "25", "--condition", "3", "--hi", "30", "--lo", "18",
         "--city", "Testville", "--month", "7", "--day", "11"])
    forecast, _fr, _top = _forecast_fields(W.build_from_args(ns))
    assert forecast[1] == 7 and forecast[2] == 11        # month, day
    assert forecast[5] == 3                              # condition code
    assert forecast[6] == 25                             # current temp
    assert forecast[8] == 30 and forecast[9] == 18       # hi (f8), lo (f9)
    assert forecast[10] == b"Testville"


def test_hi_lo_default_to_current_temp_when_omitted():
    """Sensible defaults: with no --hi/--lo, today max/min collapse to the current temp."""
    forecast, _fr, _top = _forecast_fields(
        W.build_from_args(_parse_weather_args(["weather", "--temp", "17"])))
    assert forecast[6] == 17 and forecast[8] == 17 and forecast[9] == 17


# --------------------------------------------------------------------------- parity (single source of truth)
def test_wrapper_is_byte_for_byte_pass_through_to_base_builder():
    """The command must not re-encode the wire format — it maps args onto base.Weather and
    delegates to base.build_weather (byte-parity with GB StarmaxMessages.buildWeather)."""
    ns = _parse_weather_args(
        ["weather", "--temp", "22", "--condition", "6", "--hi", "28", "--lo", "15",
         "--city", "Anytown", "--month", "7", "--day", "11", "--seq", "0x20"])
    via_cli = W.build_from_args(ns)
    via_base = base.build_weather(base.Weather(
        city="Anytown", month=7, day=11, hour=0, minute=0, condition=6,
        temp_current=22, temp_max=28, temp_min=15), seq=0x20)
    assert via_cli == via_base


# --------------------------------------------------------------------------- dry-run / CLI wiring
def test_register_adds_weather_subcommand():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    W.register(sub, client=None)
    assert "weather" in sub.choices


def test_dry_run_prints_hex_frame(capsys):
    ns = _parse_weather_args(["weather", "--no-enable", "--dry-run"])   # --no-enable = single frame
    rc = asyncio.run(ns.func(ns))
    assert rc == 0
    out = capsys.readouterr().out.strip()
    fr = framing.parse_frame(bytes.fromhex(out))    # single hex line round-trips to a frame
    assert fr.opcode == base.OP_WEATHER


def test_weather_runnable_with_no_positional_args():
    """No required positional -> `weather --dry-run` alone builds (needed by the smoke gate's
    end-to-end dry-run path)."""
    ns = _parse_weather_args(["weather", "--dry-run"])
    assert getattr(ns, "func", None) is not None


def test_default_auto_sends_enable_then_weather(capsys):
    """DEFAULT: weather sends the 0x04 feature-enable FIRST, then the 0x12 weather frame — display
    is gated on the enable (verified live), so `weather` Just Works without a separate `activate`.
    Dry-run prints both frames in order: 0x04 then 0x12."""
    ns = _parse_weather_args(["weather", "--dry-run"])
    rc = asyncio.run(ns.func(ns))
    assert rc == 0
    lines = [ln for ln in capsys.readouterr().out.strip().splitlines() if ln]
    assert len(lines) == 2
    assert framing.parse_frame(bytes.fromhex(lines[0])).opcode == 0x04          # enable first
    assert framing.parse_frame(bytes.fromhex(lines[1])).opcode == base.OP_WEATHER  # then weather


def test_no_enable_sends_only_weather(capsys):
    """--no-enable opts out of the auto 0x04: only the 0x12 weather frame is sent."""
    ns = _parse_weather_args(["weather", "--no-enable", "--dry-run"])
    asyncio.run(ns.func(ns))
    lines = [ln for ln in capsys.readouterr().out.strip().splitlines() if ln]
    assert len(lines) == 1
    assert framing.parse_frame(bytes.fromhex(lines[0])).opcode == base.OP_WEATHER


# --------------------------------------------------------------------------- privacy guard
def test_defaults_are_pii_free():
    """The default location is a synthetic placeholder; a real captured city must never leak."""
    assert W.DEFAULT_CITY == "Anytown"
    frame = W.build_from_args(_parse_weather_args(["weather"]))
    # synthetic stand-ins for the (city, city, name) shape of real captured PII; the default
    # frame is built from placeholders, so no real location/name can ever appear. The actual
    # regression guard is the DEFAULT_CITY == "Anytown" assertion above.
    for leaked in (b"Realcity", b"Sometown", b"Realname"):
        assert leaked not in frame


# --------------------------------------------------------------------------- large frame (fragments, ~366B)
def test_full_forecast_frame_fragments_and_roundtrips():
    """A full day (24 hourly + 3 daily) is far larger than a current-only push and, at a real
    BLE MTU, splits into 0xC1 + 0xC3 PDUs yet parses back to one 0x12 frame. (The clean-room
    builder is a faithful SUBSET — unresolved fields omitted — so it is ~220B, smaller than the
    vendor's ~366B; fragmentation is driven by the live MTU, not any 255-byte limit — §3.7.)"""
    argv = ["weather", "--temp", "31", "--condition", "6", "--city", "Anytown",
            "--hourly", ",".join(["31/21"] * 24),
            "--daily", "33/22/6,32/21/6,32/23/1"]
    frame = W.build_from_args(_parse_weather_args(argv))
    minimal = W.build_from_args(_parse_weather_args(["weather", "--temp", "31"]))
    assert len(frame) > len(minimal) + 100                    # the 24h+3d payload landed
    pdus = framing.frame_to_pdus(frame, 40)                   # realistic small MTU
    assert len(pdus) > 1
    assert pdus[0][0] == framing.SOF and all(p[0] == framing.CONT for p in pdus[1:])
    fr = framing.parse_frame(frame)
    forecast = {k: v for k, _w, v in pb.parse(fr.payload)}
    assert fr.opcode == base.OP_WEATHER
    # 24 hourly (f11) + 3 daily (f19) entries survived the round-trip.
    assert sum(1 for k, _w, _v in pb.parse(forecast[3]) if k == 11) == 24
    assert sum(1 for k, _w, _v in pb.parse(forecast[3]) if k == 19) == 3
