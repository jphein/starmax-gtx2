#!/usr/bin/env python3
"""Unpack a GTX2 (Actions ATS3085x / Runmefit) watch-face .bin into a working dir.

A GTX2 dial .bin is a ZIP of a `dial.json` manifest + loose assets
(PNG = RGBA8888, BMP = RGB565 target) under a `firmware/` tree, optionally inside
a numeric slot dir (e.g. `2/`). See docs/watchface-format.md.

Usage:
    scripts/dial_unpack.py <dial.bin> <outdir>

Extracts preserving the exact internal paths (including the slot dir) so the tree
can be edited and repacked with scripts/dial_pack.py. Stdlib only (no Pillow).
"""
import argparse
import json
import os
import sys
import zipfile
from collections import Counter


def safe_members(zf: zipfile.ZipFile):
    """Yield entry names, refusing path-traversal / absolute paths (committed-tool hygiene)."""
    for name in zf.namelist():
        norm = name.replace("\\", "/")
        if norm.startswith("/") or ".." in norm.split("/"):
            sys.exit(f"refusing unsafe zip entry: {name!r}")
        yield name


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Unpack a GTX2 watch-face .bin (ZIP) into a working directory.",
        epilog="Then edit dial.json / assets and repack with scripts/dial_pack.py.",
    )
    ap.add_argument("dial_bin", help="input dial .bin (a ZIP archive)")
    ap.add_argument("outdir", help="directory to extract into (created if missing)")
    args = ap.parse_args()

    if not os.path.isfile(args.dial_bin):
        sys.exit(f"no such file: {args.dial_bin}")
    if not zipfile.is_zipfile(args.dial_bin):
        sys.exit(f"not a ZIP archive (GTX2 dials are ZIPs): {args.dial_bin}")

    os.makedirs(args.outdir, exist_ok=True)
    with zipfile.ZipFile(args.dial_bin) as zf:
        names = list(safe_members(zf))
        zf.extractall(args.outdir, members=names)

    files = [n for n in names if not n.endswith("/")]
    first = names[0] if names else ""
    slot = first.split("/")[0] if ("/" in first and not first.startswith("firmware/")) else "(none / flat)"
    exts = Counter(os.path.splitext(n)[1].lower() or "(none)" for n in files)

    dial_json = next((n for n in names if n.endswith("firmware/dial.json")), None)

    print(f"unpacked {len(files)} files -> {args.outdir}")
    print(f"  slot dir : {slot}")
    print(f"  assets   : " + ", ".join(f"{e}:{c}" for e, c in sorted(exts.items())))
    if dial_json:
        with open(os.path.join(args.outdir, dial_json)) as fh:
            d = json.load(fh)
        print(f"  manifest : {dial_json}")
        print(
            f"    name={d.get('name')!r} resolution={d.get('resolution_ratio')} "
            f"platform={d.get('platform')} items={len(d.get('item', []))} "
            f"fade_items={len(d.get('fade_item', []))}"
        )
    else:
        print("  WARNING  : no firmware/dial.json found — is this a GTX2 dial?", file=sys.stderr)


if __name__ == "__main__":
    main()
