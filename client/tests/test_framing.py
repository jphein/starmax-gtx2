"""Frame codec tests: build/parse round-trip, CRC verify, 0xC3 reassembly."""
import pytest

from starmax_client import framing, protobuf as pb
from starmax_client.framing import (DIR_APP_TO_WATCH, DIR_WATCH_TO_APP, Frame,
                                     FrameError, Reassembler, build_command,
                                     frame_to_pdus, parse_frame)
from tests import fixtures as F


# ---------------------------------------------------------------- build / header
def test_build_command_header_layout():
    frame = build_command(0x02, b"\xaa\xbb", flag=0, seq=0x07)
    assert frame[0] == 0xC1           # SOF
    assert frame[1] == 0x07           # seq
    assert frame[2] == DIR_APP_TO_WATCH
    assert frame[3] == 0x01           # proto version
    assert frame[4] == 0x00           # flag
    assert frame[5] == 0x02           # opcode
    assert frame[6] | (frame[7] << 8) == len(frame)  # LEN = total
    assert frame[8:11] == b"\x00\x00\x00"


def test_build_command_len_is_16bit_le():
    # A 400-byte payload -> LEN needs the high byte (proves 2-byte LE length).
    frame = build_command(0x12, b"\x00" * 400, seq=1)
    total = len(frame)
    assert frame[6] == (total & 0xFF)
    assert frame[7] == ((total >> 8) & 0xFF)
    assert total == 411


def test_build_command_appends_no_crc():
    # app->watch carries no CRC: total length == header + payload exactly.
    frame = build_command(0x18, b"\x08\x02", seq=1)
    assert len(frame) == framing.HEADER_LEN + 2


# ---------------------------------------------------------------- parse + CRC
def test_parse_real_reply_crc_ok():
    fr = parse_frame(bytes.fromhex(F.SETTING_REPLY_SEQ82))
    assert fr.direction == DIR_WATCH_TO_APP
    assert fr.opcode == 0x22
    assert fr.crc_ok is True
    assert pb.to_dict(fr.payload) == {1: 1, 2: 244}


def test_parse_detects_bad_crc():
    frame = bytearray.fromhex(F.SETTING_REPLY_SEQ82)
    frame[-1] ^= 0xFF  # corrupt the stored CRC
    fr = parse_frame(bytes(frame))
    assert fr.crc_ok is False


def test_parse_command_has_no_crc_flag():
    fr = parse_frame(bytes.fromhex(F.FIND_ON_SEQ0B))
    assert fr.direction == DIR_APP_TO_WATCH
    assert fr.crc_ok is None
    assert pb.to_dict(fr.payload) == {1: 2, 2: 1, 3: 1}


def test_health_sync_binary_record_no_crc():
    # A watch->app 0x0e flag=1 frame is a binary record: LEN=total, no CRC.
    # Synthesize one with a plausible date marker (Shape B, cat 2).
    body = bytes.fromhex("040010022001000000000000ea070b" + "aabbcc")
    total = framing.HEADER_LEN + len(body)
    frame = bytes([0xC1, 0x8e, 0x00, 0x01, 0x01, 0x0e,
                   total & 0xFF, (total >> 8) & 0xFF, 0, 0, 0]) + body
    fr = parse_frame(frame, direction=DIR_WATCH_TO_APP)
    assert fr.is_binary is True
    assert fr.crc_ok is None
    assert fr.payload == body


def test_parse_rejects_non_c1():
    with pytest.raises(FrameError):
        parse_frame(b"\xd2\x00\x01")


def test_build_parse_roundtrip():
    payload = pb.ProtobufWriter().varint(1, 2).varint(2, 1).varint(3, 0).to_bytes()
    frame = build_command(0x18, payload, seq=0x0b)
    fr = parse_frame(frame)
    assert fr.opcode == 0x18 and fr.payload == payload


# ---------------------------------------------------------------- fragmentation
def test_fragmentation_synthetic_roundtrip():
    # A frame larger than the MTU must split into 1x C1 + N x C3 and reassemble exactly.
    frame = build_command(0x12, bytes(range(256)) * 2, seq=0x20)  # 512-byte payload
    pdus = frame_to_pdus(frame, mtu=100)
    assert len(pdus) > 1
    assert pdus[0][0] == framing.SOF
    assert all(p[0] == framing.CONT and p[1] == 0x20 for p in pdus[1:])
    r = Reassembler(direction=DIR_APP_TO_WATCH)
    out = []
    for p in pdus:
        out += r.feed(p)
    assert len(out) == 1
    assert out[0].raw == frame


def test_fragmentation_small_frame_single_pdu():
    frame = build_command(0x01, b"", seq=1)
    assert frame_to_pdus(frame, mtu=200) == [frame]


def test_reassemble_real_0x16_c1_plus_c3():
    # The real fragmented dial-list reply: C1 (240B) + C3 (17B) -> 255B, CRC OK.
    r = Reassembler(direction=DIR_WATCH_TO_APP)
    out = r.feed(bytes.fromhex(F.DIAL_LIST_C1)) + r.feed(bytes.fromhex(F.DIAL_LIST_C3))
    assert len(out) == 1
    fr = out[0]
    assert fr.opcode == 0x16
    assert len(fr.raw) == 255
    assert fr.crc_ok is True
    # The reply lists watch-face filenames (published in spec §3.10).
    assert b"YHZN_1021@LC.bin" in fr.payload


def test_reassemble_c1_c2_c3_middle_fragment():
    # The watch splits large frames as C1 (first) -> C2 (middle) -> C3 (last). Dropping the
    # 0xC2 middle fragment (the bug ported from GB) desynced the stream on any >2-PDU frame
    # (e.g. the live dial-list reply). Verify all three reassemble to the original frame.
    frame = build_command(0x16, bytes(range(200)), seq=0x84)
    mtu = 80
    step = mtu - 2
    c1 = frame[:mtu]
    rest = frame[mtu:]
    c2 = bytes([framing.MIDDLE, 0x84]) + rest[:step]
    c3 = bytes([framing.CONT, 0x84]) + rest[step:]
    r = Reassembler(direction=DIR_APP_TO_WATCH)
    out = r.feed(c1) + r.feed(c2) + r.feed(c3)
    assert len(out) == 1 and out[0].raw == frame


def test_reassembler_orphan_continuation_raises():
    r = Reassembler()
    with pytest.raises(FrameError):
        r.feed(bytes.fromhex(F.DIAL_LIST_C3))  # C3 with no open C1
