"""``FA EE EB DE`` OTA firmware-container parse + integrity validation.

The standalone counterpart to :mod:`starmax_client.dialfmt` (which parses the dial container);
this parses the Actions **OTA image** the watch flashes as ``res.ota``. Layout + both integrity
CRCs are from ``docs/firmware-ota-byte-map.md`` (2026-07-11) — every field there is
either ``[C]`` CRC-brute-reproduced or ``[E]`` single-sample-inferred, analysed READ-ONLY from
the one published image (``cb05_yhzn01_v1.0.3``; only one firmware version exists for this unit).

    OUTER "FA EE EB DE" container (44-byte header @0x00, one TOC entry = whole payload)
     └─ payload [0x2C .. EOF]                          — outer crc32 @0x04 covers ALL of it
         └─ INNER section-0 header [0x2C .. 0x6C]  +  DATA [0x6C .. 0x6C+data_len]
                                                       — inner crc32 @0x38 covers the DATA

Two integrity gates, **both brute-reproduced** (byte-map §TL;DR), both stock CRC-32/ISO-HDLC (zlib):

  * outer ``payload_crc32 @0x04 = zlib.crc32(file[0x2C:EOF])``   (covers the whole payload)
  * inner ``data_crc32    @0x38 = zlib.crc32(file[0x6C:0x6C+data_len])``

The word at ``0x2C`` is a **fixed Actions sub-image MAGIC** ``0xA578875A`` (:data:`SECTION_MAGIC`)
— **not** a checksum (byte-map §4, resolved during firmware RE 2026-07-13: it recurs verbatim as a
code literal inside the Zephyr stream and no CRC/sum reproduces it over any range). So it is
**never recomputed**: a repacked — even app-modified — image leaves it byte-identical; only
``data_crc32@0x38`` and ``payload_crc32@0x04`` change. We treat it as a **format-sanity gate**
(``section_magic_ok``): a valid Actions section-0 descriptor must carry it. There is **no signature
on the app path** — acceptance beyond the two CRC-32s is only a device/version-compat check in the
watch's ``os_ota`` (the ``"firmware partition signature is 0x%04x"`` string is a 16-bit partition
compat word, not crypto and not this field).

Clean-room: STANDALONE lane (Track B). This is a capture/analysis-derived container reader; no
APK/SDK bytes ship here. It reads bytes and verifies checksums — it does **not** repack images
(that is ``scripts/ota_repack.py`` in the research tree) and it does not flash anything.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import List

MAGIC = b"\xFA\xEE\xEB\xDE"

# --- offsets (little-endian u32 unless noted) — docs/firmware-ota-byte-map.md §2-§3 -----------
OUTER_HDR_LEN = 0x2C      # outer header size; payload / inner section-0 header begins here
INNER_DATA = 0x6C         # inner section-0 DATA begins here (end of the 64-byte section-0 header)

OFF_OUTER_CRC = 0x04      # zlib.crc32(file[0x2C:EOF])                     [C]
OFF_TOTAL_SIZE = 0x08     # = filesize + 0x2C                             [E]
OFF_FILE_COUNT = 0x0C     # # of outer TOC entries (1 for this device)    [E]
OFF_VERSION = 0x10        # version/build/flags (not a CRC)               [E]
OFF_OUTER_NAME = 0x14     # TOC entry-0 name, 16 bytes NUL-padded         [C]
OFF_PAYLOAD_OFF = 0x24    # = 0x2C                                        [C]
OFF_PAYLOAD_LEN = 0x28    # = filesize - 0x2C                             [C]
OFF_SECTION_MAGIC = 0x2C  # fixed Actions sub-image MAGIC (see SECTION_MAGIC) — NOT a checksum
OFF_INNER_CRC = 0x38      # zlib.crc32(file[0x6C:0x6C+data_len])          [C]
OFF_INNER_LEN = 0x3C      # inner image length                           [C]
OFF_INNER_LEN2 = 0x40     # duplicate length (orig/load size)            [E]
OFF_INNER_NAME = 0x4C     # section-0 name, 20 bytes                      [C]

# The fixed Actions sub-image magic a valid section-0 descriptor carries at 0x2C (byte-map §4,
# firmware RE 2026-07-13). Format-sanity only — never recomputed, left byte-identical on repack.
SECTION_MAGIC = 0xA578875A


class OtaFormatError(ValueError):
    """Raised when a buffer is not a parseable ``FA EE EB DE`` OTA container."""


def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def _cstr(b: bytes, o: int, n: int) -> str:
    return b[o:o + n].split(b"\x00", 1)[0].decode("latin-1")


@dataclass
class OtaImage:
    """Parsed + integrity-checked view of a ``FA EE EB DE`` OTA image.

    ``valid`` is the integrity gate: correct container magic + section-0 magic
    (:data:`SECTION_MAGIC` @0x2C), **both** brute-reproduced CRC-32s match, and the declared
    inner-data range lies within the file. The section magic is a fixed constant (never
    recomputed), not a content checksum.
    """
    filesize: int
    ok_magic: bool
    # outer header
    outer_crc_stored: int
    outer_crc_computed: int
    total_size_stored: int
    total_size_expected: int
    file_count: int
    version_flags: int
    outer_name: str
    payload_offset: int
    payload_len_stored: int
    payload_len_expected: int
    # inner section-0 header
    section_magic: int       # fixed Actions sub-image MAGIC @0x2C (see SECTION_MAGIC) — not a CRC
    inner_crc_stored: int
    inner_crc_computed: int
    inner_data_len: int
    inner_data_len2: int
    inner_name: str
    inner_range_ok: bool     # 0x6C + data_len <= filesize (inner CRC range fits in the file)

    # --- individual gate results ---------------------------------------------------------------
    @property
    def outer_crc_ok(self) -> bool:
        return self.outer_crc_stored == self.outer_crc_computed

    @property
    def inner_crc_ok(self) -> bool:
        return self.inner_range_ok and self.inner_crc_stored == self.inner_crc_computed

    @property
    def total_size_ok(self) -> bool:
        return self.total_size_stored == self.total_size_expected

    @property
    def payload_len_ok(self) -> bool:
        return self.payload_len_stored == self.payload_len_expected

    @property
    def section_magic_ok(self) -> bool:
        """The section-0 descriptor carries the fixed Actions sub-image magic at 0x2C.

        A format-sanity gate (byte-map §4): it is a constant, NOT a content checksum, so it is
        never recomputed and stays byte-identical across a repack.
        """
        return self.section_magic == SECTION_MAGIC

    # --- overall verdict -----------------------------------------------------------------------
    @property
    def valid(self) -> bool:
        """True iff the image passes the integrity gate: container magic + section magic + both
        CRC-32s + in-range inner length. Excludes the single-sample size fields (see :meth:`warnings`).
        """
        return (self.ok_magic and self.section_magic_ok
                and self.outer_crc_ok and self.inner_crc_ok)

    def problems(self) -> List[str]:
        """Human-readable integrity failures (empty iff :attr:`valid`)."""
        out: List[str] = []
        if not self.ok_magic:
            out.append("bad magic (not a FA-EE-EB-DE container)")
        if not self.section_magic_ok:
            out.append(f"section magic@0x2C 0x{self.section_magic:08X} != 0x{SECTION_MAGIC:08X} "
                       f"(not a valid Actions section-0 descriptor)")
        if not self.inner_range_ok:
            out.append(f"inner data range 0x6C+{self.inner_data_len} overruns the {self.filesize}-byte file")
        if not self.outer_crc_ok:
            out.append(f"outer CRC32@0x04 mismatch: stored 0x{self.outer_crc_stored:08X} "
                       f"!= computed 0x{self.outer_crc_computed:08X}")
        if self.inner_range_ok and not self.inner_crc_ok:
            out.append(f"inner CRC32@0x38 mismatch: stored 0x{self.inner_crc_stored:08X} "
                       f"!= computed 0x{self.inner_crc_computed:08X}")
        return out

    def warnings(self) -> List[str]:
        """Non-fatal, single-sample-inferred field mismatches (do NOT gate ``valid``)."""
        out: List[str] = []
        if not self.total_size_ok:
            out.append(f"total_size@0x08 0x{self.total_size_stored:08X} != expected "
                       f"0x{self.total_size_expected:08X} (filesize+0x2C) [single-sample field]")
        if not self.payload_len_ok:
            out.append(f"payload_len@0x28 0x{self.payload_len_stored:08X} != expected "
                       f"0x{self.payload_len_expected:08X} (filesize-0x2C)")
        return out


def parse_ota_image(data: bytes) -> OtaImage:
    """Parse + integrity-check a ``FA EE EB DE`` OTA image.

    Raises :class:`OtaFormatError` if ``data`` is too short to hold the headers or the magic is
    wrong. A structurally-parseable image whose CRCs do NOT match is returned with ``valid=False``
    (inspect :meth:`OtaImage.problems`) — parsing never raises on a mere integrity failure, so the
    caller can print a full diagnostic before refusing.
    """
    if len(data) < INNER_DATA:
        raise OtaFormatError(f"too small ({len(data)} B) to contain the OTA headers (need >= 0x6C)")
    if data[:4] != MAGIC:
        raise OtaFormatError(f"not a FA-EE-EB-DE container (magic={data[:4].hex()})")

    inner_data_len = _u32(data, OFF_INNER_LEN)
    inner_end = INNER_DATA + inner_data_len
    inner_range_ok = inner_end <= len(data)

    outer_crc_computed = zlib.crc32(data[OUTER_HDR_LEN:]) & 0xFFFFFFFF
    # Only compute the inner CRC over a range that actually fits; if the declared length overruns
    # the file the image is already invalid (inner_range_ok=False) and the value is irrelevant.
    inner_crc_computed = (zlib.crc32(data[INNER_DATA:inner_end]) & 0xFFFFFFFF
                          if inner_range_ok else 0)

    return OtaImage(
        filesize=len(data),
        ok_magic=True,
        outer_crc_stored=_u32(data, OFF_OUTER_CRC),
        outer_crc_computed=outer_crc_computed,
        total_size_stored=_u32(data, OFF_TOTAL_SIZE),
        total_size_expected=len(data) + OUTER_HDR_LEN,
        file_count=_u32(data, OFF_FILE_COUNT),
        version_flags=_u32(data, OFF_VERSION),
        outer_name=_cstr(data, OFF_OUTER_NAME, 16),
        payload_offset=_u32(data, OFF_PAYLOAD_OFF),
        payload_len_stored=_u32(data, OFF_PAYLOAD_LEN),
        payload_len_expected=len(data) - OUTER_HDR_LEN,
        section_magic=_u32(data, OFF_SECTION_MAGIC),
        inner_crc_stored=_u32(data, OFF_INNER_CRC),
        inner_crc_computed=inner_crc_computed,
        inner_data_len=inner_data_len,
        inner_data_len2=_u32(data, OFF_INNER_LEN2),
        inner_name=_cstr(data, OFF_INNER_NAME, 20),
        inner_range_ok=inner_range_ok,
    )
