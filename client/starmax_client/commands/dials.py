"""B4b — custom watch-face (dial) INSTALL over the bulk plane.  [CAP]

This module turns "install a custom watch face" into a real CLI action. It is the standalone
counterpart to what the Runmefit app does, decoded **byte-exact** from our own BLE capture
(the app pushing dial ``CWR05G_23687`` as ``custom_id_25022.bin``). Full evidence + protocol
write-up in internal RE notes.

Key facts the capture nailed (all [CAP] — capture-derived, PORTABLE):

  * A dial install is a **bulk-plane push** (``D3 → D1 → D2* → D4``), the *same* transport as
    firmware/AGPS — there is **no** separate C1 announce/stream sub-protocol.
  * The on-wire filename is ``custom_id_<dialId>.bin``; ``field2 == size`` (from-scratch push).
  * The streamed payload is a **transcoded native container** (:mod:`starmax_client.dialfmt`),
    NOT the distributed ZIP ``.bin``.
  * The finalize CRC is ``crc16_xmodem`` over that whole container.
  * The install **auto-activates** — no switch command is needed; the watch makes the freshly
    pushed face active on ``D4`` (verified via the ``0x16`` active-dial field before/after).

Design note (why ``dial-push`` confirms via a ``0x16`` read rather than D2-ack pacing): the
transport's inbound path runs every notification through the C1 ``Reassembler``, which drops
raw D-plane ack frames — so ack-paced flow control needs a transport hook we deliberately do
NOT add here (shared file). v1 streams paced and confirms the install by re-reading the dial
list. See the transport RE notes §1.4 / §4.

Clean-room: STANDALONE lane (Track B). The transfer is [CAP]; the ``dial-activate`` switch
frame reuses the [SCHEMA/INFERRED] ``0x16`` builder from :mod:`~.files` (uncaptured opcode).
"""
from __future__ import annotations

import argparse
import os
from typing import Callable, List, Optional

from starmax_client import dialfmt
from starmax_client.commands import files

GROUP = "dials"

# The bulk-plane D2 payload cap (236-B ATT value = d2 + ctr + 234). Kept in sync with files.
CHUNK_MAX = files.CHUNK_MAX


# =============================================================================
# Wire naming + push planning  [CAP]
# =============================================================================
def dial_wire_filename(dial_id: int) -> str:
    """[CAP] The on-watch filename the D1 announce carries: ``custom_id_<id>.bin``.

    ``dial_id`` is the id the watch keys the installed face by (observed 25022; the SDK's
    custom-dial id space per the vendor SDK opcode map). It is independent of the dial's
    internal manifest ``name``.
    """
    if not (0 < dial_id <= 0xFFFF):
        raise ValueError(f"dial_id out of range (1..65535): {dial_id}")
    return f"custom_id_{dial_id}.bin"


def plan_dial_push(blob: bytes, dial_id: int, *, chunk: int = CHUNK_MAX) -> List[bytes]:
    """[CAP] The full ``[D3, D1, D2*, D4]`` frame sequence for pushing ``blob`` as ``dial_id``.

    ``blob`` is the *native transcoded container* (:func:`starmax_client.dialfmt.build_blob`),
    not the distributed ZIP. Reuses the byte-exact bulk-plane builders in :mod:`~.files`.
    """
    return files.plan_bulk_transfer(dial_wire_filename(dial_id), blob, chunk=chunk)


def load_dial_blob(path: str) -> bytes:
    """Read a dial file and return the native container bytes to stream.

    Accepts either form:
      * a **native blob** (validated by parsing), or
      * a **distributed dial ``.bin`` (ZIP)** — transcoded on the fly into the native container
        via :mod:`starmax_client.dialtranscode` (needs the ``transcode`` extra: Pillow + lz4).

    So ``dial-push some-face.bin`` works whether ``some-face.bin`` is a raw dial ZIP or an
    already-native blob.
    """
    data = open(path, "rb").read()
    if data[:2] == b"PK":
        from starmax_client import dialtranscode  # lazy: only needed for ZIP input
        return dialtranscode.transcode_zip(data)  # TranscodeError (a ValueError) on failure
    dialfmt.parse_blob(data)  # raises DialFormatError if it isn't a valid native container
    return data


# =============================================================================
# Live streaming driver  [CAP]
# =============================================================================
async def push_dial(client, blob: bytes, *, dial_id: int, chunk: int = CHUNK_MAX,
                    confirm: bool = True, on_progress: Optional[Callable[[int, int], None]] = None
                    ) -> dict:
    """Stream ``blob`` to the watch as ``custom_id_<dial_id>.bin`` and (optionally) confirm.

    Returns a result dict ``{sent, total, confirmed, active_dial}``. ``client`` must provide
    async ``send_raw(frame, response=...)`` and (for ``confirm``) async ``request(frame, opcode)``
    — the real :class:`~starmax_client.transport.StarmaxClient` satisfies both; tests inject a fake.

    **Reliable delivery is mandatory.** The D-plane frames are sent **write-WITH-response**
    (``response=True``). This was proven live on hardware: write-without-response fire-hoses the
    989 chunks with no delivery guarantee and overruns the watch (~36% in, then the watch stops
    acking and the tail is dropped → the D4 CRC rejects the incomplete blob). Awaiting each ATT
    write-with-response paces the stream to what the watch can absorb, and the captured dial then
    installs + auto-activates. (A future windowed pacing keyed on the watch's D2 byte-count acks
    is an optional *speed* optimization, not needed for correctness.)

    Raw D-plane frames must go out as single ATT PDUs (no C1 fragmentation). We assert the
    negotiated payload is large enough for a full D2 chunk before sending anything.
    """
    dialfmt.parse_blob(blob)  # fail fast on a malformed container (before touching the radio)
    frames = plan_dial_push(blob, dial_id, chunk=chunk)
    d3, d1, *d2s, d4 = frames

    mtu_payload = getattr(client, "_mtu_payload", 244)
    biggest = max(len(f) for f in frames)
    if biggest > mtu_payload:
        raise RuntimeError(
            f"link payload {mtu_payload}B < largest D-plane frame {biggest}B — a raw D-plane "
            f"frame would be C1-fragmented and corrupt the stream. Reduce --chunk or raise MTU.")

    total = len(blob)
    await client.send_raw(d3, response=True)
    await client.send_raw(d1, response=True)
    sent = 0
    for i, d2 in enumerate(d2s):
        await client.send_raw(d2, response=True)
        sent += len(d2) - 2  # minus the 'd2 <ctr>' header
        if on_progress and (i % 16 == 0 or i == len(d2s) - 1):
            on_progress(min(sent, total), total)
    await client.send_raw(d4, response=True)

    result = {"sent": min(sent, total), "total": total, "confirmed": None, "active_dial": None}
    if confirm:
        want = dial_wire_filename(dial_id)
        reply = await client.request(files.build_dial_list_request(), files.OP_DIAL_LIST)
        if reply is not None:
            info = files.parse_dial_list_reply(reply.payload)
            result["active_dial"] = info.get("active_dial")
            result["confirmed"] = (info.get("active_dial") == want
                                   or want in (info.get("filenames") or []))
    return result


async def push_dial_face(client, background, widgets, *, dial_id: int,
                         aod=None, name: str = "CustomFace", dial_type: int = 1,
                         chunk: int = CHUNK_MAX, confirm: bool = True,
                         on_progress: Optional[Callable[[int, int], None]] = None) -> dict:
    """Author a LIVE custom face (custom background + live widgets) and stream it.

    Thin wrapper: :func:`starmax_client.dialface.build_dial_face` (offline synthesis of the
    native container — background + ``dial.json`` live-widget render list) →
    :func:`push_dial` (the byte-exact bulk-plane transport). Same result dict as ``push_dial``.

    ``widgets`` follows the decoded ``dial.json`` schema (see :mod:`starmax_client.dialface`):
    e.g. ``[{"type":"hour","widget":"pointer"}, {"type":"date","widget":"text","x":..,...}]``.
    Needs the ``transcode`` extra (Pillow + lz4) for the offline build; the stream itself does not.
    """
    from starmax_client import dialface  # lazy: Pillow + lz4 only needed to BUILD, not to stream
    blob = dialface.build_dial_face(background, widgets, name=name, aod=aod, dial_type=dial_type)
    return await push_dial(client, blob, dial_id=dial_id, chunk=chunk,
                           confirm=confirm, on_progress=on_progress)


# =============================================================================
# CONTROL PLANE — activate an already-installed face  [SCHEMA / INFERRED opcode]
# =============================================================================
def build_dial_activate(dial_id: int = 1, *, color: int = 0, align: int = 0, seq: int = 0) -> bytes:
    """[SCHEMA/INFERRED] Switch the active face to an already-installed dial by id.

    Companion to ``dial-push`` (which auto-activates on install, so this is only needed to
    switch *between* installed faces). Delegates to the single ``0x16``/``Notify.DialInfo``
    builder in :mod:`~.files` — the opcode is uncaptured on this unit, so prefer ``--dry-run``
    and confirm on a fresh capture before trusting on hardware.
    """
    return files.build_dial_switch(dial_id, color=color, align=align, seq=seq)


# =============================================================================
# CONTROL PLANE — DELETE an installed face by filename  [FW]
# =============================================================================
def build_dial_delete(dial_id: Optional[int] = None, *, name: Optional[str] = None,
                      seq: int = 0) -> bytes:
    """[FW] Build the ``0x16`` DELETE frame for a custom dial id OR an explicit on-watch filename.

    ``dial_id`` (a custom id) maps to ``custom_id_<id>.bin`` via :func:`dial_wire_filename`;
    pass ``name`` instead to delete a built-in/market face by its literal filename (as it appears
    in the ``0x16`` list). Delegates to the byte-exact :func:`starmax_client.commands.files.build_dial_delete`.
    """
    if name:
        target = name
    elif dial_id is not None:
        target = dial_wire_filename(dial_id)
    else:
        raise ValueError("build_dial_delete needs a dial_id or an explicit name")
    return files.build_dial_delete(target, seq=seq)


async def delete_dial(client, dial_name: str, *, confirm: bool = True) -> dict:
    """[FW] Send a DELETE for ``dial_name`` over ``0x16``, then (optionally) confirm via a list re-read.

    Returns ``{target, deleted, count, remaining}``. ``deleted`` is ``True`` if the filename is
    absent from the post-delete ``0x16`` list, ``False`` if it's still present, ``None`` if confirm
    is disabled / no reply. Mirrors :func:`push_dial`'s confirm-by-re-reading-the-list discipline
    (the delete opcode is [FW]-derived, so a live list read is the on-hardware proof it took).

    ``client`` needs async ``send_raw(frame)`` and (for ``confirm``) async ``request(frame, opcode)``
    — the real transport satisfies both; tests inject a fake.
    """
    frame = files.build_dial_delete(dial_name)
    await client.send_raw(frame)
    result = {"target": dial_name, "deleted": None, "count": None, "remaining": None}
    if confirm:
        reply = await client.request(files.build_dial_list_request(), files.OP_DIAL_LIST)
        if reply is not None:
            info = files.parse_dial_list_reply(reply.payload)
            names = info.get("filenames") or []
            result["remaining"] = names
            result["count"] = info.get("count")
            result["deleted"] = dial_name not in names
    return result


# =============================================================================
# CLI wiring — register(subparsers, client) + COMMANDS  (auto-discovered)
# =============================================================================
# COMMANDS holds single-frame C1 builders only (the smoke gate builds + validates each as a
# 0xC1 frame). The bulk-plane ``dial-push`` is a multi-frame streaming op, so — exactly like
# files.py's ota-preview/send-file — it is registered below but NOT listed here.
COMMANDS = {
    "dial-activate": build_dial_activate,
}


def _hexdump_push_plan(frames: List[bytes], blob: bytes, dial_id: int) -> None:
    d2n = len(frames) - 3
    print(f"dial-push plan for custom_id_{dial_id}.bin: {len(frames)} frames "
          f"(D3 probe, D1 announce, {d2n} x D2 data, D4 finalize); blob={len(blob)}B")
    print(f"  D3 : {frames[0].hex()}")
    print(f"  D1 : {frames[1].hex()}")
    print(f"  D2#0: {frames[2].hex()[:48]}... ({len(frames[2])} B)")
    print(f"  D4 : {frames[-1].hex()}   (crc16/xmodem of the native blob)")
    print("  install auto-activates on D4 (watchface-track.md §1.3)")


def register(subparsers, client=None) -> None:
    """Add ``dial-activate`` (C1) and ``dial-push`` (bulk). Both support ``--dry-run``.

    ``client`` is None at registration time (the CLI builds the parser via
    ``register_all(sub, client=None)``). For a non-dry-run invocation the CLI's ``_run``
    connects + binds and injects the live client as ``args._client`` — which is why each
    subparser sets a ``_client`` default (so ``hasattr(args, "_client")`` is true) and the
    handlers read ``getattr(args, "_client", None)`` rather than this closure. Mirrors the
    health.py / files.py module pattern.
    """

    async def _activate(args) -> int:
        frame = build_dial_activate(args.dial_id)
        if getattr(args, "dry_run", False):
            print(frame.hex())
            return 0
        conn = getattr(args, "_client", None)
        if conn is None:
            print("no client connected; re-run with --dry-run to preview the frame")
            return 1
        await conn.send_raw(frame)
        return 0

    async def _delete(args) -> int:
        # Resolve the target on-watch filename: an explicit --name (built-in/market face) or a
        # custom dial id -> custom_id_<id>.bin.
        if getattr(args, "name", None):
            target = args.name
        elif args.dial_id is not None:
            try:
                target = dial_wire_filename(args.dial_id)
            except ValueError as e:
                print(f"bad dial id: {e}")
                return 1
        else:
            print("provide a custom dial id (-> custom_id_<id>.bin) or --name <on-watch filename>")
            return 2
        frame = files.build_dial_delete(target)
        conn = getattr(args, "_client", None)
        if getattr(args, "dry_run", False) or conn is None:
            print(f"dial-delete target: {target}  [FW-derived opcode — see delete-opcode-RE.md]")
            print(frame.hex())
            if conn is None and not getattr(args, "dry_run", False):
                print("no client connected; re-run with --dry-run to preview only")
                return 1
            return 0
        result = await delete_dial(conn, target, confirm=not args.no_confirm)
        print(f"sent DELETE for {target}")
        if result["deleted"] is True:
            print(f"delete CONFIRMED — {target} no longer installed "
                  f"({result['count']} faces remain)")
            return 0
        if result["deleted"] is False:
            print(f"delete NOT confirmed — {target} still present ({result['count']} faces)")
            return 2
        return 0  # confirm disabled / no reply

    async def _push(args) -> int:
        path = args.dial_file
        if not os.path.isfile(path):
            print(f"no such file: {path}")
            return 1
        try:
            blob = load_dial_blob(path)
        except (ValueError, dialfmt.DialFormatError) as e:
            print(f"cannot push {path}: {e}")
            return 1
        parsed = dialfmt.parse_blob(blob)
        frames = plan_dial_push(blob, args.dial_id, chunk=args.chunk)
        conn = getattr(args, "_client", None)
        if getattr(args, "dry_run", False) or conn is None:
            print(f"dial '{parsed.name}' ({len(parsed.assets)} assets)")
            _hexdump_push_plan(frames, blob, args.dial_id)
            if conn is None and not getattr(args, "dry_run", False):
                print("no client connected; re-run with --dry-run to preview only")
                return 1
            return 0

        def _prog(done, tot):
            print(f"  … {done}/{tot} bytes ({100 * done // max(tot, 1)}%)")

        result = await push_dial(conn, blob, dial_id=args.dial_id, chunk=args.chunk,
                                 confirm=not args.no_confirm, on_progress=_prog)
        print(f"streamed {result['sent']}/{result['total']} bytes as "
              f"custom_id_{args.dial_id}.bin")
        if result["confirmed"] is True:
            print(f"install CONFIRMED — active dial is now {result['active_dial']!r}")
            return 0
        if result["confirmed"] is False:
            print(f"install NOT confirmed — active dial is {result['active_dial']!r} "
                  f"(expected custom_id_{args.dial_id}.bin)")
            return 2
        return 0  # confirm disabled / no reply

    async def _build(args) -> int:
        src = args.dial_file
        if not os.path.isfile(src):
            print(f"no such file: {src}")
            return 1
        data = open(src, "rb").read()
        try:
            if data[:2] == b"PK":
                from starmax_client import dialtranscode
                blob = dialtranscode.transcode_zip(data)
            else:
                dialfmt.parse_blob(data)  # validate an already-native blob
                blob = data
        except (ValueError, dialfmt.DialFormatError) as e:
            print(f"cannot build: {e}")
            return 1
        parsed = dialfmt.parse_blob(blob)
        with open(args.out, "wb") as fh:
            fh.write(blob)
        print(f"built native container '{parsed.name}' ({len(parsed.assets)} assets, "
              f"{len(blob)} bytes) -> {args.out}")
        print(f"push it with:  dial-push {args.out} --dial-id <id>")
        return 0

    sp = subparsers.add_parser(
        "dial-build",
        help="transcode a dial .bin (ZIP) into the native container (offline; needs transcode extra)")
    sp.add_argument("dial_file", help="distributed dial .bin (ZIP) or an already-native blob")
    sp.add_argument("out", help="output path for the native container")
    sp.set_defaults(func=_build)

    sp = subparsers.add_parser("dial-activate",
                               help="[inferred opcode] switch active face to an installed dial id")
    sp.add_argument("dial_id", type=int, help="installed dial id")
    sp.add_argument("--dry-run", action="store_true", help="print the hex frame, don't send")
    sp.add_argument("--force", action="store_true",
                    help="send even if the wire opcode/payload is unverified (docs/command-audit.md)")
    sp.set_defaults(func=_activate, _client=client)

    sp = subparsers.add_parser(
        "dial-delete",
        help="[FW opcode] DELETE an installed watch face by id (custom_id_<id>.bin) or --name")
    sp.add_argument("dial_id", type=int, nargs="?", default=None,
                    help="custom dial id -> custom_id_<id>.bin (omit and use --name for built-in/market)")
    sp.add_argument("--name", help="explicit on-watch filename to delete (from the 0x16 list)")
    sp.add_argument("--no-confirm", action="store_true",
                    help="skip the post-delete dial-list confirm read")
    sp.add_argument("--dry-run", action="store_true", help="print the hex frame, don't send")
    sp.add_argument("--force", action="store_true",
                    help="send even if the wire opcode/payload is unverified (docs/command-audit.md)")
    sp.set_defaults(func=_delete, _client=client)

    sp = subparsers.add_parser(
        "dial-push", help="install a native dial container and auto-activate it (bulk plane)")
    sp.add_argument("dial_file", help="path to a NATIVE dial blob (see watchface-track.md §2)")
    sp.add_argument("--dial-id", dest="dial_id", type=int, default=25001,
                    help="custom dial id -> custom_id_<id>.bin (default 25001)")
    sp.add_argument("--chunk", type=int, default=CHUNK_MAX,
                    help=f"D2 payload bytes per chunk (default/max {CHUNK_MAX})")
    sp.add_argument("--no-confirm", action="store_true",
                    help="skip the post-push dial-list confirm read")
    sp.add_argument("--dry-run", action="store_true",
                    help="print the transfer plan, don't stream")
    sp.set_defaults(func=_push, _client=client)
