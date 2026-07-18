"""Minimal protobuf writer/reader tests."""
import pytest

from starmax_client import protobuf as pb


@pytest.mark.parametrize("value,encoded", [
    (0, "00"),
    (1, "01"),
    (127, "7f"),
    (128, "8001"),
    (300, "ac02"),
    (2026, "ea0f"),        # the set-time year
    (1140, "f408"),        # set-time f9 constant
    (1783753692, "dcd7c7d206"),  # a real set-time epoch
])
def test_varint_roundtrip(value, encoded):
    enc = pb.encode_varint(value)
    assert enc.hex() == encoded
    dec, pos = pb.decode_varint(enc, 0)
    assert dec == value and pos == len(enc)


def test_truncated_varint_raises():
    with pytest.raises(ValueError):
        pb.decode_varint(b"\x80\x80", 0)  # continuation bit set, no terminator


def test_writer_field_order_preserved():
    w = pb.ProtobufWriter().varint(1, 2).varint(2, 7).varint(3, 11)
    assert w.to_bytes().hex() == "08021007180b"  # f3=11 -> 0x0b


def test_writer_reader_roundtrip_mixed_types():
    w = (pb.ProtobufWriter()
         .varint(1, 42)
         .string(2, "hi")
         .message(3, pb.ProtobufWriter().varint(1, 9))
         .fixed32(4, 0x11223344))
    fields = pb.parse(w.to_bytes())
    assert fields[0] == (1, pb.WIRE_VARINT, 42)
    assert fields[1] == (2, pb.WIRE_LEN, b"hi")
    assert fields[2] == (3, pb.WIRE_LEN, b"\x08\x09")
    assert fields[3] == (4, pb.WIRE_I32, 0x11223344)


def test_nested_message_parse():
    inner = pb.ProtobufWriter().varint(1, 466).varint(2, 466).to_bytes()
    outer = pb.ProtobufWriter().message(19, inner).to_bytes()
    d = pb.to_dict(outer)
    assert pb.to_dict(d[19]) == {1: 466, 2: 466}


def test_repeated_field_iteration():
    w = pb.ProtobufWriter().varint(11, 1).varint(11, 2).varint(11, 3)
    assert list(pb.iter_fields(w.to_bytes(), 11)) == [1, 2, 3]


def test_string_utf8_roundtrip():
    w = pb.ProtobufWriter().string(6, "café ☕")
    val = pb.get(w.to_bytes(), 6)
    assert val.decode("utf-8") == "café ☕"


def test_truncated_length_delimited_raises():
    # tag f1 wire2, length 5, but only 2 bytes follow.
    with pytest.raises(ValueError):
        pb.parse(b"\x0a\x05ab")
