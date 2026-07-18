#!/usr/bin/env python3
"""Repack an Actions "FA EE EB DE" OTA container with a modified app payload.

Splices a new section-0 payload (the inner firmware/zephyr.bin region — what
ota_unpack.py exports as section0_firmware_zephyr.bin) into the stock container,
then fixes the checksums/lengths per docs/firmware-ota-byte-map.md:

  * inner data_crc32 @0x38 = zlib.crc32(out[0x6C : 0x6C+data_len])
  * outer payload_crc32 @0x04 = zlib.crc32(out[0x2C : EOF])   <- recomputed LAST
  * total_size @0x08 = filesize + 0x2C   (and length@0x28, data_len@0x3C/@0x40 if size changed)
  * checksum_A @0x2C is LEFT UNTOUCHED (unresolved field; coverage undetermined)

    scripts/ota_repack.py <orig.ota> <new-app-payload> <out.ota>

Stdlib only (zlib, struct).

################################################################################
#  WARNING: produces images for a DEFERRED, recovery-gated device flash test   #
#  only — do NOT flash without a proven unbrick path.                          #
################################################################################
"""
import argparse
import os
import struct
import sys
import zlib

MAGIC = b"\xFA\xEE\xEB\xDE"
OFF_OUTER_CRC = 0x04
OFF_TOTAL_SIZE = 0x08
OFF_PAYLOAD_OFF = 0x24
OFF_PAYLOAD_LEN = 0x28
OUTER_HDR_LEN = 0x2C
OFF_CHECKSUM_A = 0x2C     # <- UNRESOLVED: never written
OFF_INNER_CRC = 0x38
OFF_INNER_LEN = 0x3C
OFF_INNER_LEN2 = 0x40
INNER_DATA = 0x6C

BANNER = (
    "############################################################################\n"
    "#  WARNING: this produces a firmware image for a DEFERRED, recovery-gated   #\n"
    "#  device flash test ONLY. Do NOT flash without a proven unbrick path.      #\n"
    "#  checksum_A @0x2C is left untouched (unresolved); device-accept unknown.  #\n"
    "############################################################################"
)


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def put32(b, o, v):
    struct.pack_into("<I", b, o, v & 0xFFFFFFFF)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Repack a FA-EE-EB-DE OTA image with a modified section-0 payload.",
        epilog="DEFERRED/recovery-gated: do NOT flash the output without a proven unbrick path.",
    )
    ap.add_argument("orig", help="stock/original .ota (source of headers + sdfs tail)")
    ap.add_argument("payload", help="new section-0 payload (edited firmware/zephyr.bin region)")
    ap.add_argument("out", help="output .ota")
    args = ap.parse_args()

    print(BANNER, file=sys.stderr)

    for p in (args.orig, args.payload):
        if not os.path.isfile(p):
            sys.exit(f"no such file: {p}")
    orig = open(args.orig, "rb").read()
    if orig[:4] != MAGIC:
        sys.exit(f"orig is not a FA-EE-EB-DE container (magic={orig[:4].hex()})")
    if u32(orig, OFF_PAYLOAD_OFF) != OUTER_HDR_LEN:
        sys.exit(f"unexpected payload offset @0x24 = 0x{u32(orig, OFF_PAYLOAD_OFF):X} (expected 0x2C)")

    old_len = u32(orig, OFF_INNER_LEN)
    new = open(args.payload, "rb").read()
    new_len = len(new)

    # splice: [outer + inner-s0 headers] + [new payload] + [original sdfs tail + trailing]
    tail = orig[INNER_DATA + old_len:]
    out = bytearray(orig[:INNER_DATA] + new + tail)

    size_changed = new_len != old_len
    # length/size fields (idempotent when size unchanged)
    put32(out, OFF_INNER_LEN, new_len)                 # @0x3C
    put32(out, OFF_INNER_LEN2, new_len)                # @0x40 (assume load==stored; single-sample)
    put32(out, OFF_PAYLOAD_LEN, len(out) - OUTER_HDR_LEN)  # @0x28
    put32(out, OFF_TOTAL_SIZE, len(out) + OUTER_HDR_LEN)   # @0x08

    # checksum_A @0x2C: intentionally NOT touched.

    # inner CRC first (it lives inside the outer-CRC range) ...
    inner_crc = zlib.crc32(out[INNER_DATA:INNER_DATA + new_len]) & 0xFFFFFFFF
    put32(out, OFF_INNER_CRC, inner_crc)
    # ... outer CRC LAST (covers 0x2C..EOF, incl. @0x38 and @0x2C)
    outer_crc = zlib.crc32(out[OUTER_HDR_LEN:]) & 0xFFFFFFFF
    put32(out, OFF_OUTER_CRC, outer_crc)

    outdir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(outdir, exist_ok=True)
    open(args.out, "wb").write(out)

    # self-consistency check on what we wrote
    chk = open(args.out, "rb").read()
    assert chk[:4] == MAGIC
    ok = (u32(chk, OFF_INNER_CRC) == (zlib.crc32(chk[INNER_DATA:INNER_DATA + new_len]) & 0xFFFFFFFF)
          and u32(chk, OFF_OUTER_CRC) == (zlib.crc32(chk[OUTER_HDR_LEN:]) & 0xFFFFFFFF))

    identical = bytes(out) == orig
    print(f"repacked -> {args.out} ({len(out)} bytes)")
    print(f"  payload: {old_len} -> {new_len} bytes ({'SIZE CHANGED' if size_changed else 'same size'})")
    print(f"  inner crc @0x38 = 0x{inner_crc:08X}")
    print(f"  outer crc @0x04 = 0x{outer_crc:08X}  (recomputed last)")
    print(f"  checksum_A @0x2C left as 0x{u32(chk, OFF_CHECKSUM_A):08X} (untouched)")
    print(f"  self-consistency: {'OK' if ok else 'FAILED'}")
    print(f"  byte-identical to orig: {identical}")
    if size_changed:
        print("  NOTE: size changed -> patched single-sample-inferred fields "
              "(@0x3C/@0x40/@0x28/@0x08); unvalidated until a device-accept test.", file=sys.stderr)
    if not ok:
        sys.exit("self-consistency check FAILED")


if __name__ == "__main__":
    main()
