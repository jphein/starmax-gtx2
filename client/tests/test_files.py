"""B4 files.py tests — byte-exact vs captures where they exist, else structural.

Byte-exact ground truth (docs/firmware-dfu.md §B, tests/fixtures.py):
  * CRC-16/XMODEM check value 0x31C3 for "123456789";
  * the res.ota D1/D2#0/D4 frames + running-CRC checkpoints vs the REAL image
    (skipped if firmware/*.ota is absent — it is gitignored);
  * the 0x16 dial-list reply (DIAL_LIST_C1 + _C3) parsed to its watch-face filenames.
"""
import os

import pytest

from starmax_client import framing
from starmax_client.commands import files as FL
from starmax_client.protobuf import ProtobufWriter as W, parse as pb_parse
from tests import fixtures as F


def _pb(payload):
    return {f: v for f, _w, v in pb_parse(payload)}

_IMAGE = os.path.join(os.path.dirname(__file__), "..", "..", "firmware",
                      "cb05_yhzn01_v1.0.3_20241218_02.ota")


# --------------------------------------------------------------- CRC-16/XMODEM
def test_crc16_xmodem_check_value():
    # Canonical CRC-16/XMODEM check: crc("123456789") == 0x31C3.
    assert FL.crc16_xmodem(b"123456789") == 0x31C3
    assert FL.crc16_xmodem(b"") == 0x0000


# --------------------------------------------------------------- bulk plane (byte-exact)
def test_d3_query_bytes():
    assert FL.build_d3_query() == bytes.fromhex("d300")


def test_d1_announce_res_ota_matches_capture():
    # docs/firmware-dfu.md §B.2: d1 00 e0c62000 e0c62000 0f "res.ota"\0
    frame = FL.build_d1_announce("res.ota", 0x20C6E0)
    assert frame.hex() == "d100" + "e0c62000" + "e0c62000" + "0f" + b"res.ota".hex() + "00"


def test_d2_chunk_bytes():
    assert FL.build_d2_chunk(0, bytes.fromhex("faeeebde")) == bytes.fromhex("d200faeeebde")
    assert FL.build_d2_chunk(0x105 & 0xFF, b"x") == bytes([0xD2, 0x05]) + b"x"
    with pytest.raises(ValueError):
        FL.build_d2_chunk(0, b"\x00" * 235)  # > 234


def test_d4_finalize_matches_capture():
    # §B.2: d4 00 00 0daa0000  (whole-file CRC-16/XMODEM 0xAA0D in a LE u32)
    assert FL.build_d4_finalize(0xAA0D).hex() == "d400000daa0000"


def test_bulk_reply_parsers():
    # final D2 ack from §B.2: d2 00 00 e0c62000 0daa0000
    ack = FL.parse_d2_ack(bytes.fromhex("d20000" + "e0c62000" + "0daa0000"))
    assert ack == {"offset": 0x20C6E0, "crc": 0xAA0D}
    d3 = FL.parse_d3_reply(bytes.fromhex("d30000" + "00000000" + "00000000"))
    assert d3 == {"staged_offset": 0, "field2": 0}
    assert FL.parse_d1_ack(bytes.fromhex("d10000")) is True
    assert FL.parse_d4_ack(bytes.fromhex("d40000")) is True
    assert FL.parse_d4_ack(bytes.fromhex("d40001")) is False


def test_plan_bulk_transfer_structure():
    data = bytes(range(256)) * 3  # 768 B -> ceil(768/234)=4 chunks
    frames = FL.plan_bulk_transfer("test.bin", data)
    assert frames[0] == FL.build_d3_query()
    assert frames[1] == FL.build_d1_announce("test.bin", 768)
    body = frames[2:-1]
    assert len(body) == 4                                  # 4 D2 chunks
    assert [f[1] for f in body] == [0, 1, 2, 3]            # incrementing counters
    assert b"".join(f[2:] for f in body) == data           # payload reconstructs
    assert frames[-1] == FL.build_d4_finalize(FL.crc16_xmodem(data))


# --------------------------------------------------------------- real-image byte-exact
@pytest.mark.skipif(not os.path.isfile(_IMAGE), reason="gitignored firmware image absent")
def test_ota_plan_matches_real_image():
    img = open(_IMAGE, "rb").read()
    assert len(img) == 2148064
    # whole-file + windowed running CRCs verified in docs/firmware-dfu.md §B.1
    assert FL.crc16_xmodem(img) == 0xAA0D
    assert FL.crc16_xmodem(img[:3510]) == 0xD1F8
    assert FL.crc16_xmodem(img[:7020]) == 0xB037
    assert FL.crc16_xmodem(img[:10530]) == 0x59BE
    frames = FL.plan_ota(img)
    assert frames[1] == FL.build_d1_announce("res.ota", len(img))   # D1
    assert frames[2] == FL.build_d2_chunk(0, img[:234])             # chunk #0
    assert frames[2].hex().startswith("d200faeeebde")              # image header
    assert frames[-1].hex() == "d400000daa0000"                    # D4


# --------------------------------------------------------------- dial list 0x16 (byte-exact)
def test_dial_list_request_structure():
    frame = FL.build_dial_list_request(seq=0x30)
    fr = framing.parse_frame(frame)
    assert fr.opcode == FL.OP_DIAL_LIST and fr.flag == 0
    assert _pb(fr.payload) == {1: 0}


# the canonical reassembled 0x16 reply = C1 + C3-payload (240 + 15 = 255 bytes)
_DIAL_FRAME = bytes.fromhex(F.DIAL_LIST_C1) + bytes.fromhex(F.DIAL_LIST_C3)[2:]


def test_parse_dial_list_reply_from_capture():
    # reassemble the captured C1 + C3 fragments, parse the 0x16 inventory reply
    rasm = framing.Reassembler(direction=framing.DIR_WATCH_TO_APP)
    out = rasm.feed(bytes.fromhex(F.DIAL_LIST_C1))
    out += rasm.feed(bytes.fromhex(F.DIAL_LIST_C3))
    assert len(out) == 1 and out[0].raw == _DIAL_FRAME
    reply = FL.parse_dial_list_reply(out[0].payload)
    # installed set (filenames published in docs/protocol-spec.md §3.10)
    assert reply["count"] == 7
    assert "YHZN_1021@LC.bin" in reply["filenames"]
    assert "num061109_10.bin" in reply["filenames"]
    assert any(n.startswith("CW06G_187") for n in reply["filenames"])
    # active dial + storage (the installed/active/storage triple, issue #10)
    assert reply["active_dial"] == "YHZN_1021@LC.bin"
    assert reply["storage_total"] == 3145728 and reply["storage_used"] == 2457600
    e0 = reply["entries"][0]
    assert e0["slot"] == 1 and e0["size"] == 262144 and e0["filename"] == "YHZN_1021@LC.bin"


def test_parse_dial_list_reply_exposes_capacity():
    # [FW] f18 all_plate_support_max = the MAX faces the watch stores; the captured daily unit
    # reported 12 with 7 installed -> 5 free slots. This is the mission's install-barrier signal.
    rasm = framing.Reassembler(direction=framing.DIR_WATCH_TO_APP)
    out = rasm.feed(bytes.fromhex(F.DIAL_LIST_C1))
    out += rasm.feed(bytes.fromhex(F.DIAL_LIST_C3))
    reply = FL.parse_dial_list_reply(out[0].payload)
    assert reply["max_dials"] == 12
    assert reply["count"] == 7
    assert reply["slots_free"] == 5
    # per-category counts (RAW firmware field names) — surfaced so the install barrier can be read
    # as GLOBAL (max_dials) vs a per-type cap. Fixture header: f4=12,f5=7,f6=1,f8=1,f17=3,f18=12.
    c = reply["counts"]
    assert c["cloud_plate_num"] == 12 and c["user_cloud_plate_num"] == 7
    assert c["photo_plate_num"] == 1 and c["wallpaper_plate_num"] == 1
    assert c["plate_photo_pic_support_num"] == 3 and c["all_plate_support_max"] == 12
    assert c["user_photo_plate_num"] is None  # absent in this capture


def test_build_dial_delete_structure():
    # DELETE = 0x16 operate {f1=DELETE(2), f2=dial_name}. Enum value + field layout are [FW]
    # (byte-exact from the firmware protobuf-c tables — see delete-opcode-RE.md).
    frame = FL.build_dial_delete("custom_id_25022.bin", seq=0x41)
    fr = framing.parse_frame(frame, direction=framing.DIR_APP_TO_WATCH)
    assert fr.opcode == FL.OP_DIAL_LIST and fr.flag == 0
    assert _pb(fr.payload) == {1: FL.DIAL_OP_DELETE, 2: b"custom_id_25022.bin"}
    assert FL.DIAL_OP_INQUIRE == 0 and FL.DIAL_OP_SET == 1 and FL.DIAL_OP_DELETE == 2


def test_build_dial_delete_rejects_empty_name():
    with pytest.raises(ValueError):
        FL.build_dial_delete("")


def _refragment(frame: bytes, mtu: int, seq: int) -> list:
    """Re-split a whole watch->app frame into wire PDUs like the watch does at ``mtu``:
    C1 first, 0xC2 middles, 0xC3 last (payload past a 2-byte [type][seq] header)."""
    pdus = [frame[:mtu]]
    rest, step = frame[mtu:], mtu - 2
    chunks = [rest[i:i + step] for i in range(0, len(rest), step)]
    for i, ch in enumerate(chunks):
        typ = framing.CONT if i == len(chunks) - 1 else framing.MIDDLE  # last=C3 middles=C2
        pdus.append(bytes([typ, seq]) + ch)
    return pdus


def test_dial_list_low_mtu_reassembly():
    # Issue #14: at the low default ATT MTU the 0x16 reply spans C1 + C2* + C3; the pre-fix
    # reassembler choked on 0xC2 ("orphan C3"). Confirm the 0xC2 fix (framing 871de53) +
    # MTU handling reassemble it byte-identically across MTUs, from tiny (many middles) up.
    assert len(_DIAL_FRAME) == 255
    for mtu in (23, 32, 64, 128, 240):
        pdus = _refragment(_DIAL_FRAME, mtu, seq=_DIAL_FRAME[1])
        assert pdus[0][0] == framing.SOF
        assert all(p[0] == framing.MIDDLE for p in pdus[1:-1])   # middles are 0xC2
        assert pdus[-1][0] == framing.CONT                        # last is 0xC3
        r = framing.Reassembler(direction=framing.DIR_WATCH_TO_APP)
        done = []
        for p in pdus:
            done += r.feed(p)
        assert len(done) == 1, f"mtu={mtu}: got {len(done)} frames"
        assert done[0].raw == _DIAL_FRAME                         # byte-identical
        reply = FL.parse_dial_list_reply(done[0].payload)
        assert reply["count"] == 7 and reply["active_dial"] == "YHZN_1021@LC.bin"


def test_orphan_continuation_raises():
    # a stray continuation with no open C1 is a hard error (0xC2 now recognised, not
    # "unexpected byte"); this is the guard that used to fire as the "orphan C3" warning.
    for marker in (framing.MIDDLE, framing.CONT):
        r = framing.Reassembler(direction=framing.DIR_WATCH_TO_APP)
        with pytest.raises(framing.FrameError):
            r.feed(bytes([marker, 0x86, 0x00]))


def test_binary_0e_record_large_reassembly():
    """Issue #14 residual: a large BINARY 0x0e flag=1 health record (e.g. cat1 ~715 B) spans
    C1 + 0xC2* + 0xC3. Empirically (every 0x0e flag=1 frame across captures/) a flag=1 record
    carries NO CRC and its LEN == total, so ``_declared_total`` returns LEN and the record must
    reassemble byte-exact with no orphan/truncation. (flag=0 0x0e frames are CRC'd, LEN==total-2.)
    This locks the binary-record reassembly path across MTUs 23..240 (23 = many 0xC2 middles).
    """
    total = 715
    payload = bytes(i & 0xFF for i in range(total - framing.HEADER_LEN))
    hdr = bytes([framing.SOF, 0x88, framing.DIR_WATCH_TO_APP, framing.PROTO_VER,
                 1, framing.OP_HEALTH_SYNC, total & 0xFF, (total >> 8) & 0xFF, 0, 0, 0])
    frame = hdr + payload
    assert len(frame) == total
    for mtu in (23, 64, 128, 240):
        pdus = _refragment(frame, mtu, seq=frame[1])
        assert all(p[0] == framing.MIDDLE for p in pdus[1:-1]) and pdus[-1][0] == framing.CONT
        r = framing.Reassembler(direction=framing.DIR_WATCH_TO_APP)
        done = []
        for p in pdus:
            done += r.feed(p)
        assert len(done) == 1, f"mtu={mtu}: got {len(done)} frames"
        fr = done[0]
        assert fr.raw == frame and fr.is_binary and fr.crc_ok is None    # binary: no CRC
        assert fr.opcode == framing.OP_HEALTH_SYNC and fr.flag == 1
        assert fr.payload == payload                                     # byte-exact, no loss


def test_live_cat1_record_reassembly_and_dup_orphan_cause():
    """Reproduces the issue #14 live sync-run1 cat-1 orphan, PII-free.

    The real seq=0x82 record is a 888-byte 0x0e flag=1 binary frame whose C1 LEN field ==
    888 (== the exact reassembled total: C1[240] + C2[240] + C2[240] + C3[174]). Structural
    preamble is the real one; the HR data region is synthetic (no biometrics committed).

    Proves: (a) with each fragment delivered ONCE the reassembler is correct — LEN==total,
    one frame, ZERO orphans; (b) the live "orphan continuation" was purely the DOUBLE
    DELIVERY (each notification arrived twice: C1,C1,C2,C2,C2,C2,C3,C3) — dup C2/C3 landing
    after the frame already completed. i.e. a transport dedup concern, NOT a framing LEN bug.
    """
    total = 888
    preamble = bytes.fromhex("030020e8060100280000000000000000")  # real record header shape, no bio
    payload = preamble + bytes(total - framing.HEADER_LEN - len(preamble))  # synthetic body
    hdr = bytes([framing.SOF, 0x82, framing.DIR_WATCH_TO_APP, framing.PROTO_VER,
                 1, framing.OP_HEALTH_SYNC, total & 0xFF, (total >> 8) & 0xFF, 0, 0, 0])
    frame = hdr + payload
    assert len(frame) == total and (frame[6] | (frame[7] << 8)) == total   # LEN == total (diff 0)

    # (a) clean (de-duped) fragments at the real ~240-B PDU size -> one frame, no orphan
    pdus = _refragment(frame, 240, seq=0x82)   # -> C1[240] C2[240] C2[240] C3[174]
    assert [len(p) for p in pdus] == [240, 240, 240, 174]
    r = framing.Reassembler(direction=framing.DIR_WATCH_TO_APP)
    done, orph = [], 0
    for p in pdus:
        done += r.feed(p)
    assert len(done) == 1 and done[0].raw == frame and done[0].payload == payload

    # (b) double-delivered fragments reproduce the live orphan (transport artifact)
    r2 = framing.Reassembler(direction=framing.DIR_WATCH_TO_APP)
    comps = 0
    for p in pdus:
        for _dup in (p, p):                     # each notification arrives twice
            try:
                comps += len(r2.feed(_dup))
            except framing.FrameError as e:
                if "orphan" in str(e):
                    orph += 1
    assert orph > 0            # dups DO cause orphan warnings ...
    # ... which is why the fix belongs in transport (dedup), not framing (LEN is correct).


# --------------------------------------------------------------- schema-derived (structural)
def test_dial_switch_structure():
    fr = framing.parse_frame(FL.build_dial_switch(5123, color=2, align=1, seq=1))
    assert fr.opcode == FL.OP_DIAL_SET
    top = _pb(fr.payload)
    sub = _pb(top[2])
    assert top[1] == 2 and sub == {1: 1, 2: 5123, 3: 2, 4: 1}


def test_parse_dial_info():
    from starmax_client.protobuf import ProtobufWriter as W
    info = W().varint(1, 1).varint(2, 25022).varint(3, 7).varint(4, 0).to_bytes()
    payload = W().varint(1, 0).message(2, info).to_bytes()
    d = FL.parse_dial_info(payload)
    assert d["status"] == 0 and d["infos"][0] == {"selected": 1, "dial_id": 25022,
                                                   "color": 7, "align": 0}


def test_sport_control_and_parse():
    fr = framing.parse_frame(FL.build_sport_control(sport_type=1, status=FL.SPORT_STATUS_START, seq=2))
    assert fr.opcode == FL.OP_SPORT
    assert _pb(fr.payload) == {1: 1, 2: 1}
    from starmax_client.protobuf import ProtobufWriter as W
    ss = W().varint(1, 1).varint(2, 2).varint(11, 3600).to_bytes()
    assert FL.parse_sport_sync(ss)["seconds"] == 3600


def test_parse_gps_sync():
    from starmax_client.protobuf import ProtobufWriter as W
    ele = W().varint(1, 1).varint(2, 100).varint(3, 116_400_000).varint(4, 39_900_000).to_bytes()
    payload = W().varint(1, 0).message(2, ele).bool(3, True).to_bytes()
    g = FL.parse_gps_sync(payload)
    assert g["has_next"] is True and g["points"][0]["lat"] == 39_900_000


def test_nfc_build_and_parse():
    fr = framing.parse_frame(FL.build_nfc_list_request(card_type=1, seq=3))
    assert fr.opcode == FL.OP_NFC and _pb(fr.payload) == {1: 1, 2: 0}
    from starmax_client.protobuf import ProtobufWriter as W
    card = W().varint(1, 2).string(2, "Metro").to_bytes()
    payload = W().varint(1, 0).varint(2, 1).message(3, card).to_bytes()
    info = FL.parse_nfc_card_info(payload)
    assert info["cards"][0] == {"card_type": 2, "card_name": "Metro"}


# --------------------------------------------------------------- CLI contract
def test_register_and_commands_contract():
    import argparse
    assert set(FL.COMMANDS) >= {"dial-list", "dial-switch", "sport-control", "nfc-list"}
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    FL.register(sub, client=None)  # must not raise; client optional
    # dry-run of a single-frame command prints hex and exits 0
    args = p.parse_args(["dial-list", "--dry-run"])
    assert hasattr(args, "func")


def test_cli_dial_list_live_read_prints_capacity(capsys):
    # dial-list is now a live READ: request 0x16, parse the full reply, print count/capacity.
    # This is the authoritative full-list read the node log can't give (it truncates).
    import argparse
    import asyncio
    from types import SimpleNamespace

    class _LiveClient:
        async def request(self, frame, opcode, timeout=5.0):
            entry = (W().varint(1, 1).varint(2, 1).varint(3, 262144)
                     .string(4, "custom_id_25022.bin").to_bytes())
            payload = (W().message(10, entry).string(14, "custom_id_25022.bin")
                       .varint(18, 12).to_bytes())  # f18 all_plate_support_max = 12
            return SimpleNamespace(opcode=opcode, payload=payload)

    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    FL.register(sub, client=None)
    args = p.parse_args(["dial-list"])
    args._client = _LiveClient()  # what cli._run injects when connected
    rc = asyncio.run(args.func(args))
    out = capsys.readouterr().out
    assert rc == 0
    assert "installed dials : 1/12 (11 free)" in out
    assert "active face     : custom_id_25022.bin" in out
    assert "custom_id_25022.bin  (262144 B)" in out
