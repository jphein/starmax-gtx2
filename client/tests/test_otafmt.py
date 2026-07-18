"""Tests for the FA-EE-EB-DE OTA container parser (:mod:`starmax_client.otafmt`).

SYNTHETIC image vectors only — no real firmware bytes are committed. :func:`build_synth_ota`
mirrors the vendor packer (docs/firmware-ota-byte-map.md): the two integrity CRC-32s are filled
LAST (inner over the DATA, then outer over the whole payload) so a well-formed vector validates;
individual field corruptions then prove each gate independently.

An optional structure-only check runs against the real published stock image if it is present
locally (it lives OUTSIDE this repo, under the research tree) — asserting only the byte-map's
already-published constants, never anything private.
"""
from __future__ import annotations

import os
import struct
import zlib

import pytest

from starmax_client import otafmt


def build_synth_ota(inner_data: bytes, *, trailing_zeros: int = 0,
                    outer_name: str = "zephyr.bin", inner_name: str = "firmware/zephyr.bin",
                    version: int = 0x0FD9, section_magic: int = otafmt.SECTION_MAGIC) -> bytes:
    """Build a minimal VALID FA-EE-EB-DE container around ``inner_data`` (synthetic, correct CRCs)."""
    data_len = len(inner_data)
    payload = inner_data + b"\x00" * trailing_zeros          # payload = inner DATA (+ optional pad)
    buf = bytearray(otafmt.INNER_DATA + len(payload))
    buf[0:4] = otafmt.MAGIC
    # outer header
    struct.pack_into("<I", buf, otafmt.OFF_TOTAL_SIZE, len(buf) + otafmt.OUTER_HDR_LEN)
    struct.pack_into("<I", buf, otafmt.OFF_FILE_COUNT, 1)
    struct.pack_into("<I", buf, otafmt.OFF_VERSION, version)
    buf[otafmt.OFF_OUTER_NAME:otafmt.OFF_OUTER_NAME + len(outer_name)] = outer_name.encode()
    struct.pack_into("<I", buf, otafmt.OFF_PAYLOAD_OFF, otafmt.OUTER_HDR_LEN)
    struct.pack_into("<I", buf, otafmt.OFF_PAYLOAD_LEN, len(buf) - otafmt.OUTER_HDR_LEN)
    # inner section-0 header
    struct.pack_into("<I", buf, otafmt.OFF_SECTION_MAGIC, section_magic)
    struct.pack_into("<I", buf, otafmt.OFF_INNER_LEN, data_len)
    struct.pack_into("<I", buf, otafmt.OFF_INNER_LEN2, data_len)
    buf[otafmt.OFF_INNER_NAME:otafmt.OFF_INNER_NAME + len(inner_name)] = inner_name.encode()
    # DATA
    buf[otafmt.INNER_DATA:] = payload
    # integrity CRCs — inner first (over the DATA), outer LAST (covers the whole payload, incl the
    # inner CRC field). Mirrors the byte-map §6 repack order.
    inner_crc = zlib.crc32(bytes(buf[otafmt.INNER_DATA:otafmt.INNER_DATA + data_len])) & 0xFFFFFFFF
    struct.pack_into("<I", buf, otafmt.OFF_INNER_CRC, inner_crc)
    outer_crc = zlib.crc32(bytes(buf[otafmt.OUTER_HDR_LEN:])) & 0xFFFFFFFF
    struct.pack_into("<I", buf, otafmt.OFF_OUTER_CRC, outer_crc)
    return bytes(buf)


# --------------------------------------------------------------------------- valid vectors
def test_valid_container_parses_and_validates():
    img = otafmt.parse_ota_image(build_synth_ota(b"THE-INNER-ZEPHYR-IMAGE" * 8))
    assert img.ok_magic and img.valid and img.problems() == []
    assert img.outer_crc_ok and img.inner_crc_ok and img.inner_range_ok
    assert img.outer_name == "zephyr.bin" and img.inner_name == "firmware/zephyr.bin"
    assert img.version_flags == 0x0FD9
    assert img.section_magic == otafmt.SECTION_MAGIC == 0xA578875A  # fixed Actions magic, gates valid
    assert img.section_magic_ok


def test_valid_with_trailing_zero_padding():
    # the real image's declared payload runs to EOF incl a trailing zero pad; outer CRC covers it.
    img = otafmt.parse_ota_image(build_synth_ota(b"abcd" * 16, trailing_zeros=56))
    assert img.valid and img.total_size_ok and img.payload_len_ok


# --------------------------------------------------------------------------- format errors
def test_bad_magic_raises():
    bad = bytearray(build_synth_ota(b"x" * 64)); bad[0] ^= 0xFF
    with pytest.raises(otafmt.OtaFormatError):
        otafmt.parse_ota_image(bytes(bad))


def test_too_short_raises():
    with pytest.raises(otafmt.OtaFormatError):
        otafmt.parse_ota_image(otafmt.MAGIC + b"\x00" * 8)


# --------------------------------------------------------------------------- integrity gates
def test_corrupt_inner_data_fails_inner_crc():
    b = bytearray(build_synth_ota(b"payload-bytes" * 8))
    b[otafmt.INNER_DATA + 3] ^= 0xFF             # flip a DATA byte -> inner (and outer) CRC break
    img = otafmt.parse_ota_image(bytes(b))
    assert img.ok_magic and not img.inner_crc_ok and not img.valid
    assert any("inner CRC32" in p for p in img.problems())


def test_corrupt_outer_only_fails_outer_crc():
    # the reserved bytes @0x30 sit in the outer-CRC range [0x2C:] but OUTSIDE the inner DATA [0x6C:]
    # AND are not a gating field — flipping one breaks ONLY the outer gate, isolating the two CRCs.
    b = bytearray(build_synth_ota(b"payload" * 10))
    b[0x31] ^= 0xFF
    img = otafmt.parse_ota_image(bytes(b))
    assert not img.outer_crc_ok and not img.valid
    assert img.inner_crc_ok and img.section_magic_ok   # inner DATA + section magic untouched
    assert any("outer CRC32" in p for p in img.problems())


def test_wrong_section_magic_fails_validity():
    # section magic @0x2C is packed BEFORE the CRCs are computed, so the CRCs stay self-consistent
    # — this isolates the section-magic gate from the CRC gates.
    img = otafmt.parse_ota_image(build_synth_ota(b"w" * 40, section_magic=0xDEADBEEF))
    assert img.outer_crc_ok and img.inner_crc_ok       # CRCs verify...
    assert not img.section_magic_ok and not img.valid  # ...but the section magic is wrong
    assert any("section magic" in p for p in img.problems())


def test_inner_range_overrun_is_invalid():
    b = bytearray(build_synth_ota(b"z" * 32))
    struct.pack_into("<I", b, otafmt.OFF_INNER_LEN, 0x7FFFFFFF)  # absurd declared inner length
    img = otafmt.parse_ota_image(bytes(b))
    assert not img.inner_range_ok and not img.valid
    assert any("overruns" in p for p in img.problems())


# --------------------------------------------------------------------------- non-fatal warnings
def test_size_field_mismatch_warns_without_failing_valid():
    b = bytearray(build_synth_ota(b"payload" * 10))
    # total_size@0x08 is < 0x2C, so it is NOT in the outer-CRC range — changing it keeps the image
    # VALID (integrity intact) but trips the single-sample size-field warning.
    struct.pack_into("<I", b, otafmt.OFF_TOTAL_SIZE, 0xDEADBEEF)
    img = otafmt.parse_ota_image(bytes(b))
    assert img.valid and not img.total_size_ok
    assert any("total_size" in w for w in img.warnings())


# --------------------------------------------------------------------------- real image (optional)
# The one published stock image lives OUTSIDE this repo (research tree). If present, confirm our
# parser reproduces the byte-map's already-published constants — no private data is asserted.
_REAL_OTA = os.path.join(os.path.dirname(__file__), "..", "..", "firmware",
                         "cb05_yhzn01_v1.0.3_20241218_02.ota")


@pytest.mark.skipif(not os.path.isfile(_REAL_OTA), reason="stock OTA image not present locally")
def test_real_stock_image_matches_byte_map():
    img = otafmt.parse_ota_image(open(_REAL_OTA, "rb").read())
    assert img.valid                                            # both CRC-32 gates reproduce
    assert img.filesize == 2148064
    assert img.outer_crc_stored == 0xE6E6BB23                  # byte-map §2 [C]
    assert img.inner_crc_stored == 0xE78749BE                  # byte-map §3 [C]
    assert img.section_magic == 0xA578875A and img.section_magic_ok   # byte-map §4 (fixed magic)
    assert img.outer_name == "zephyr.bin" and img.inner_name == "firmware/zephyr.bin"
