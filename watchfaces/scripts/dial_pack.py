#!/usr/bin/env python3
"""Pack a working dir into a valid GTX2 (Actions ATS3085x / Runmefit) watch-face .bin.

Bakes in the format gotchas (see docs/watchface-format.md, watchface-custom-build.md):
  * DEFLATE level 9 — NEVER 'store'. Storing bloats the bundle ~4x because the
    24-bit BMP backgrounds don't compress; stock dials are deflated.
  * preserves the exact tree, including the numeric slot dir (2/, 4/) AND the
    directory entries, so the result is structure-equivalent to a stock dial.
  * passes asset bytes through untouched (no re-encoding / mangling).
  * verifies (stdlib only, no Pillow) that dial.json parses, both previews are
    present, and *_0565.bmp backgrounds are authored as 24-bit BMPs.

Usage:
    scripts/dial_pack.py <workdir> <out.bin> [--no-verify]

<workdir> is the directory that CONTAINS the slot dir (or firmware/ for flat
dials) — i.e. the directory scripts/dial_unpack.py extracted into.
"""
import argparse
import json
import os
import struct
import sys
import zipfile


def bmp_bitcount(path: str):
    """Return a BMP's biBitCount (stdlib only), or None if it isn't a BMP."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(30)
        if head[:2] != b"BM" or len(head) < 30:
            return None
        return struct.unpack_from("<H", head, 28)[0]  # biBitCount @ offset 28
    except OSError:
        return None


def collect(workdir: str):
    """Yield (abspath | None, arcname) top-down and sorted.

    Directory entries have abspath None and an arcname ending in '/'.
    """
    workdir = os.path.abspath(workdir)
    for root, dirs, files in os.walk(workdir):
        dirs.sort()
        files.sort()
        rel = os.path.relpath(root, workdir)
        if rel != ".":
            yield None, rel.replace(os.sep, "/") + "/"
        for fn in files:
            ap = os.path.join(root, fn)
            yield ap, os.path.relpath(ap, workdir).replace(os.sep, "/")


def verify(workdir: str, arcnames: set) -> list:
    warns = []
    manifests = [a for a in arcnames if a.endswith("firmware/dial.json")]
    if not manifests:
        warns.append("no firmware/dial.json (is this a GTX2 dial working dir?)")
    else:
        fwdir = os.path.dirname(manifests[0])
        try:
            json.load(open(os.path.join(workdir, manifests[0])))
        except (ValueError, OSError) as exc:
            warns.append(f"dial.json does not parse: {exc}")
        for preview in ("app_preview.png", "preview_0565.bmp"):
            if f"{fwdir}/{preview}" not in arcnames:
                warns.append(f"missing preview '{preview}' (previews are independent assets)")
    for a in arcnames:
        if a.lower().endswith("_0565.bmp"):
            bc = bmp_bitcount(os.path.join(workdir, a))
            if bc is not None and bc != 24:
                warns.append(f"{a}: expected 24-bit BMP (RGB565 target authored as 24-bit), got {bc}-bit")
    return warns


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Pack a working dir into a valid GTX2 watch-face .bin (DEFLATE-9 ZIP).",
        epilog="Repacks what dial_unpack.py extracted. Always DEFLATE (never store).",
    )
    ap.add_argument("workdir", help="dir containing the slot dir (or firmware/ for flat dials)")
    ap.add_argument("out_bin", help="output dial .bin")
    ap.add_argument("--no-verify", action="store_true", help="skip manifest/asset sanity checks")
    args = ap.parse_args()

    if not os.path.isdir(args.workdir):
        sys.exit(f"not a directory: {args.workdir}")

    entries = list(collect(args.workdir))
    arcnames = [arc for _, arc in entries]
    files = [arc for ap_, arc in entries if ap_ is not None]
    if not files:
        sys.exit(f"no files found under {args.workdir}")

    if not args.no_verify:
        for w in verify(args.workdir, set(arcnames)):
            print(f"  WARNING: {w}", file=sys.stderr)

    os.makedirs(os.path.dirname(os.path.abspath(args.out_bin)), exist_ok=True)
    # DEFLATE level 9 for every member — NEVER store.
    with zipfile.ZipFile(args.out_bin, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for ap_, arc in entries:
            if ap_ is None:
                zi = zipfile.ZipInfo(arc)
                zi.external_attr = (0o40775 << 16) | 0x10  # unix dir mode + MS-DOS dir flag
                zf.writestr(zi, b"")
            else:
                zf.write(ap_, arc)

    # self-check the archive we just wrote
    with zipfile.ZipFile(args.out_bin) as zf:
        bad = zf.testzip()
        if bad is not None:
            sys.exit(f"packed archive failed integrity check at: {bad}")

    size = os.path.getsize(args.out_bin)
    print(f"packed {len(files)} files ({len(arcnames)} entries incl. dirs) -> {args.out_bin} ({size} bytes)")
    print("  compression: DEFLATE level 9 (verified integrity OK)")


if __name__ == "__main__":
    main()
