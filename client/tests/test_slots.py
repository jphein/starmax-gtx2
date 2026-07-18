"""[CFW] Wire-format + validation tests for starmax_client.slots (opcode 0xA2, design §2.2).

These byte-exact vectors ARE the shared contract with the firmware cfw_slots.{c,h}: if the firmware
0xA2 parser and these disagree, one side is wrong. Offline, transport-independent (twin of
test_crown.py)."""
import struct

import pytest

from starmax_client import framing, slots
from starmax_client.slots import SlotError, SlotVal


def _hdr(frame):
    """(sof, flag, opcode, len_field) from an app->watch frame header."""
    return frame[0], frame[4], frame[5], frame[6] | (frame[7] << 8)


def test_numeric_batch_bytes_exact():
    frame = slots.build_set_slots([SlotVal(0, 124, 1), SlotVal(2, 8400, 0)], seq=7)
    sof, flag, opcode, length = _hdr(frame)
    assert sof == framing.SOF
    assert opcode == slots.OP_CFW_SLOTS == 0xA2
    assert flag == slots.FLAG_NUMERIC == 0x00
    assert frame[1] == 7                                   # seq
    assert length == len(frame)                            # app->watch LEN = whole frame, no CRC
    payload = frame[framing.HEADER_LEN:]
    assert payload[0] == 2                                 # count
    assert payload[1:7] == struct.pack("<BBi", 0, 1, 124)  # {idx,dec,value_le}
    assert payload[7:13] == struct.pack("<BBi", 2, 0, 8400)
    assert len(payload) == 1 + 2 * 6                       # 6 B/slot (design §2.2)


def test_full_8_slot_batch_payload_is_49_bytes():
    frame = slots.build_set_slots([SlotVal(i, i * 100, 0) for i in range(8)])
    assert len(frame) - framing.HEADER_LEN == 49          # design §2.2: 1 + 8*6


def test_negative_value_roundtrips_signed_i32():
    frame = slots.build_set_slots([SlotVal(0, -1234, 2)])
    idx, dec, value = struct.unpack_from("<BBi", frame, framing.HEADER_LEN + 1)
    assert (idx, dec, value) == (0, 2, -1234)


def test_text_frame_bytes_exact():
    frame = slots.build_set_text_slot(1, "GRID OK", seq=3)
    sof, flag, opcode, _ = _hdr(frame)
    assert opcode == slots.OP_CFW_SLOTS and flag == slots.FLAG_TEXT == 0x01 and frame[1] == 3
    payload = frame[framing.HEADER_LEN:]
    assert payload[0] == 1                                 # idx
    assert payload[1] == len(b"GRID OK")                   # len
    assert payload[2:] == b"GRID OK"


def test_text_truncated_to_19_bytes():
    frame = slots.build_set_text_slot(0, "x" * 40)
    payload = frame[framing.HEADER_LEN:]
    assert payload[1] == slots.CFW_TXT_LEN - 1 == 19
    assert len(payload) == 2 + 19


def test_encode_fixed_half_up_sign_preserving():
    assert slots.encode_fixed(12.4, 1) == 124
    assert slots.encode_fixed(1.126, 3) == 1126
    assert slots.encode_fixed(8400, 0) == 8400
    assert slots.encode_fixed(-0.55, 1) == -6              # half-up on magnitude, sign kept


def test_tuple_shorthand_equals_slotval():
    assert slots.build_set_slots([(0, 124, 1)]) == slots.build_set_slots([SlotVal(0, 124, 1)])


def test_validation_rejects_out_of_contract():
    for bad in (SlotVal(8, 0, 0),            # idx >= CFW_NUM_SLOTS
                SlotVal(-1, 0, 0),           # idx < 0
                SlotVal(0, 0, 4),            # dec > DEC_MAX
                SlotVal(0, 2 ** 31, 0)):     # value > int32 max
        with pytest.raises(SlotError):
            slots.build_set_slots([bad])
    with pytest.raises(SlotError):
        slots.build_set_slots([])                          # empty batch
    with pytest.raises(SlotError):
        slots.build_set_slots([SlotVal(i, 0, 0) for i in range(9)])  # over CFW_NUM_SLOTS
    with pytest.raises(SlotError):
        slots.build_set_text_slot(2, "x")                  # text idx >= CFW_TXT_SLOTS
    with pytest.raises(SlotError):
        slots.build_set_text_slot(0, "café")          # non-ASCII


def test_max_slots_per_frame_fits_full_store_at_mtu():
    assert slots.max_slots_per_frame(244) == slots.CFW_NUM_SLOTS   # full batch fits one write
    assert slots.max_slots_per_frame(20) >= 1                      # even at the floor, >=1 slot
