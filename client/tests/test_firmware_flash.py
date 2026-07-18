"""Firmware-flash SAFETY + transport tests (:mod:`starmax_client.commands.files`, issue #29).

The flasher is DRY-RUN by default and triple-gated: a real transmit needs ``--force-flash`` AND
``RECOVERY_PROVEN`` (#17) AND a CRC-valid image. These tests prove:
  * the default path NEVER transmits (and structurally cannot — no ``_client`` for cli._run);
  * each gate refuses with a distinct exit code;
  * the streaming driver, when reached, sends the ``res.ota`` bulk plane write-WITH-response.

A live flash is NEVER performed — the driver talks only to a fake in-memory client, and
``RECOVERY_PROVEN`` is asserted False so the CLI arm path is gated shut.
"""
from __future__ import annotations

import asyncio
import os
import struct

import pytest

from starmax_client import otafmt
from starmax_client.cli import build_parser
from starmax_client.commands import files
from starmax_client.transport import StarmaxClient
from tests import fixtures as F
from tests.test_otafmt import build_synth_ota


def _valid_ota() -> bytes:
    return build_synth_ota(b"ZEPHYR-APP-IMAGE-BODY" * 20, trailing_zeros=16)


class FlashFakeClient:
    """Records outbound frames; delivers a configurable D4 finalize ack to the raw tap so the
    accept/version-reject path is exercised. ``d4_reply=None`` = watch stays silent."""

    def __init__(self, mtu: int = 244, d4_reply=b"\xd4\x00\x00") -> None:
        self.sent = []
        self.responses = []
        self._raw = []
        self._mtu_payload = mtu
        self._d4_reply = d4_reply

    def add_raw_listener(self, cb):
        self._raw.append(cb)

    def remove_raw_listener(self, cb):
        if cb in self._raw:
            self._raw.remove(cb)

    async def send_raw(self, frame, response=False):
        self.sent.append(bytes(frame))
        self.responses.append(response)
        if bytes(frame)[:1] == b"\xd4" and self._d4_reply is not None:  # simulate the D4 ack
            for cb in list(self._raw):
                cb(self._d4_reply)


# =========================================================================== driver mechanics
def test_flash_firmware_streams_res_ota_bulk_plane():
    image = _valid_ota()
    client = FlashFakeClient()
    result = asyncio.run(files.flash_firmware(client, image))
    # order: D3, D1, then all D2, then D4
    assert client.sent[0][0] == 0xD3
    assert client.sent[1][0] == 0xD1
    assert client.sent[-1][0] == 0xD4
    assert all(f[0] == 0xD2 for f in client.sent[2:-1])
    # D1 announces the firmware filename; D4 finalize = crc16/xmodem of the whole image
    assert b"res.ota\x00" in client.sent[1]
    assert struct.unpack_from("<I", client.sent[-1], 3)[0] & 0xFFFF == files.crc16_xmodem(image)
    assert result["sent"] == len(image) == result["total"]
    assert result["applied"] is True and result["d4_ack"] == "d40000"   # watch acked d4 00 00


def test_flash_firmware_reports_version_reject():
    # a D4 reply that isn't `d4 00 00` -> not accepted (most likely a version-compat reject)
    client = FlashFakeClient(d4_reply=b"\xd4\x01\x00")
    result = asyncio.run(files.flash_firmware(client, _valid_ota()))
    assert result["applied"] is False and result["d4_ack"] == "d40100"


def test_flash_firmware_no_ack_when_silent():
    client = FlashFakeClient(d4_reply=None)              # watch says nothing / reboots first
    result = asyncio.run(files.flash_firmware(client, _valid_ota(), ack_timeout=0.05))
    assert result["applied"] is None and result["d4_ack"] is None


def test_flash_firmware_uses_write_with_response():
    client = FlashFakeClient()
    asyncio.run(files.flash_firmware(client, _valid_ota()))
    assert client.responses and all(client.responses), \
        "D-plane frames must be sent with response=True (ATT-level pacing, live-proven)"


def test_flash_firmware_refuses_crc_invalid_image():
    b = bytearray(_valid_ota()); b[otafmt.INNER_DATA + 5] ^= 0xFF   # corrupt the inner DATA
    client = FlashFakeClient()
    with pytest.raises(ValueError, match="CRC-invalid"):
        asyncio.run(files.flash_firmware(client, bytes(b)))
    assert client.sent == []                       # refused BEFORE any byte hit the radio


def test_flash_firmware_rejects_non_container():
    client = FlashFakeClient()
    with pytest.raises(otafmt.OtaFormatError):
        asyncio.run(files.flash_firmware(client, b"not an ota image" * 8))
    assert client.sent == []


def test_flash_firmware_guards_fragmenting_mtu():
    client = FlashFakeClient(mtu=100)              # < a full 236-byte D2 frame
    with pytest.raises(RuntimeError, match="fragment"):
        asyncio.run(files.flash_firmware(client, _valid_ota()))
    assert client.sent == []                       # nothing streamed before the guard fired


# =========================================================================== captured-session self-test
# Checkpoints captured from a real OTA session
# (a BLE capture; docs/firmware-dfu.md §29). They are
# deterministic functions of the PUBLIC stock image — asserting our plan reproduces them proves the
# flasher's D3/D1/D2*/D4 byte-stream is wire-correct with NO watch attached (the offline self-test
# rung). The image lives outside this repo (research tree); skip when it is absent.
_STOCK_OTA = os.path.join(os.path.dirname(__file__), "..", "..", "firmware",
                          "cb05_yhzn01_v1.0.3_20241218_02.ota")
_CAP_D1 = "d100e0c62000e0c620000f7265732e6f746100"      # D1 announce: res.ota, size==field2
_CAP_D4 = "d400000daa0000"                              # D4 finalize: whole-file crc16/xmodem 0xAA0D
_CAP_RUNNING_CRC = {3510: 0xD1F8, 7020: 0xB037, 10530: 0x59BE}  # running CRC at 15/30/45-chunk acks


@pytest.mark.skipif(not os.path.isfile(_STOCK_OTA), reason="stock OTA image not present locally")
def test_wire_stream_matches_captured_fw_session():
    image = open(_STOCK_OTA, "rb").read()
    assert otafmt.parse_ota_image(image).valid              # both CRC-32s + section magic verify
    d3, d1, *d2s, d4 = files.plan_bulk_transfer(files.FILE_FIRMWARE, image)
    assert d3 == files.build_d3_query()
    assert d1.hex() == _CAP_D1                              # exact D1 announce from the capture
    assert d2s[0][:2].hex() == "d200"                       # D2 chunk#0 header (ctr 0)
    assert d2s[0][2:].hex().startswith("faeeebde23bbe6e6")  # ...carries the image from offset 0
    assert d4.hex() == _CAP_D4                              # exact D4 finalize from the capture
    # running CRC-16/XMODEM at the watch's 15-chunk ack windows == the capture's ack CRCs
    for off, want in _CAP_RUNNING_CRC.items():
        assert files.crc16_xmodem(image[:off]) == want, f"running CRC diverges at offset {off}"
    assert sum(len(f) - 2 for f in d2s) == len(image)       # the D2 chunks tile the whole image


# =========================================================================== the hard gates
def test_recovery_proven_is_false_by_default():
    """Guard against an accidental RECOVERY_PROVEN=True landing in a commit before #17."""
    assert files.RECOVERY_PROVEN is False


def _flash_ns(tmp_path, *extra, data=None):
    p = tmp_path / "fw.ota"
    p.write_bytes(_valid_ota() if data is None else data)
    return build_parser().parse_args(["flash-firmware", str(p), *extra])


def test_cli_flash_firmware_has_no_client_default(tmp_path):
    """No ``_client`` attr => cli._run's needs_client is False => the default path NEVER connects."""
    ns = _flash_ns(tmp_path)
    assert not hasattr(ns, "_client")
    assert ns.force_flash is False                 # dry-run by default


def test_cli_flash_firmware_default_is_dry_run(tmp_path, capsys):
    ns = _flash_ns(tmp_path)
    rc = asyncio.run(ns.func(ns))
    out = capsys.readouterr().out
    assert rc == 0
    assert "VALID" in out and "flash plan" in out and "DRY-RUN" in out
    assert "res.ota" in out                        # the plan names the firmware transfer


def test_cli_flash_firmware_force_flash_refused_until_recovery(tmp_path, capsys):
    # a synthetic (valid-CRC) image is NOT on the allowlist -> refused behind RECOVERY_PROVEN=False
    ns = _flash_ns(tmp_path, "--force-flash")
    ns._client = FlashFakeClient()
    rc = asyncio.run(ns.func(ns))
    err = capsys.readouterr().err
    assert rc == 3                                 # not-recovery + not-allowlisted exit code
    assert "#17" in err and "not on the verified-safe allowlist" in err
    assert ns._client.sent == []                   # refused before any byte went out


def test_cli_flash_firmware_force_flash_refuses_crc_invalid(tmp_path, capsys):
    b = bytearray(_valid_ota()); b[otafmt.INNER_DATA + 7] ^= 0xFF
    ns = _flash_ns(tmp_path, "--force-flash", data=bytes(b))
    rc = asyncio.run(ns.func(ns))
    err = capsys.readouterr().err
    assert rc == 2                                 # CRC gate fires before the recovery gate
    assert "CRC-invalid" in err


def test_cli_flash_firmware_bad_path_errors():
    ns = build_parser().parse_args(["flash-firmware", "/no/such/file.ota"])
    assert asyncio.run(ns.func(ns)) == 1


def test_cli_flash_firmware_non_container_errors(tmp_path):
    p = tmp_path / "junk.ota"
    p.write_bytes(b"PK\x03\x04 this is a zip, not an ota" * 4)
    ns = build_parser().parse_args(["flash-firmware", str(p)])
    assert asyncio.run(ns.func(ns)) == 1


def test_cli_flash_firmware_arms_when_recovery_proven(tmp_path, monkeypatch, capsys):
    """With #17 gated open AND a connected client injected, the CLI streams the real plan.

    Exercises the arm path end-to-end WITHOUT hardware: RECOVERY_PROVEN is monkeypatched True and a
    fake client is injected as args._client (so the handler uses it and never opens a real link)."""
    monkeypatch.setattr(files, "RECOVERY_PROVEN", True)
    ns = _flash_ns(tmp_path, "--force-flash")
    ns._client = FlashFakeClient()                 # injected 'connected' client -> no real connect
    rc = asyncio.run(ns.func(ns))
    out = capsys.readouterr().out
    assert rc == 0
    assert ns._client.sent[0][0] == 0xD3 and ns._client.sent[-1][0] == 0xD4
    assert all(ns._client.responses)               # streamed write-WITH-response
    assert "[recovery] #17 proven" in out          # synthetic image not allowlisted -> recovery path
    assert "ACCEPTED" in out and "reboots" in out   # D4-accept surfaced from the ack


# =========================================================================== verified-safe allowlist
_POC_OTA = os.path.join(os.path.dirname(__file__), "..", "..", "firmware",
                        "cb05_yhzn01_v1.0.3_CFWPOC.ota")
_CFW2_OTA = os.path.join(os.path.dirname(__file__), "..", "..", "firmware",
                         "cb05_yhzn01_v1.0.3_CFW2.ota")


def test_allowlist_hashes_are_pinned():
    """Guard against accidental edits to the verified-safe hashes / the allowlist size / the gate."""
    assert files.STOCK_V103_SHA256 == "5dac413b0e8e68581d5de1d6916f022727ef9a96bacc4003e1751f86c2967cc0"
    assert files.CFWPOC_SHA256 == "9865fe25473c739e6ab0d58f2a0e58bd0aa819d10cf363a4a39aa98db2ab0c48"
    assert files.CFW2_SHA256 == "e9994fc442533e287ba4f48cee4156deb5fbfe5d653d70b50f05afe6158b8545"
    assert len(files.FLASH_ALLOWLIST) == 3
    assert files.RECOVERY_PROVEN is False          # the narrow allowance must NOT flip the broad gate


def test_allowlisted_rejects_unknown_blob():
    assert files.allowlisted(b"definitely not a verified image" * 4) is None


@pytest.mark.skipif(not (os.path.isfile(_STOCK_OTA) and os.path.isfile(_POC_OTA)),
                    reason="stock/POC OTA images not present locally")
def test_allowlist_matches_stock_and_poc_exactly():
    assert files.allowlisted(open(_STOCK_OTA, "rb").read()) == files.FLASH_ALLOWLIST[files.STOCK_V103_SHA256]
    assert files.allowlisted(open(_POC_OTA, "rb").read()) == files.FLASH_ALLOWLIST[files.CFWPOC_SHA256]
    if os.path.isfile(_CFW2_OTA):
        assert files.allowlisted(open(_CFW2_OTA, "rb").read()) == files.FLASH_ALLOWLIST[files.CFW2_SHA256]
    # exact-hash gate: a 1-byte tamper is NOT allowlisted
    tampered = bytearray(open(_STOCK_OTA, "rb").read()); tampered[-1] ^= 0xFF
    assert files.allowlisted(bytes(tampered)) is None


@pytest.mark.skipif(not os.path.isfile(_STOCK_OTA), reason="stock OTA image not present locally")
def test_cli_force_flash_allowlisted_passes_gate_without_recovery(capsys):
    """The narrow allowance: an allowlisted image flashes pre-#17 while RECOVERY_PROVEN stays False."""
    assert files.RECOVERY_PROVEN is False
    ns = build_parser().parse_args(["flash-firmware", _STOCK_OTA, "--force-flash"])
    ns._client = FlashFakeClient()                  # injected 'connected' client (no real BLE)
    rc = asyncio.run(ns.func(ns))
    out = capsys.readouterr().out
    assert rc == 0
    assert "[allowlist]" in out and "stock v1.0.3" in out
    assert "SPARE" in out                           # loud spare-only warning (spare-only governance rider)
    assert ns._client.sent[0][0] == 0xD3 and ns._client.sent[-1][0] == 0xD4   # actually streamed
    assert "ACCEPTED" in out                        # the fake delivered a d4-accept


# =========================================================================== read-only probe
class ProbeFakeClient:
    """Fake for the read-only probe: records sent frames + raw taps, and (optionally) delivers a
    D3 reply to the tap when the D3 query is sent — simulating the watch's notification."""

    def __init__(self, reply=b"\xd3\x00\x00" + b"\x00" * 8, mtu=244):
        self.sent = []
        self.responses = []
        self._raw = []
        self._reply = reply
        self._mtu_payload = mtu

    def add_raw_listener(self, cb):
        self._raw.append(cb)

    def remove_raw_listener(self, cb):
        if cb in self._raw:
            self._raw.remove(cb)

    async def send_raw(self, frame, response=False):
        self.sent.append(bytes(frame))
        self.responses.append(response)
        if bytes(frame) == files.build_d3_query() and self._reply is not None:
            for cb in list(self._raw):        # simulate the watch's raw D3 reply notification
                cb(self._reply)


def test_probe_sends_only_d3_and_parses_state():
    client = ProbeFakeClient()
    result = asyncio.run(files.probe_firmware(client))
    assert client.sent == [files.build_d3_query()]        # EXACTLY one frame — the D3 query
    assert result["state"] == {"staged_offset": 0, "field2": 0}
    assert result["reply"] == b"\xd3\x00\x00" + b"\x00" * 8
    assert client._raw == []                              # the tap was unregistered (finally)


def test_probe_never_sends_firmware_frames():
    client = ProbeFakeClient()
    asyncio.run(files.probe_firmware(client))
    assert all(f[0] not in (0xD1, 0xD2, 0xD4) for f in client.sent), \
        "probe must NEVER emit an announce/chunk/finalize frame"


def test_probe_times_out_cleanly_without_reply():
    client = ProbeFakeClient(reply=None)                  # watch says nothing
    result = asyncio.run(files.probe_firmware(client, timeout=0.05))
    assert result["state"] is None and result["reply"] is None
    assert client.sent == [files.build_d3_query()]        # still only the D3 query went out
    assert client._raw == []                              # tap removed even on timeout


def test_probe_reports_resume_offset():
    reply = b"\xd3\x00\x00" + struct.pack("<II", 84210, 0)  # staged_offset != 0 -> resume point
    result = asyncio.run(files.probe_firmware(ProbeFakeClient(reply=reply)))
    assert result["state"]["staged_offset"] == 84210


def test_cli_probe_is_read_only(tmp_path, capsys):
    ns = _flash_ns(tmp_path, "--probe")
    ns._client = ProbeFakeClient()                        # injected 'connected' client
    rc = asyncio.run(ns.func(ns))
    out = capsys.readouterr().out
    assert rc == 0
    assert "[probe] read-only" in out and "OTA state" in out and "nothing was written" in out
    assert "SINGLE-OWNER" in out                          # single-owner note surfaced
    assert ns._client.sent == [files.build_d3_query()]    # only D3 — never firmware


def test_cli_probe_incompatible_with_force_flash(tmp_path, capsys):
    ns = _flash_ns(tmp_path, "--probe", "--force-flash")
    ns._client = ProbeFakeClient()
    rc = asyncio.run(ns.func(ns))
    assert rc == 2
    assert "cannot be combined" in capsys.readouterr().err
    assert ns._client.sent == []                          # refused before any send


def test_cli_probe_timeout_returns_2(tmp_path, capsys):
    ns = _flash_ns(tmp_path, "--probe")
    ns._client = ProbeFakeClient(reply=None)
    rc = asyncio.run(ns.func(ns))
    assert rc == 2
    assert "no D3 reply" in capsys.readouterr().err


# --- transport raw-PDU tap (the read-only hook the probe relies on) ---
def test_transport_raw_tap_sees_dropped_d3_reply():
    c = StarmaxClient("00:11:22:33:44:55")
    seen = []
    c.add_raw_listener(seen.append)
    d3_reply = bytes.fromhex("d300000000000000000000")
    c._on_notify(None, bytearray(d3_reply))
    assert seen == [d3_reply]         # the tap saw the raw D3 PDU...
    assert c._inbox.empty()           # ...which the C1 reassembler drops (never reaches the inbox)


def test_transport_raw_tap_does_not_disturb_c1_delivery():
    c = StarmaxClient("00:11:22:33:44:55")
    raw_seen, frames = [], []
    c.add_raw_listener(raw_seen.append)
    c.add_listener(frames.append)
    pdu = bytes.fromhex(F.SETTING_REPLY_SEQ82)             # a complete watch->app 0x22 C1 reply
    c._on_notify(None, bytearray(pdu))
    assert raw_seen == [pdu]                               # tap sees the raw pdu...
    assert len(frames) == 1 and frames[0].opcode == 0x22   # ...and the C1 frame still delivers


def test_transport_remove_raw_listener_is_noop_when_absent():
    c = StarmaxClient("00:11:22:33:44:55")
    cb = lambda p: None                                    # noqa: E731
    c.add_raw_listener(cb)
    assert cb in c._raw_listeners
    c.remove_raw_listener(cb)
    assert cb not in c._raw_listeners
    c.remove_raw_listener(cb)                              # second remove must not raise
