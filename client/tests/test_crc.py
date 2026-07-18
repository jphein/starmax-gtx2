"""CRC-16/CCITT-FALSE tests, including the canonical 0x29B1 check value."""
from starmax_client.crc import crc16_ccitt_false, crc16_bytes
from tests import fixtures as F


def test_canonical_check_value():
    # The defining check value for CRC-16/CCITT-FALSE.
    assert crc16_ccitt_false(b"123456789") == 0x29B1


def test_spec_reference_frame():
    # docs/protocol-spec.md §1.2: CRC over the frame body = 0x7c5e.
    body = bytes.fromhex("c182000100221000000000080110f401")
    assert crc16_ccitt_false(body) == 0x7C5E


def test_little_endian_storage():
    # Stored little-endian on the wire: 0x7c5e -> bytes 5e 7c.
    body = bytes.fromhex("c182000100221000000000080110f401")
    assert crc16_bytes(body) == bytes.fromhex("5e7c")


def test_real_reply_trailer_matches():
    frame = bytes.fromhex(F.SETTING_REPLY_SEQ82)
    body, trailer = frame[:-2], frame[-2:]
    assert crc16_bytes(body) == trailer


def test_empty_input():
    # CRC of empty data is just the init value.
    assert crc16_ccitt_false(b"") == 0xFFFF
