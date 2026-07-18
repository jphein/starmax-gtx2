"""Tests for the custom watch-face (dial) install path.

Offline, no BLE. Covers the native-container codec (:mod:`starmax_client.dialfmt`), the
push planning + wire-naming, and the streaming driver (via a fake client). A byte-exact
regression against the real captured install runs only if the (gitignored) capture blob is
present locally — the committed vectors are tiny + PII-free.
"""
from __future__ import annotations

import asyncio
import os
import struct
import zlib
from types import SimpleNamespace

import pytest

from starmax_client import dialfmt
from starmax_client.commands import dials, files
from starmax_client.protobuf import ProtobufWriter


# --------------------------------------------------------------------------- fixtures
def _synthetic_blob():
    """A small, PII-free native container exercising JSON + two 'image' assets."""
    assets = [
        ("dial.json", b'{"name":"TEST_DIAL","resolution_ratio":"466x466","platform":"ats3085s"}'),
        ("file.json", b'{"item":[{"name":"Number","format":"png"}]}'),
        ("BG_0565.bmp", bytes(range(64)) * 3),
        ("Number_0_8888.png", b"\x18\x38\x00\x05\x1f\x00" + b"\xde\xad\xbe\xef" * 8),
    ]
    return dialfmt.build_blob("TEST_DIAL", assets), assets


CAPTURED = os.path.join(os.path.dirname(__file__), "..", "..",
                        "scratch", "full-impl", "captured_dial_25022.bin")


# --------------------------------------------------------------------------- codec
def test_build_then_parse_roundtrips_assets():
    blob, assets = _synthetic_blob()
    parsed = dialfmt.parse_blob(blob)
    assert parsed.name == "TEST_DIAL"
    assert parsed.asset_names == [a for a, _ in assets]
    for (name, data) in assets:
        assert parsed.get(name) == data


def test_rebuild_is_byte_identical():
    blob, _ = _synthetic_blob()
    assert dialfmt.rebuild_blob(dialfmt.parse_blob(blob)) == blob


def test_header_layout_is_byte_exact():
    blob, assets = _synthetic_blob()
    # name field (30B, NUL-padded)
    assert blob[:30] == b"TEST_DIAL" + b"\x00" * (30 - len("TEST_DIAL"))
    # magics little-endian, count BIG-endian, crc32 over blob[0x2c:]
    assert struct.unpack_from("<HH", blob, 0x1e) == (dialfmt.MAGIC1, dialfmt.MAGIC2)
    assert struct.unpack_from(">H", blob, 0x24)[0] == len(assets)
    assert struct.unpack_from("<I", blob, 0x28)[0] == zlib.crc32(blob[0x2c:]) & 0xFFFFFFFF
    # asset table is 38-byte records starting at 0x2c
    assert dialfmt.ENTRY_LEN == 38 and dialfmt.HEADER_LEN == 0x2c


def test_asset_offsets_are_contiguous_and_absolute():
    blob, assets = _synthetic_blob()
    data_start = dialfmt.HEADER_LEN + len(assets) * dialfmt.ENTRY_LEN
    cursor = data_start
    for i, (name, data) in enumerate(assets):
        eoff = dialfmt.HEADER_LEN + i * dialfmt.ENTRY_LEN
        off, length = struct.unpack_from("<II", blob, eoff + dialfmt.NAME_LEN)
        assert off == cursor and length == len(data)
        cursor += length
    assert cursor == len(blob)  # zero trailing padding


def test_parse_rejects_bad_magic():
    blob, _ = _synthetic_blob()
    bad = bytearray(blob)
    bad[0x1e] ^= 0xFF
    with pytest.raises(dialfmt.DialFormatError):
        dialfmt.parse_blob(bytes(bad))


def test_parse_rejects_crc_mismatch():
    blob, _ = _synthetic_blob()
    bad = bytearray(blob)
    bad[-1] ^= 0xFF  # corrupt a data byte -> header crc no longer matches
    with pytest.raises(dialfmt.DialFormatError):
        dialfmt.parse_blob(bytes(bad))
    # ...but parsing can be forced past the CRC for salvage
    dialfmt.parse_blob(bytes(bad), verify_crc=False)


@pytest.mark.skipif(not os.path.isfile(CAPTURED), reason="captured dial blob not present")
def test_byte_exact_against_real_capture():
    """The codec reproduces the REAL captured CWR05G_23687 install byte-for-byte."""
    blob = open(CAPTURED, "rb").read()
    parsed = dialfmt.parse_blob(blob, verify_crc=True)
    assert parsed.name == "CWR05G_23687"
    assert len(parsed.assets) == 24
    assert dialfmt.rebuild_blob(parsed) == blob
    assert dialfmt.build_blob(parsed.name, [(a.name, a.data) for a in parsed.assets]) == blob
    # the D4 finalize CRC in the capture was crc16/xmodem 0xB735 over this container
    assert files.crc16_xmodem(blob) == 0xB735


# --------------------------------------------------------------------------- wire naming + plan
def test_dial_wire_filename():
    assert dials.dial_wire_filename(25022) == "custom_id_25022.bin"
    with pytest.raises(ValueError):
        dials.dial_wire_filename(0)
    with pytest.raises(ValueError):
        dials.dial_wire_filename(70000)


def test_plan_dial_push_shape_and_checksums():
    blob, _ = _synthetic_blob()
    frames = dials.plan_dial_push(blob, 25001)
    d3, d1, *d2s, d4 = frames
    assert d3 == files.build_d3_query()
    # D1 announce carries the custom filename + size==field2 (from-scratch push)
    assert d1[0] == 0xD1
    size, field2 = struct.unpack_from("<II", d1, 2)
    assert size == len(blob) == field2
    assert b"custom_id_25001.bin\x00" in d1
    # D2 chunks cap at 234 payload bytes and cover the whole blob
    assert all(f[0] == 0xD2 and len(f) - 2 <= dials.CHUNK_MAX for f in d2s)
    assert sum(len(f) - 2 for f in d2s) == len(blob)
    # D4 finalize = crc16/xmodem of the blob
    assert d4[0] == 0xD4
    assert struct.unpack_from("<I", d4, 3)[0] & 0xFFFF == files.crc16_xmodem(blob)


def test_load_dial_blob_rejects_malformed_zip(tmp_path):
    # A ZIP dial .bin is now transcoded (see test_dialtranscode); a malformed one still errors
    # cleanly as a ValueError (TranscodeError), not a raw BadZipFile.
    z = tmp_path / "face.bin"
    z.write_bytes(b"PK\x03\x04rest-of-zip")  # PK magic but not a valid archive
    with pytest.raises(ValueError):
        dials.load_dial_blob(str(z))


def test_load_dial_blob_accepts_native(tmp_path):
    blob, _ = _synthetic_blob()
    p = tmp_path / "face.blob"
    p.write_bytes(blob)
    assert dials.load_dial_blob(str(p)) == blob


# --------------------------------------------------------------------------- streaming driver
class FakeClient:
    """Records outbound frames; answers a dial-list request with a chosen active dial."""

    def __init__(self, active="custom_id_25001.bin", mtu=244):
        self.sent = []
        self.responses = []
        self._mtu_payload = mtu
        self._active = active

    async def send_raw(self, frame, response=False):
        self.sent.append(bytes(frame))
        self.responses.append(response)

    async def request(self, frame, opcode):
        payload = (ProtobufWriter()
                   .message(10, ProtobufWriter().varint(1, 1).varint(2, 1)
                            .varint(3, len(self._active)).string(4, self._active).to_bytes())
                   .string(14, self._active)
                   .to_bytes())
        return SimpleNamespace(opcode=opcode, payload=payload)


def test_push_dial_sends_frames_in_order_and_confirms():
    blob, _ = _synthetic_blob()
    client = FakeClient(active="custom_id_25001.bin")
    result = asyncio.run(dials.push_dial(client, blob, dial_id=25001))
    # order: D3, D1, then all D2, then D4
    assert client.sent[0][0] == 0xD3
    assert client.sent[1][0] == 0xD1
    assert client.sent[-1][0] == 0xD4
    assert all(f[0] == 0xD2 for f in client.sent[2:-1])
    assert result["sent"] == len(blob) == result["total"]
    assert result["confirmed"] is True
    assert result["active_dial"] == "custom_id_25001.bin"


def test_push_dial_uses_write_with_response():
    """Reliable delivery: every D-plane frame goes out write-WITH-response (verified live —
    write-without-response overran the watch at ~36% and the install failed)."""
    blob, _ = _synthetic_blob()
    client = FakeClient()
    asyncio.run(dials.push_dial(client, blob, dial_id=25001))
    assert client.responses and all(client.responses), \
        "D-plane frames must be sent with response=True (ATT-level pacing)"


def test_push_dial_reports_unconfirmed_on_wrong_active():
    blob, _ = _synthetic_blob()
    client = FakeClient(active="YHZN_1021@LC.bin")  # some other face stayed active
    result = asyncio.run(dials.push_dial(client, blob, dial_id=25001))
    assert result["confirmed"] is False
    assert result["active_dial"] == "YHZN_1021@LC.bin"


def test_push_dial_guards_against_fragmenting_mtu():
    blob, _ = _synthetic_blob()
    client = FakeClient(mtu=100)  # < a full 236-byte D2 frame
    with pytest.raises(RuntimeError, match="fragment"):
        asyncio.run(dials.push_dial(client, blob, dial_id=25001))
    assert client.sent == []  # nothing streamed before the guard fired


def test_push_dial_no_confirm_skips_request():
    blob, _ = _synthetic_blob()
    client = FakeClient()
    result = asyncio.run(dials.push_dial(client, blob, dial_id=25001, confirm=False))
    assert result["confirmed"] is None


# --------------------------------------------------------------------------- CLI activate frame
def test_dial_activate_is_a_valid_c1_frame():
    from starmax_client import framing
    frame = dials.build_dial_activate(25001)
    fr = framing.parse_frame(frame, direction=framing.DIR_APP_TO_WATCH)
    assert frame[0] == framing.SOF
    assert fr.length_field == len(frame)
    assert fr.opcode == files.OP_DIAL_SET


# --------------------------------------------------------------------------- CLI client wiring
# Regression for the live-test bug: the CLI handlers must read the injected args._client, and
# each subparser must declare a `_client` default so cli._run knows to connect + inject it.
def test_cli_subparsers_declare_client_default_for_injection(tmp_path):
    from starmax_client.cli import build_parser
    blob, _ = _synthetic_blob()
    p = tmp_path / "face.blob"
    p.write_bytes(blob)
    parser = build_parser()
    push_ns = parser.parse_args(["dial-push", str(p), "--dial-id", "25001"])
    act_ns = parser.parse_args(["dial-activate", "25001"])
    # `hasattr(args, "_client")` is exactly what cli._run gates connect-and-inject on.
    assert hasattr(push_ns, "_client") and push_ns._client is None
    assert hasattr(act_ns, "_client") and act_ns._client is None


def test_cli_dial_push_streams_to_injected_client(tmp_path):
    from starmax_client.cli import build_parser
    blob, _ = _synthetic_blob()
    p = tmp_path / "face.blob"
    p.write_bytes(blob)
    ns = build_parser().parse_args(["dial-push", str(p), "--dial-id", "25001"])
    ns._client = FakeClient(active="custom_id_25001.bin")  # what cli._run injects when connected
    rc = asyncio.run(ns.func(ns))
    assert rc == 0
    # actually streamed the bulk plane to the injected client (not the None closure)
    assert ns._client.sent, "handler streamed nothing — it read the wrong client reference"
    assert ns._client.sent[0][0] == 0xD3 and ns._client.sent[-1][0] == 0xD4
    assert any(f[0] == 0xD2 for f in ns._client.sent)


def test_cli_dial_push_without_client_reports_and_errors(tmp_path):
    from starmax_client.cli import build_parser
    blob, _ = _synthetic_blob()
    p = tmp_path / "face.blob"
    p.write_bytes(blob)
    ns = build_parser().parse_args(["dial-push", str(p), "--dial-id", "25001"])
    # _client left as None (no injection) and not --dry-run -> report no client, nonzero rc
    rc = asyncio.run(ns.func(ns))
    assert rc == 1


def test_cli_dial_activate_sends_to_injected_client():
    from starmax_client.cli import build_parser
    ns = build_parser().parse_args(["dial-activate", "25001"])
    ns._client = FakeClient()
    rc = asyncio.run(ns.func(ns))
    assert rc == 0
    assert len(ns._client.sent) == 1 and ns._client.sent[0][0] == 0xC1  # one C1 0x16 frame


# --------------------------------------------------------------------------- dial DELETE [FW]
class DeleteFakeClient:
    """Records sent frames; answers a 0x16 list request with a chosen remaining-filename set."""

    def __init__(self, remaining=(), mtu=244):
        self.sent = []
        self._mtu_payload = mtu
        self._remaining = list(remaining)

    async def send_raw(self, frame, response=False):
        self.sent.append(bytes(frame))

    async def request(self, frame, opcode):
        w = ProtobufWriter()
        for nm in self._remaining:
            w.message(10, ProtobufWriter().varint(1, 1).varint(2, 1)
                      .varint(3, len(nm)).string(4, nm).to_bytes())
        w.varint(18, 12)  # all_plate_support_max
        return SimpleNamespace(opcode=opcode, payload=w.to_bytes())


def test_build_dial_delete_targets_filename():
    from starmax_client import framing
    from starmax_client.protobuf import parse as pb_parse
    # a custom id maps to custom_id_<id>.bin
    frame = dials.build_dial_delete(25022)
    fr = framing.parse_frame(frame, direction=framing.DIR_APP_TO_WATCH)
    assert fr.opcode == files.OP_DIAL_LIST and fr.flag == 0
    fields = {f: v for f, _w, v in pb_parse(fr.payload)}
    assert fields[1] == files.DIAL_OP_DELETE and fields[2] == b"custom_id_25022.bin"
    # --name deletes an arbitrary (built-in/market) filename verbatim
    fr2 = framing.parse_frame(dials.build_dial_delete(name="YHZN_1021@LC.bin"),
                              direction=framing.DIR_APP_TO_WATCH)
    assert {f: v for f, _w, v in pb_parse(fr2.payload)}[2] == b"YHZN_1021@LC.bin"


def test_build_dial_delete_needs_a_target():
    with pytest.raises(ValueError):
        dials.build_dial_delete()


def test_delete_dial_confirms_removal():
    client = DeleteFakeClient(remaining=["custom_id_1.bin"])  # 25022 is gone from the list
    result = asyncio.run(dials.delete_dial(client, "custom_id_25022.bin"))
    assert len(client.sent) == 1 and client.sent[0][0] == 0xC1  # one C1 0x16 DELETE frame
    assert result["deleted"] is True and result["count"] == 1
    assert "custom_id_25022.bin" not in result["remaining"]


def test_delete_dial_reports_not_confirmed_when_still_present():
    client = DeleteFakeClient(remaining=["custom_id_25022.bin", "custom_id_1.bin"])
    result = asyncio.run(dials.delete_dial(client, "custom_id_25022.bin"))
    assert result["deleted"] is False and result["count"] == 2


def test_delete_dial_no_confirm_skips_request():
    client = DeleteFakeClient(remaining=[])
    result = asyncio.run(dials.delete_dial(client, "custom_id_25022.bin", confirm=False))
    assert result["deleted"] is None and len(client.sent) == 1


def test_cli_dial_delete_dry_run_prints_target(capsys):
    from starmax_client.cli import build_parser
    ns = build_parser().parse_args(["dial-delete", "25022", "--dry-run"])
    rc = asyncio.run(ns.func(ns))
    assert rc == 0
    assert "custom_id_25022.bin" in capsys.readouterr().out


def test_cli_dial_delete_sends_and_confirms():
    from starmax_client.cli import build_parser
    ns = build_parser().parse_args(["dial-delete", "25022"])
    ns._client = DeleteFakeClient(remaining=["custom_id_1.bin"])
    rc = asyncio.run(ns.func(ns))
    assert rc == 0
    assert ns._client.sent and ns._client.sent[0][0] == 0xC1


def test_cli_dial_delete_requires_a_target():
    from starmax_client.cli import build_parser
    ns = build_parser().parse_args(["dial-delete"])  # no id, no --name
    ns._client = DeleteFakeClient()
    rc = asyncio.run(ns.func(ns))
    assert rc == 2 and ns._client.sent == []
