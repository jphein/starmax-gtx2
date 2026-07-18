#!/usr/bin/env python3
"""Unpack an Actions "FA EE EB DE" OTA container for inspection.

Splits the container documented in docs/firmware-ota-byte-map.md into its outer
header fields and inner sections (section 0 = firmware/zephyr.bin, section 1 =
sdfs.bin), and verifies the two stored CRC-32s.

    scripts/ota_unpack.py <image.ota> <outdir>

The exported `section0_firmware_zephyr.bin` is the editable "app payload": edit it
and feed it back to scripts/ota_repack.py. Stdlib only (zlib, struct, json).
"""
import argparse
import json
import os
import struct
import sys
import zlib

MAGIC = b"\xFA\xEE\xEB\xDE"
# offsets (little-endian u32 unless noted) — see docs/firmware-ota-byte-map.md
OFF_OUTER_CRC = 0x04      # zlib.crc32(file[0x2C:EOF])
OFF_TOTAL_SIZE = 0x08     # filesize + 0x2C
OFF_FILE_COUNT = 0x0C
OFF_VERSION = 0x10
OFF_OUTER_NAME = 0x14     # 16 bytes, NUL-padded
OFF_PAYLOAD_OFF = 0x24    # = 0x2C
OFF_PAYLOAD_LEN = 0x28    # filesize - 0x2C
OUTER_HDR_LEN = 0x2C      # outer payload / inner section-0 header begins here
OFF_CHECKSUM_A = 0x2C     # UNRESOLVED (leave untouched on repack)
OFF_INNER_CRC = 0x38      # zlib.crc32(file[0x6C:0x6C+data_len])
OFF_INNER_LEN = 0x3C
OFF_INNER_LEN2 = 0x40
OFF_INNER_NAME = 0x4C     # 20 bytes
INNER_DATA = 0x6C         # inner section-0 data begins here


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def cstr(b, o, n):
    return b[o:o + n].split(b"\x00", 1)[0].decode("latin-1")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Unpack an Actions FA-EE-EB-DE OTA container (inspection).",
        epilog="Edit the exported section0_firmware_zephyr.bin, then repack with scripts/ota_repack.py.",
    )
    ap.add_argument("image", help="input .ota image")
    ap.add_argument("outdir", help="directory to write parts + manifest.json into")
    args = ap.parse_args()

    if not os.path.isfile(args.image):
        sys.exit(f"no such file: {args.image}")
    b = open(args.image, "rb").read()
    if b[:4] != MAGIC:
        sys.exit(f"not a FA-EE-EB-DE container (magic={b[:4].hex()})")
    if len(b) < INNER_DATA:
        sys.exit("file too small to contain the headers")

    os.makedirs(args.outdir, exist_ok=True)

    data_len = u32(b, OFF_INNER_LEN)
    s1 = INNER_DATA + data_len                       # section-1 (sdfs) dir-entry offset
    outer_crc_calc = zlib.crc32(b[OUTER_HDR_LEN:]) & 0xFFFFFFFF
    inner_crc_calc = zlib.crc32(b[INNER_DATA:INNER_DATA + data_len]) & 0xFFFFFFFF

    def chk(off):
        return f"0x{u32(b, off):08X}"

    manifest = {
        "file": os.path.basename(args.image),
        "filesize": len(b),
        "outer_header": {
            "magic": b[:4].hex(),
            "payload_crc32@0x04": {"stored": chk(OFF_OUTER_CRC),
                                    "computed": f"0x{outer_crc_calc:08X}",
                                    "match": u32(b, OFF_OUTER_CRC) == outer_crc_calc},
            "total_size@0x08": {"stored": chk(OFF_TOTAL_SIZE),
                                 "expected": f"0x{len(b) + 0x2C:08X}",
                                 "match": u32(b, OFF_TOTAL_SIZE) == len(b) + 0x2C},
            "file_count@0x0C": u32(b, OFF_FILE_COUNT),
            "version_flags@0x10": chk(OFF_VERSION),
            "name@0x14": cstr(b, OFF_OUTER_NAME, 16),
            "payload_offset@0x24": chk(OFF_PAYLOAD_OFF),
            "payload_length@0x28": {"stored": chk(OFF_PAYLOAD_LEN),
                                     "expected": f"0x{len(b) - 0x2C:08X}",
                                     "match": u32(b, OFF_PAYLOAD_LEN) == len(b) - 0x2C},
        },
        "inner_section0": {
            "checksum_A@0x2C": {"value": chk(OFF_CHECKSUM_A),
                                 "status": "UNRESOLVED - leave untouched on repack (see byte-map §4)"},
            "data_crc32@0x38": {"stored": chk(OFF_INNER_CRC),
                                 "computed": f"0x{inner_crc_calc:08X}",
                                 "match": u32(b, OFF_INNER_CRC) == inner_crc_calc},
            "data_len@0x3C": f"0x{data_len:08X}",
            "data_len_2@0x40": chk(OFF_INNER_LEN2),
            "name@0x4C": cstr(b, OFF_INNER_NAME, 20),
            "data_range": [f"0x{INNER_DATA:X}", f"0x{s1:X}"],
        },
    }

    exports = {}
    parts = {
        "00_outer_header.bin": b[0x00:OUTER_HDR_LEN],
        "01_section0_header.bin": b[OUTER_HDR_LEN:INNER_DATA],
        "section0_firmware_zephyr.bin": b[INNER_DATA:s1],
        "section1_sdfs.bin": b[s1:],
    }
    for name, blob in parts.items():
        open(os.path.join(args.outdir, name), "wb").write(blob)
        exports[name] = len(blob)

    # section-1 (sdfs) descriptor — byte-map §5 (name-first, 32B)
    if s1 + 32 <= len(b):
        manifest["inner_section1_sdfs"] = {
            "dir_entry_offset": f"0x{s1:X}",
            "name": cstr(b, s1, 12),
            "type@+0x0C": f"0x{u32(b, s1 + 0x0C):08X}",
            "size@+0x10": f"0x{u32(b, s1 + 0x10):08X}",
            "word@+0x18": f"0x{u32(b, s1 + 0x18):08X}",
            "word@+0x1C": f"0x{u32(b, s1 + 0x1C):08X}",
            "tail_bytes": len(b) - s1,
        }
    manifest["exports"] = exports
    open(os.path.join(args.outdir, "manifest.json"), "w").write(json.dumps(manifest, indent=2))

    o = manifest["outer_header"]
    print(f"unpacked {args.image} ({len(b)} bytes) -> {args.outdir}")
    print(f"  magic            : {b[:4].hex()}")
    print(f"  outer crc  @0x04 : stored {o['payload_crc32@0x04']['stored']} "
          f"computed {o['payload_crc32@0x04']['computed']}  match={o['payload_crc32@0x04']['match']}")
    print(f"  inner crc  @0x38 : stored {manifest['inner_section0']['data_crc32@0x38']['stored']} "
          f"computed {manifest['inner_section0']['data_crc32@0x38']['computed']}  "
          f"match={manifest['inner_section0']['data_crc32@0x38']['match']}")
    print(f"  checksum_A @0x2C : {manifest['inner_section0']['checksum_A@0x2C']['value']}  (UNRESOLVED)")
    print(f"  section0 (app payload) : section0_firmware_zephyr.bin ({parts['section0_firmware_zephyr.bin'].__len__()} B)")
    print(f"  section1 (sdfs tail)   : section1_sdfs.bin ({parts['section1_sdfs.bin'].__len__()} B)")
    if not (o['payload_crc32@0x04']['match'] and manifest['inner_section0']['data_crc32@0x38']['match']):
        print("  WARNING: a stored CRC did not verify — image may be corrupt or format differs", file=sys.stderr)


if __name__ == "__main__":
    main()
