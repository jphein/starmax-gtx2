"""Tests for the ``monitor`` CLI verb — live realtime sensor stream + link telemetry.

Offline/structural: the realtime channel is SCHEMA-derived (wire opcode UNRESOLVED), so we verify
the frame the monitor BUILDS (opcode + per-sensor protobuf bits) and the CLI wiring — not a live
stream. The sensor->field map is byte-checked so a regression in the bit layout fails loudly.
"""
import asyncio
import io
from contextlib import redirect_stdout

from starmax_client import cli, framing, protobuf as pb


def _fields(payload):
    return {k: v for k, _w, v in pb.parse(payload)}


def _open(sensors):
    raw = bytes(cli._monitor_open_frame(sensors, seq=0))
    fr = framing.parse_frame(raw, direction=framing.DIR_APP_TO_WATCH)
    return raw, fr, _fields(fr.payload)


# --------------------------------------------------------------- CLI wiring
def test_monitor_is_a_registered_core_verb():
    p = cli.build_parser()
    ns = p.parse_args(["monitor", "--dry-run", "--sensors", "accel", "hr"])
    assert ns.func is cli.cmd_monitor


def test_monitor_rejects_unknown_sensor():
    import pytest
    p = cli.build_parser()
    with pytest.raises(SystemExit):                     # argparse choices= guard
        p.parse_args(["monitor", "--sensors", "gps"])


# --------------------------------------------------------------- built frame
def test_open_frame_is_health_opcode_no_crc():
    raw, fr, _ = _open(["accel"])
    assert fr.opcode == 0x0e                            # best-guess realtime opcode (OP_HEALTH)
    assert fr.crc_ok is None and fr.length_field == len(raw)   # app->watch: LEN==total, no CRC


def test_open_frame_sensor_to_field_map():
    """accel->f2, steps->f3, hr->f4, bp->f5, spo2->f6, temp->f7, sugar->f8 (schema RealTimeOpen)."""
    _, _, f = _open(["accel", "spo2"])
    assert f.get(2) == 1 and f.get(6) == 1              # gsensor + blood_oxygen enabled
    assert (f.get(3), f.get(4), f.get(5), f.get(7), f.get(8)) == (0, 0, 0, 0, 0)


def test_open_frame_empty_list_closes_every_sensor():
    _, _, f = _open([])
    assert all(f.get(n) == 0 for n in (2, 3, 4, 5, 6, 7, 8))


def test_open_frame_all_sensors_enabled():
    _, _, f = _open(list(cli._MONITOR_SENSORS))
    assert all(f.get(n) == 1 for n in (2, 3, 4, 5, 6, 7, 8))


# --------------------------------------------------------------- end-to-end dry-run
def _dry_run_fields(argv):
    ns = cli.build_parser().parse_args(argv)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = asyncio.run(ns.func(ns))
    assert rc == 0
    out = buf.getvalue().strip()
    fr = framing.parse_frame(bytes.fromhex(out), direction=framing.DIR_APP_TO_WATCH)
    return _fields(fr.payload)


def test_monitor_dry_run_default_sensors():
    """No --sensors => accel hr steps spo2 (f2,f3,f4,f6 on; f5,f7,f8 off)."""
    f = _dry_run_fields(["monitor", "--dry-run"])
    assert (f.get(2), f.get(3), f.get(4), f.get(6)) == (1, 1, 1, 1)
    assert (f.get(5), f.get(7), f.get(8)) == (0, 0, 0)


def test_monitor_dry_run_all_expands():
    f = _dry_run_fields(["monitor", "--dry-run", "--sensors", "all"])
    assert all(f.get(n) == 1 for n in (2, 3, 4, 5, 6, 7, 8))
