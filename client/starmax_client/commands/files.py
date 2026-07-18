"""B4 — dial / file / firmware(OTA) / workout / GPS / NFC / AGPS command surface.

Two wire planes are used by this group (see docs/firmware-dfu.md §A, docs/protocol-spec.md):

* **Control plane** — ``0xC1`` protobuf frames built via :func:`framing.build_command`.
  Only the **dial/resource list** opcode ``0x16`` is capture-confirmed for this watch
  (docs/protocol-spec.md §3.10; reply vector in tests/fixtures.py). Dial-switch, sport,
  GPS and NFC message *layouts* come from the vendor APK schema (internal RE notes,
  allowed for the standalone client) — but their C1 wire opcodes were **never captured**
  on this unit (protocol-spec §E), so those builders' opcodes are marked ``[INFERRED]``
  and the parsers (which need no opcode) are the reliable part.

* **Bulk plane** — ``0xD1/0xD2/0xD3/0xD4`` raw frames (NOT the C1 envelope). This is the
  generic file-transfer sub-protocol that carries **firmware (`res.ota`)**, **AGPS/EPO
  (`ephemeris.gnss` / `offEphemeris.agnss`)** and **dial/resource install** — all fully
  capture-confirmed and byte-exact-tested against the real image (docs/firmware-dfu.md §B).

Every builder returns ready-to-send ``bytes``; every ``parse_*`` takes a payload/frame.
``[CAP]`` = reproduced against a capture · ``[SCHEMA]`` = APK schema · ``[INFERRED]`` = best
guess pending a fresh capture.

⚠️ Firmware flashing is DEFERRED / recovery-gated (docs/firmware-dfu.md "VERDICT"). The OTA
helper builds and *previews* the transfer plan; it never streams to a device from the CLI.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import struct
import sys
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from starmax_client import framing, otafmt
from starmax_client.protobuf import ProtobufWriter, parse as pb_parse

# =============================================================================
# CRC-16/XMODEM — the bulk-plane transport checksum (docs/firmware-dfu.md §B.1)
# poly 0x1021, init 0x0000, no reflection, xorout 0x0000. Check("123456789")==0x31C3.
# (crc.py ships only CCITT-FALSE/init 0xFFFF; XMODEM is init 0x0000 — kept local here so
#  the shared core is untouched.)
# =============================================================================
def crc16_xmodem(data: bytes) -> int:
    """CRC-16/XMODEM of ``data`` (16-bit int). Verifies the whole-file D4 check."""
    crc = 0x0000
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc


# =============================================================================
# BULK PLANE — generic file transfer (firmware / AGPS / dial install)  [CAP]
# =============================================================================
D1, D2, D3, D4 = 0xD1, 0xD2, 0xD3, 0xD4
D1_TYPE_FLAG = 0x0F          # constant byte after the two sizes in D1 (§B.1)
CHUNK_MAX = 234              # D2 payload cap: 236-B ATT value = d2 + ctr + 234 (§B.1)

# Magic filenames the watch dispatches on (docs/firmware-dfu.md §B; APK libapp.so literals).
FILE_FIRMWARE = "res.ota"
FILE_AGPS_EPHEMERIS = "ephemeris.gnss"
FILE_AGPS_OFFLINE = "offEphemeris.agnss"


def build_d3_query() -> bytes:
    """[CAP] Resume/state probe. A->W ``d3 00``; reply parsed by :func:`parse_d3_reply`."""
    return bytes([D3, 0x00])


def build_d1_announce(name: str, size: int, *, field2: Optional[int] = None) -> bytes:
    """[CAP] Start-of-file: ``d1 00 <u32 size> <u32 field2> 0f <name>\\0`` (§B.1).

    ``field2`` duplicates ``size`` for a from-scratch push (verified for ``res.ota`` and
    ``ephemeris.gnss``); for a dial install docs/protocol-spec.md §9.1 reports it may carry
    a checksum instead — pass it explicitly there. Watch acks ``d1 00 00``.
    """
    f2 = size if field2 is None else field2
    return (bytes([D1, 0x00]) + struct.pack("<II", size, f2)
            + bytes([D1_TYPE_FLAG]) + name.encode("ascii") + b"\x00")


def build_d2_chunk(counter: int, payload: bytes) -> bytes:
    """[CAP] One data chunk: ``d2 <ctr:1> <payload>`` (payload <= 234 B, §B.1)."""
    if len(payload) > CHUNK_MAX:
        raise ValueError(f"D2 payload {len(payload)} > {CHUNK_MAX}")
    return bytes([D2, counter & 0xFF]) + payload


def build_d4_finalize(crc16: int) -> bytes:
    """[CAP] End-of-file: ``d4 00 00 <u32 crc>`` — whole-file CRC-16/XMODEM (§B.1).

    The 16-bit CRC is stored in a little-endian u32 (high half zero): 0xAA0D -> ``0daa0000``.
    """
    return bytes([D4, 0x00, 0x00]) + struct.pack("<I", crc16 & 0xFFFF)


def plan_bulk_transfer(name: str, data: bytes, *, chunk: int = CHUNK_MAX,
                       field2: Optional[int] = None) -> List[bytes]:
    """[CAP] Full A->W frame sequence for a bulk push: ``[D3, D1, D2*, D4]`` (§B.3).

    The D2 counter starts at 0 and wraps at 256; the final D4 carries
    ``crc16_xmodem(data)``. On the wire the transfer is flow-controlled by the watch's
    15-chunk D2-acks — pace the D2 writes to :func:`parse_d2_ack` (§B.3), don't blast.
    """
    frames: List[bytes] = [build_d3_query(), build_d1_announce(name, len(data), field2=field2)]
    for i in range(0, len(data), chunk):
        frames.append(build_d2_chunk((i // chunk) & 0xFF, data[i:i + chunk]))
    frames.append(build_d4_finalize(crc16_xmodem(data)))
    return frames


def plan_ota(image: bytes) -> List[bytes]:
    """[CAP] Firmware update = a bulk push of the whole ``FA EE EB DE`` image as ``res.ota``.

    ⚠️ DEFERRED/recovery-gated (docs/firmware-dfu.md VERDICT): use for *preview* only.
    """
    return plan_bulk_transfer(FILE_FIRMWARE, image)


def plan_agps(ephemeris: bytes, *, offline: bool = False) -> List[bytes]:
    """[CAP] Push GNSS assistance data (``ephemeris.gnss`` / ``offEphemeris.agnss``)."""
    return plan_bulk_transfer(FILE_AGPS_OFFLINE if offline else FILE_AGPS_EPHEMERIS, ephemeris)


def plan_dial_install(filename: str, data: bytes, *, field2: Optional[int] = None) -> List[bytes]:
    """[CAP] Install a watch-face/resource ``.bin`` via the bulk plane; auto-activates (§9.1)."""
    return plan_bulk_transfer(filename, data, field2=field2)


# --- bulk-plane reply parsers (watch->app) -----------------------------------
def _strip_c1(frame: bytes) -> bytes:
    """Bulk replies are raw (no C1 header). Accept either raw or a parsed .payload."""
    return frame


def parse_d3_reply(frame: bytes) -> Dict[str, int]:
    """[CAP] ``d3 00 00 <u32 staged_off> <u32 field2>`` -> resume offset (0 = fresh)."""
    if len(frame) < 11 or frame[0] != D3:
        raise ValueError("not a D3 reply")
    staged, field2 = struct.unpack_from("<II", frame, 3)
    return {"staged_offset": staged, "field2": field2}


def parse_d2_ack(frame: bytes) -> Dict[str, int]:
    """[CAP] ``d2 00 00 <u32 offset> <u32 crc>`` -> windowed progress (§B.1).

    ``crc`` is the running CRC-16/XMODEM of ``image[0:offset]`` (verify against the stream).
    """
    if len(frame) < 11 or frame[0] != D2:
        raise ValueError("not a D2 ack")
    offset, crc = struct.unpack_from("<II", frame, 3)
    return {"offset": offset, "crc": crc & 0xFFFF}


def parse_d1_ack(frame: bytes) -> bool:
    """[CAP] Announce accepted iff ``d1 00 00``."""
    return len(frame) >= 3 and frame[0] == D1 and frame[1] == 0 and frame[2] == 0


def parse_d4_ack(frame: bytes) -> bool:
    """[CAP] Whole-file verify OK iff ``d4 00 00`` (watch then applies + reboots, §B.2)."""
    return len(frame) >= 3 and frame[0] == D4 and frame[1] == 0 and frame[2] == 0


# =============================================================================
# FIRMWARE FLASH (res.ota) — DEFERRED / recovery-gated  (issue #29; docs/firmware-dfu.md VERDICT)
# =============================================================================
# ⚠️ HARD SAFETY. A firmware flash CAN brick the watch (no BLE recovery, no A/B slot). By default
# this path is 100% OFFLINE (parse + validate the image + print the frame plan, no radio). A real
# transmit requires --force-flash AND a CRC-valid image (image.valid) AND one of:
#
#   • RECOVERY_PROVEN (issue #17) — a wired unbrick path PROVEN. FALSE here, so every CUSTOM image
#     stays refused until #17 lands. Do NOT flip this to unblock one image — it unblocks ALL.
#   • the image's sha256 is on FLASH_ALLOWLIST — a NARROW carve-out for verified guaranteed-boot
#     images (stock v1.0.3 + the benign CFWPOC) safe to flash a SPARE with no teardown
#     (recovery verdict, 2026-07-13).
#
# So an allowlisted image may flash pre-#17; every other image needs #17. The stock round-trip is
# COMPAT-CHECKED AT FLASH — the watch's os_ota accepts (same/compatible) or gracefully rejects a
# downgrade (image never commits; NOT a brick) — so "byte-identical" is never assumed same-version.
RECOVERY_PROVEN = False
RECOVERY_GATE_MSG = "only after wired recovery (#17) is proven"

# Narrow verified-safe allowlist: a --force-flash of an image whose sha256 is here is permitted on
# a SPARE before #17 (these exact images are verified guaranteed-boot). Everything NOT here stays
# refused behind RECOVERY_PROVEN=False. Both hashes independently confirmed during firmware RE
# (2026-07-13); update via a one-line constant. This does NOT weaken the default-deny for custom images.
#
# GOVERNANCE — every image here requires the same per-image review: a pure-DATA-only change +
# ``version_flags@0x10`` UNTOUCHED (so os_ota's version-compat decision is identical to stock) +
# both CRC-32s valid + it is guaranteed-boot (byte-identical to a known-booting image apart from the
# data change). NEVER allowlist a non-guaranteed-boot image — that is exactly what RECOVERY_PROVEN /
# #17 gates. Add only via a reviewed one-line constant.
STOCK_V103_SHA256 = "5dac413b0e8e68581d5de1d6916f022727ef9a96bacc4003e1751f86c2967cc0"
CFWPOC_SHA256 = "9865fe25473c739e6ab0d58f2a0e58bd0aa819d10cf363a4a39aa98db2ab0c48"
# CFW2: stock v1.0.3 with device name GTX2->CFW2 at all 3 inline-string sites (.ota 0x501C/0x192FC/
# 0x124AD0). Diff vs stock = 17 bytes (9 pure-DATA name bytes + 2 recomputed CRC-32s); magic@0x2C +
# version_flags@0x10 untouched; both CRC gates green; dry-run VALID with stock-identical D1 announce.
# Guaranteed-boot (same class as CFWPOC). Reviewed 2026-07-15.
CFW2_SHA256 = "e9994fc442533e287ba4f48cee4156deb5fbfe5d653d70b50f05afe6158b8545"
FLASH_ALLOWLIST = {
    STOCK_V103_SHA256: "stock v1.0.3 (cb05_yhzn01_v1.0.3_20241218_02.ota)",
    CFWPOC_SHA256: "CFWPOC (benign guaranteed-boot repack of stock)",
    CFW2_SHA256: "CFW2 (stock v1.0.3 + device name GTX2->CFW2, guaranteed-boot)",
}


def allowlisted(image: bytes) -> Optional[str]:
    """Return the allowlist label if ``image``'s sha256 is a verified-safe hash, else ``None``."""
    return FLASH_ALLOWLIST.get(hashlib.sha256(image).hexdigest())


async def flash_firmware(client, image: bytes, *, chunk: int = CHUNK_MAX,
                         on_progress: Optional[Callable[[int, int], None]] = None,
                         ack_timeout: float = 10.0) -> Dict[str, object]:
    """[CAP] Stream an OTA ``image`` to the watch as ``res.ota`` over the bulk plane.

    Same live-proven transport as the dial push (``D3 -> D1 -> D2* -> D4``): raw D-plane frames go
    out **write-WITH-response** so the ATT link-layer acks pace the stream to what the watch can
    absorb. (Write-without-response fire-hoses the chunks and overruns the watch — the tail is
    dropped and the D4 whole-file CRC-16 then rejects the truncated image; proven on hardware with
    the 231 KB dial push stalling at ~84 KB.) Raw D-plane frames must each fit one ATT PDU, so we
    assert the negotiated payload is large enough before sending anything.

    Validates the container (both CRC-32 gates) BEFORE touching the radio and refuses a CRC-invalid
    image — defence in depth even though the CLI handler already gated. ``client`` needs async
    ``send_raw(frame, response=...)`` (the real transport; tests inject a fake).

    The watch windows the transfer with a D2 ack every 15 chunks (``d2 00 00 <cum_off> <running
    CRC-16/XMODEM>``, docs/firmware-dfu.md §29). We do NOT consume those acks for flow control —
    the transport's inbound path runs every notification through the C1 Reassembler, which drops
    raw D-plane frames — so write-WITH-response ATT pacing is what keeps the watch from overrunning
    (proven on the dial push). The running-CRC checkpoints are asserted offline in the
    captured-session self-test (tests/test_firmware_flash.py) instead.

    Returns ``{sent, total, applied, d4_ack}``. We read the raw D4 finalize ack via a transport
    raw-PDU tap (the same mechanism the probe uses; the C1 Reassembler drops raw D-plane frames):
    ``applied`` is ``True`` if the watch acked ``d4 00 00`` (accepted -> it then stages -> verifies
    -> commits -> reboots ~28 s, no activate frame; confirm by reconnecting + re-reading the bind
    descriptor C1 0x01), ``False`` if it replied with a different D4 frame (most likely a
    version-compat REJECT — EXPECTED + SAFE, the image is not committed, not a brick), or ``None``
    if no D4 ack was seen within ``ack_timeout`` (or the transport exposes no raw tap). ``d4_ack``
    is the raw reply hex, or ``None``.
    """
    img = otafmt.parse_ota_image(image)  # OtaFormatError if it isn't a FA-EE-EB-DE container
    if not img.valid:
        raise ValueError("refusing to stream a CRC-invalid OTA image: " + "; ".join(img.problems()))

    frames = plan_bulk_transfer(FILE_FIRMWARE, image, chunk=chunk)
    d3, d1, *d2s, d4 = frames
    mtu_payload = getattr(client, "_mtu_payload", 244)
    biggest = max(len(f) for f in frames)
    if biggest > mtu_payload:
        raise RuntimeError(
            f"link payload {mtu_payload}B < largest D-plane frame {biggest}B — a raw D-plane frame "
            f"would be C1-fragmented and corrupt the stream. Reduce --chunk or raise MTU.")

    # Raw tap to observe the D4 finalize ack (accept vs version-reject) — the C1 Reassembler drops
    # it. Best-effort: a minimal fake client without a raw tap simply yields applied=None.
    loop = asyncio.get_running_loop()
    d4_fut = loop.create_future()

    def _set(pdu: bytes) -> None:
        if not d4_fut.done():
            d4_fut.set_result(pdu)

    def _tap(pdu: bytes) -> None:
        if pdu[:1] == bytes([D4]):
            loop.call_soon_threadsafe(_set, bytes(pdu))

    tap_ok = hasattr(client, "add_raw_listener")
    if tap_ok:
        client.add_raw_listener(_tap)
    total = len(image)
    sent = 0
    try:
        await client.send_raw(d3, response=True)
        await client.send_raw(d1, response=True)
        for i, d2 in enumerate(d2s):
            await client.send_raw(d2, response=True)
            sent += len(d2) - 2  # minus the 'd2 <ctr>' header
            if on_progress and (i % 16 == 0 or i == len(d2s) - 1):
                on_progress(min(sent, total), total)
        await client.send_raw(d4, response=True)
        ack = None
        if tap_ok:
            try:
                ack = await asyncio.wait_for(d4_fut, timeout=ack_timeout)
            except asyncio.TimeoutError:
                ack = None
    finally:
        if tap_ok:
            client.remove_raw_listener(_tap)

    applied = parse_d4_ack(ack) if ack is not None else None
    return {"sent": min(sent, total), "total": total, "applied": applied,
            "d4_ack": ack.hex() if ack is not None else None}


async def probe_firmware(client, *, timeout: float = 5.0) -> Dict[str, object]:
    """READ-ONLY on-watch OTA probe: send ONLY the D3 resume/state query and read the reply.

    This path is **physically incapable of transmitting firmware** — it builds and sends the 2-byte
    D3 query and NOTHING else (no D1 announce, no D2 chunks, no D4 — zero image bytes leave the
    host). D3 is a **non-arming** state/resume query, capture-confirmed on the fw-complete session:
    it was sent once, standalone (`d3 00` -> `d3 00 00 <staged_off> <field2>`), and the AGPS
    transfers in the same capture started on D1 with **no D3 at all** — so D1 (announce), not D3,
    is what arms a transfer. Hence this probe is exempt from ``RECOVERY_PROVEN`` (#17): no write to
    a firmware partition occurs.

    The D3 reply is a raw D-plane PDU (``d3 00 00 <u32 staged_off> <u32 field2>``) which the C1
    Reassembler drops, so we read it via a transport raw-PDU tap (:meth:`StarmaxClient.add_raw_
    listener`). Returns ``{reply, state}`` where ``state`` is :func:`parse_d3_reply`'s dict (or
    ``None`` on timeout / an unparseable reply).
    """
    loop = asyncio.get_running_loop()
    fut: "asyncio.Future" = loop.create_future()

    def _set(pdu: bytes) -> None:
        if not fut.done():
            fut.set_result(pdu)

    def _tap(pdu: bytes) -> None:
        if pdu[:1] == bytes([D3]):
            loop.call_soon_threadsafe(_set, bytes(pdu))

    client.add_raw_listener(_tap)
    try:
        await client.send_raw(build_d3_query(), response=True)   # the ONLY frame sent — read-only
        try:
            reply = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            reply = None
    finally:
        client.remove_raw_listener(_tap)

    state = None
    if reply is not None:
        try:
            state = parse_d3_reply(reply)
        except ValueError:
            state = None
    return {"reply": reply, "state": state}


def _print_ota_report(img: otafmt.OtaImage, path: str) -> None:
    """Print the parse + integrity verdict for an OTA image (the dry-run diagnostic)."""
    tag = lambda ok: "OK" if ok else "BAD"  # noqa: E731
    print(f"OTA image: {path}  ({img.filesize} bytes)")
    print(f"  magic            : {'FA EE EB DE  OK' if img.ok_magic else 'BAD'}")
    print(f"  inner section    : name={img.inner_name!r}  data_len={img.inner_data_len}  "
          f"version=0x{img.version_flags:08X}")
    print(f"  outer crc32@0x04 : stored 0x{img.outer_crc_stored:08X}  "
          f"computed 0x{img.outer_crc_computed:08X}  [{tag(img.outer_crc_ok)}]")
    print(f"  inner crc32@0x38 : stored 0x{img.inner_crc_stored:08X}  "
          f"computed 0x{img.inner_crc_computed:08X}  [{tag(img.inner_crc_ok)}]")
    print(f"  sect magic @0x2C : 0x{img.section_magic:08X}  "
          f"[fixed Actions magic (never recomputed), {tag(img.section_magic_ok)}]")
    for w in img.warnings():
        print(f"  warn             : {w}")
    verdict = "VALID" if img.valid else ("INVALID — " + "; ".join(img.problems()))
    print(f"  integrity        : {verdict}")


def _print_flash_plan(frames: List[bytes], image: bytes, chunk: int) -> None:
    """Print the exact bulk-transfer frame plan (offsets / chunk count / finalize CRC)."""
    d2n = len(frames) - 3
    last_d2 = len(frames[-2]) - 2  # last chunk payload size (frame minus 'd2 <ctr>')
    last_off = (d2n - 1) * chunk if d2n else 0
    print(f"flash plan (res.ota via bulk plane D3->D1->D2*->D4): {len(frames)} frames, "
          f"{len(image)} bytes")
    print(f"  D3 probe    : {frames[0].hex()}")
    print(f"  D1 announce : {frames[1].hex()}")
    print(f"  D2 data     : {d2n} chunks x up to {chunk} B  "
          f"(chunk#0 @offset 0, chunk#{max(d2n - 1, 0)} @offset {last_off}, last chunk {last_d2} B)")
    print(f"  D4 finalize : {frames[-1].hex()}   (crc16/xmodem of the whole image)")


# =============================================================================
# CONTROL PLANE — dial / resource list, opcode 0x16  [CAP]
# =============================================================================
OP_DIAL_LIST = 0x16          # [CAP] watch-face/resource list (docs/protocol-spec.md §3.10)


def build_dial_list_request(seq: int = 0) -> bytes:
    """[CAP] Read the installed dial/resource list (§3.10 step 10: ``f1=0``). flag=0."""
    payload = ProtobufWriter().varint(1, 0).to_bytes()
    return framing.build_command(OP_DIAL_LIST, payload, flag=0, seq=seq)


def _ascii(v) -> Optional[str]:
    return v.decode("ascii", "replace") if isinstance(v, (bytes, bytearray)) else None


def parse_dial_list_reply(payload: bytes) -> Dict[str, object]:
    """[CAP] Decode the 0x16 dial/resource-inventory reply (§3.10).

    Repeated field 10 = one installed resource: ``{1:group, 2:slot, 3:size, 4:filename}``.
    Top level also carries **active dial** (f14 = filename) and **storage** (f11 total,
    f12 used, bytes) — the same installed/active/storage triple the GB coordinator surfaces
    from 0x16 (issue #10). Verified against tests/fixtures.py DIAL_LIST (byte-exact reassembly
    of the captured C1+C3 reply; filename set published in docs/protocol-spec.md §3.10).
    """
    entries: List[Dict[str, object]] = []
    top: Dict[int, object] = {}
    for field, wire, val in pb_parse(payload):
        if field == 10 and wire == 2:  # repeated entry submessage
            sub = {f: v for f, _w, v in pb_parse(val)}
            entries.append({
                "group": sub.get(1),
                "slot": sub.get(2),
                "size": sub.get(3),
                "filename": _ascii(sub.get(4)),
            })
        else:
            top[field] = val  # last-wins for scalar header fields
    max_dials = top.get(18) if isinstance(top.get(18), int) else None  # f18 all_plate_support_max
    count = len(entries)
    # [FW] per-category plate counts (protocol_watch_dial_plate_inquire_reply field descriptors,
    # delete-opcode-RE.md). We surface them RAW under their firmware field names WITHOUT interpreting
    # which one caps "custom faces" — the install barrier may be the GLOBAL max (f18) OR a per-type
    # cap here. `measure, don't assume`: read these live to see which ceiling a watch actually hits.
    _iv = lambda f: top.get(f) if isinstance(top.get(f), int) else None  # noqa: E731
    counts = {
        "cloud_plate_num": _iv(4),
        "user_cloud_plate_num": _iv(5),
        "photo_plate_num": _iv(6),
        "user_photo_plate_num": _iv(7),
        "wallpaper_plate_num": _iv(8),
        "user_wallpaper_plate_num": _iv(9),
        "plate_photo_pic_support_num": _iv(17),
        "all_plate_support_max": max_dials,
    }
    return {
        "count": count,
        "filenames": [e["filename"] for e in entries if e["filename"]],
        "entries": entries,
        "active_dial": _ascii(top.get(14)),                     # f14 = current face (§3.10)
        "storage_total": top.get(11) if isinstance(top.get(11), int) else None,
        "storage_used": top.get(12) if isinstance(top.get(12), int) else None,
        # [FW] f18 = all_plate_support_max — the GLOBAL max plates the watch stores. `slots_free`
        # answers "is the list full?" against the GLOBAL cap; a per-category cap (see `counts`) can
        # bite first, so both are surfaced for the install-barrier test.
        "max_dials": max_dials,
        "slots_free": (max_dials - count) if isinstance(max_dials, int) else None,
        "counts": counts,
    }


# =============================================================================
# CONTROL PLANE — dial switch (APK schema Notify.DialInfo)  [SCHEMA / INFERRED opcode]
# =============================================================================
# Wire opcode NOT captured on this unit (protocol-spec §E). Schema: Notify.DialInfo
# {1:isSelected, 2:dialId, 3:dialColor, 4:align}; iOS-SDK analogue SET_Dial_Current=0xED
# (different framing). Dial id ranges: 1-5000 built-in / 5001-25000 custom / 25001+ market.
OP_DIAL_SET = 0x16  # [INFERRED] reuse the dial opcode with a set-shaped payload; confirm via pcap.


def build_dial_switch(dial_id: int, *, color: int = 0, align: int = 0, seq: int = 0) -> bytes:
    """[SCHEMA/INFERRED] Switch the active watch face by id (Notify.DialInfo).

    Structurally faithful to the schema; the C1 opcode is a best guess (see OP_DIAL_SET) —
    prefer ``--dry-run`` and confirm on a fresh capture before trusting on hardware.
    """
    info = (ProtobufWriter().varint(1, 1).varint(2, dial_id)
            .varint(3, color).varint(4, align).to_bytes())
    payload = ProtobufWriter().varint(1, 2).message(2, info).to_bytes()
    return framing.build_command(OP_DIAL_SET, payload, flag=0, seq=seq)


# =============================================================================
# CONTROL PLANE — dial DELETE (firmware `protocol_watch_dial_plate_operate`)  [FW]
# =============================================================================
# The GTX2 dial control plane is ONE protobuf message on opcode 0x16 whose field 1 selects the
# operation. Byte-exact from the watch's own firmware (protobuf-c enum/field tables; internal RE notes):
#
#   protocol_watch_dial_plate_operate {
#     1: operate    (enum DialOperateType)   0=INQUIRE  1=SET  2=DELETE
#     2: dial_name  (repeated bytes)          on-watch filename(s), e.g. "custom_id_25022.bin"
#   }
#
# INQUIRE(0) is [CAP] (the list request is literally 0x16 payload "08 00"); SET/DELETE and the
# dial_name field are [FW] (the vendor app never sends a delete, so it was never captured — but
# this is the device's own code). The watch deletes BY FILENAME: firmware logs
# `plate_management_delete_name:file_name=%s`. Prefer --dry-run; the CLI/node confirm by
# re-reading the 0x16 list and asserting the filename is gone.
DIAL_OP_INQUIRE = 0   # [CAP]
DIAL_OP_SET = 1       # [FW]
DIAL_OP_DELETE = 2    # [FW]


def build_dial_delete(dial_name: str, *, seq: int = 0) -> bytes:
    """[FW] Delete an installed watch face by its on-watch filename (``custom_id_<id>.bin``).

    Emits the ``0x16`` operate frame ``{f1=DELETE(2), f2=dial_name}``. ``dial_name`` is the exact
    on-watch filename as it appears in the ``0x16`` list reply (``parse_dial_list_reply`` →
    ``filenames``) — for a custom face that is ``custom_id_<id>.bin`` (see
    :func:`starmax_client.commands.dials.dial_wire_filename`); built-in/market faces carry their
    own names (e.g. ``YHZN_1021@LC.bin``).

    DESTRUCTIVE + [FW]-derived (not yet live-captured): preview with ``--dry-run`` and confirm on
    hardware by re-reading the list. Delete of a currently-active face is left to the watch's
    fallback (firmware backs up a default watchface).
    """
    if not dial_name:
        raise ValueError("dial_name must be a non-empty on-watch filename")
    payload = (ProtobufWriter().varint(1, DIAL_OP_DELETE)
               .bytes(2, dial_name.encode("ascii")).to_bytes())
    return framing.build_command(OP_DIAL_LIST, payload, flag=0, seq=seq)


def parse_dial_info(payload: bytes) -> Dict[str, object]:
    """[SCHEMA] Decode Notify.DialInfoData {1:status, 2:repeated DialInfo}."""
    out: Dict[str, object] = {"infos": []}
    for field, wire, val in pb_parse(payload):
        if field == 1 and wire == 0:
            out["status"] = val
        elif field == 2 and wire == 2:
            d = {f: v for f, _w, v in pb_parse(val)}
            out["infos"].append({"selected": d.get(1), "dial_id": d.get(2),
                                 "color": d.get(3), "align": d.get(4)})
    return out


# =============================================================================
# CONTROL PLANE — workout / sport + GPS track  (APK schema)  [SCHEMA / INFERRED opcode]
# =============================================================================
# protocol-spec §E: no sport/workout/GPS frame was ever observed on this watch (C1, bulk,
# or 0x0e history). Layouts are from the schema; opcodes are placeholders. Parsers are safe
# (decode a given payload); request builders carry [INFERRED] opcodes.
# DECOMPILE UPDATE (internal opcode-resolution RE): the GTX2's sport control lives in the Dart
# creek_blue_manage plugin (proto/sport.pb.dart), opcode Dart-AOT-only. Watch->app workout
# start/stop MIGHT be a 0x10 control push (f1=3 "routine status"), but that overlaps the
# records-available ping and was never cleanly captured — still UNRESOLVED, needs a capture.
OP_SPORT = 0x1A       # [INFERRED] no capture; placeholder
OP_GPS = 0x1B         # [INFERRED] no capture; placeholder

SPORT_STATUS_START, SPORT_STATUS_PAUSE, SPORT_STATUS_RESUME, SPORT_STATUS_STOP = 1, 2, 3, 0


def build_sport_control(sport_type: int, status: int, *, seq: int = 0) -> bytes:
    """[SCHEMA/INFERRED] Start/pause/resume/stop a sport session (Notify.SportSyncData
    {1:sportType, 2:sportStatus, ...}). Opcode INFERRED — dry-run only."""
    payload = (ProtobufWriter().varint(1, sport_type).varint(2, status).to_bytes())
    return framing.build_command(OP_SPORT, payload, flag=0, seq=seq)


def parse_sport_sync(payload: bytes) -> Dict[str, object]:
    """[SCHEMA] Notify.SportSyncData: {1:sportType, 2:sportStatus, ... 11:sportSeconds}."""
    d = {f: v for f, _w, v in pb_parse(payload)}
    return {"sport_type": d.get(1), "sport_status": d.get(2), "seconds": d.get(11), "raw": d}


def parse_sport_history(payload: bytes) -> Dict[str, object]:
    """[SCHEMA] Notify.SportHistory header: {2:sportLength, 3:currentSportId,
    4:currentSportDataLength, 13:sportSeconds, ...}."""
    d = {f: v for f, _w, v in pb_parse(payload)}
    return {"sport_length": d.get(2), "current_id": d.get(3),
            "data_length": d.get(4), "seconds": d.get(13), "raw": d}


def parse_gps_sync(payload: bytes) -> Dict[str, object]:
    """[SCHEMA] Notify.GpsSyncContent: {1:status, 2:repeated GpsSyncEle
    (interval,height,lng,lat), 3:hasNext, 4:notValid}. lat/lng are scaled ints."""
    out: Dict[str, object] = {"points": []}
    for field, wire, val in pb_parse(payload):
        if field == 1 and wire == 0:
            out["status"] = val
        elif field == 2 and wire == 2:
            e = {f: v for f, _w, v in pb_parse(val)}
            out["points"].append({"interval": e.get(1), "height": e.get(2),
                                  "lng": e.get(3), "lat": e.get(4)})
        elif field == 3 and wire == 0:
            out["has_next"] = bool(val)
        elif field == 4 and wire == 0:
            out["not_valid"] = bool(val)
    return out


# =============================================================================
# CONTROL PLANE — NFC (APK schema Notify.Nfc*)  [SCHEMA / INFERRED opcode]
# =============================================================================
OP_NFC = 0x1C  # [INFERRED] no capture; placeholder


def build_nfc_list_request(*, card_type: int = 0, seq: int = 0) -> bytes:
    """[SCHEMA/INFERRED] Request the NFC card list (Notify.NfcCardStatus {1:type,2:status}).
    Opcode INFERRED — dry-run only."""
    payload = ProtobufWriter().varint(1, card_type).varint(2, 0).to_bytes()
    return framing.build_command(OP_NFC, payload, flag=0, seq=seq)


def parse_nfc_card_info(payload: bytes) -> Dict[str, object]:
    """[SCHEMA] Notify.NfcCardInfo {1:status, 2:type, 3:repeated NfcCard(cardType,cardName)}."""
    out: Dict[str, object] = {"cards": []}
    for field, wire, val in pb_parse(payload):
        if field == 1 and wire == 0:
            out["status"] = val
        elif field == 2 and wire == 0:
            out["type"] = val
        elif field == 3 and wire == 2:
            c = {f: v for f, _w, v in pb_parse(val)}
            nm = c.get(2, b"")
            out["cards"].append({
                "card_type": c.get(1),
                "card_name": nm.decode("utf-8", "replace") if isinstance(nm, (bytes, bytearray)) else nm,
            })
    return out


# =============================================================================
# CLI wiring — register(subparsers, client) + COMMANDS   (B5 auto-discovers register)
# =============================================================================
# name -> frame builder (single-frame commands only; bulk transfers are multi-frame plans).
COMMANDS: Dict[str, object] = {
    "dial-list": build_dial_list_request,
    "dial-switch": build_dial_switch,
    "sport-control": build_sport_control,
    "nfc-list": build_nfc_list_request,
}


def _hexdump_plan(frames: List[bytes]) -> None:
    print(f"bulk transfer plan: {len(frames)} frames "
          f"(D3 query, D1 announce, {len(frames) - 3} x D2 data, D4 finalize)")
    print(f"  D3 : {frames[0].hex()}")
    print(f"  D1 : {frames[1].hex()}")
    print(f"  D2#0: {frames[2].hex()[:48]}... ({len(frames[2])} B)")
    print(f"  D4 : {frames[-1].hex()}   (crc16/xmodem of the file)")


def _print_dial_list(info: Dict[str, object]) -> None:
    """Pretty-print the parsed 0x16 inventory: count/capacity, active face, storage, filenames.

    This is the authoritative full-list read (the transport reassembles the entire multi-fragment
    reply — the esphome node log truncates it, which has misled manual decodes). `[FULL]` fires
    when the slot count is at `all_plate_support_max` — the install-barrier signal.
    """
    count = info.get("count")
    md = info.get("max_dials")
    sf = info.get("slots_free")
    cap = f"{count}/{md}" if isinstance(md, int) else str(count)
    tail = f" ({sf} free)" if isinstance(sf, int) else ""
    full = "  [FULL]" if (isinstance(sf, int) and sf <= 0) else ""
    print(f"installed dials : {cap}{tail}{full}")
    print(f"active face     : {info.get('active_dial')}")
    st, su = info.get("storage_total"), info.get("storage_used")
    if isinstance(st, int) and isinstance(su, int):
        print(f"storage (bytes) : {su} / {st}")
    # Per-category counts (RAW firmware field names) — the install barrier may be one of THESE, not
    # the global max. Print any that are populated so the operator can see which ceiling is hit.
    cts = info.get("counts") or {}
    shown = {k: v for k, v in cts.items() if isinstance(v, int)}
    if shown:
        print("per-category    : " + "  ".join(f"{k}={v}" for k, v in shown.items()))
    for e in info.get("entries", []) or []:
        print(f"  - {e.get('filename')}  ({e.get('size')} B)")


def register(subparsers, client=None) -> None:
    """Add this group's subcommands. Every one supports ``--dry-run`` (print hex, no send).

    Bulk/OTA transfers are exposed as *preview-only* plan printers — flashing is deferred and
    recovery-gated (docs/firmware-dfu.md); the CLI never streams an image to a device.
    """
    async def _handler(args) -> int:
        preview = getattr(args, "_files_preview", None)
        if preview:
            path = getattr(args, "image", None) or getattr(args, "path", None)
            if not path or not os.path.isfile(path):
                print(f"no such file: {path}")
                return 1
            data = open(path, "rb").read()
            if preview == "ota":
                _hexdump_plan(plan_ota(data))
            else:
                name = getattr(args, "name", None) or os.path.basename(path)
                _hexdump_plan(plan_bulk_transfer(name, data))
            print("(preview only — flashing is deferred/recovery-gated; nothing sent)")
            return 0
        builder = args._files_builder
        kwargs = {"dial_id": args.dial_id} if builder is build_dial_switch else {}
        frame = builder(**kwargs)
        if getattr(args, "dry_run", False) or client is None:
            print(frame.hex())
            return 0
        await client.send_raw(frame)
        return 0

    async def _dial_list(args) -> int:
        """``dial-list`` — live READ: request 0x16, reassemble the full reply, print the inventory
        (count/capacity/active/storage/filenames). --dry-run prints the request frame hex only.

        This is the decisive capacity read: the whole multi-fragment 0x16 reply is reassembled by
        the transport (the esphome node's diag log truncates it), so count vs `all_plate_support_max`
        (`max_dials`/`slots_free`) is authoritative here — is the dial list FULL?
        """
        frame = build_dial_list_request()
        conn = getattr(args, "_client", None)
        if getattr(args, "dry_run", False) or conn is None:
            print(frame.hex())
            if conn is None and not getattr(args, "dry_run", False):
                print("no client connected; re-run with --dry-run to preview only")
                return 1
            return 0
        reply = await conn.request(frame, OP_DIAL_LIST, timeout=5.0)
        if reply is None:
            print("(no 0x16 reply)")
            return 2
        _print_dial_list(parse_dial_list_reply(reply.payload))
        return 0

    async def _flash(args) -> int:
        """``flash-firmware`` — DRY-RUN by default; a real flash needs --force-flash AND #17."""
        path = args.image
        if not os.path.isfile(path):
            print(f"no such file: {path}")
            return 1
        image = open(path, "rb").read()
        try:
            img = otafmt.parse_ota_image(image)
        except otafmt.OtaFormatError as e:
            print(f"not a flashable OTA image: {e}")
            return 1
        _print_ota_report(img, path)
        frames = plan_bulk_transfer(FILE_FIRMWARE, image, chunk=args.chunk)
        _print_flash_plan(frames, image, args.chunk)

        # READ-ONLY on-watch probe: offline dry-run above, then a D3-only state query. Separate
        # code path — it never references D1/D2/D4/flash_firmware, so it cannot transmit firmware.
        if getattr(args, "probe", False):
            if getattr(args, "force_flash", False):
                print("--probe is read-only and cannot be combined with --force-flash.",
                      file=sys.stderr)
                return 2
            print("[probe] read-only — sending ONLY the D3 resume/state query; NO firmware bytes "
                  "(D1/D2/D4) will be transmitted.")
            print("[probe] the watch is SINGLE-OWNER — disconnect it from the phone (Gadgetbridge) "
                  "first, or the CLI cannot connect.")
            conn = getattr(args, "_client", None)
            self_conn = conn is None
            if self_conn:  # lazy import avoids a cli<->commands import cycle
                from starmax_client.cli import _connect_and_bind, _resolve_address
                address = await _resolve_address(args)
                if not address:
                    return 1
                conn = await _connect_and_bind(address)
            try:
                result = await probe_firmware(conn)
            finally:
                if self_conn and conn is not None:
                    await conn.disconnect()
            st = result["state"]
            if st is None:
                print("[probe] no D3 reply seen (timeout) — watch may be busy / not OTA-ready. "
                      "Nothing was written.", file=sys.stderr)
                return 2
            fresh = "fresh — no partial transfer staged" if st["staged_offset"] == 0 else \
                    f"RESUME point at offset {st['staged_offset']}"
            print(f"[probe] watch OTA state: staged_offset={st['staged_offset']} ({fresh}), "
                  f"field2={st['field2']}  [raw {result['reply'].hex()}]")
            print("[probe] read-only complete — nothing was written to the watch.")
            return 0

        if not getattr(args, "force_flash", False):
            print(f"(DRY-RUN — nothing transmitted. A real flash needs --force-flash, and "
                  f"{RECOVERY_GATE_MSG}.)")
            return 0

        # --force-flash: real-flash INTENT. CRC gate, then EITHER the verified-safe allowlist OR
        # RECOVERY_PROVEN (#17). RECOVERY_PROVEN stays False, so every CUSTOM image still needs #17.
        if not img.valid:
            print(f"REFUSING to flash: image is CRC-invalid — {'; '.join(img.problems())}",
                  file=sys.stderr)
            return 2
        allow_name = allowlisted(image)
        digest = hashlib.sha256(image).hexdigest()
        if allow_name is None and not RECOVERY_PROVEN:
            print(f"REFUSING to flash: wired recovery (#17) is NOT proven AND this image is not on "
                  f"the verified-safe allowlist (sha256={digest}). A custom image with no unbrick "
                  f"path can hard-brick the watch (no BLE recovery, no A/B slot). Only the stock "
                  f"v1.0.3 image and the benign CFWPOC may flash a spare pre-#17; everything else "
                  f"stays gated {RECOVERY_GATE_MSG}.", file=sys.stderr)
            return 3
        if allow_name is not None:
            print(f"[allowlist] image sha256 matches '{allow_name}' (verified guaranteed-boot) — "
                  f"permitted before #17. RECOVERY_PROVEN stays False for every other image.")
            print("⚠️  POINT THIS AT A DESIGNATED SPARE ONLY. The allowlist authorizes the IMAGE, "
                  "not the target — the CLI cannot tell a spare from your daily driver. Flashing "
                  "the daily driver is operator error this gate cannot catch.")
        else:
            print("[recovery] #17 proven (RECOVERY_PROVEN=True) — full flash permitted.")
        print("[flash] NOTE: compatibility is checked AT FLASH. The unit most likely already runs "
              "this v1.0.3, so this is probably a SAME-version reflash (accept + apply); if it is "
              "instead a downgrade the watch gracefully rejects it (image not committed). Either "
              "outcome is SAFE — not a brick. 'Byte-identical' is not assumed to be same-version.")
        conn = getattr(args, "_client", None)
        self_conn = conn is None
        if self_conn:  # lazy import avoids a cli<->commands import cycle
            from starmax_client.cli import _connect_and_bind, _resolve_address
            address = await _resolve_address(args)
            if not address:
                return 1
            conn = await _connect_and_bind(address)
        try:
            def _prog(done, tot):
                print(f"  … {done}/{tot} bytes ({100 * done // max(tot, 1)}%)")
            result = await flash_firmware(conn, image, chunk=args.chunk, on_progress=_prog)
        finally:
            if self_conn and conn is not None:
                await conn.disconnect()
        base = f"streamed {result['sent']}/{result['total']} bytes as {FILE_FIRMWARE}"
        if result["applied"] is True:
            print(f"{base}. Watch ACCEPTED (D4 ack {result['d4_ack']}) — it now stages -> verifies "
                  f"-> commits -> reboots (~28 s), no activate frame; reconnect + re-read the bind "
                  f"descriptor (C1 0x01) to confirm the new version.")
        elif result["applied"] is False:
            print(f"{base}, but the watch did NOT ack D4 as accepted (reply {result['d4_ack']}). "
                  f"Most likely a VERSION-COMPAT REJECT (e.g. v1.0.3 is a downgrade) — EXPECTED + "
                  f"SAFE: the image is not committed, NOT a brick.")
        else:
            print(f"{base}. No D4 ack observed — the watch verifies the D4 CRC then applies + "
                  f"reboots (~28 s); a version-compat reject would also land here (safe, "
                  f"uncommitted). Reconnect + re-read C1 0x01 to confirm.")
        return 0

    def _add(name, help_):
        sp = subparsers.add_parser(name, help=help_)
        sp.add_argument("--dry-run", action="store_true",
                        help="print the hex frame(s), don't send")
        sp.add_argument("--force", action="store_true",
                        help="send even if the wire opcode/payload is unverified (docs/command-audit.md)")
        sp.set_defaults(func=_handler, _files_builder=None, _files_preview=None)
        return sp

    # dial-list is a live READ (not the generic send-only handler): it reassembles + prints the
    # full 0x16 inventory incl. capacity (count/max_dials/slots_free). _client default => cli._run
    # injects a live+bound client for a non-dry-run invocation.
    sp = subparsers.add_parser("dial-list",
                               help="read the installed dial list + capacity (0x16 live read)")
    sp.add_argument("--dry-run", action="store_true", help="print the request frame hex, don't send")
    sp.add_argument("--force", action="store_true",
                    help="send even if the wire opcode/payload is unverified (docs/command-audit.md)")
    sp.set_defaults(func=_dial_list, _files_builder=build_dial_list_request,
                    _files_preview=None, _client=client)

    sp = _add("dial-switch", "[inferred opcode] switch active watch face by id")
    sp.add_argument("dial_id", type=int, help="dial id (1-5000 built-in / 5001+ custom)")
    sp.set_defaults(_files_builder=build_dial_switch)

    _add("nfc-list", "[inferred opcode] request the NFC card list").set_defaults(
        _files_builder=build_nfc_list_request)

    sp = _add("ota-preview", "PREVIEW ONLY: build the res.ota bulk-transfer plan for a file")
    sp.add_argument("image", help="path to a FA-EE-EB-DE .ota image")
    sp.set_defaults(_files_preview="ota")

    sp = _add("send-file", "PREVIEW ONLY: build a generic/dial/AGPS bulk-transfer plan")
    sp.add_argument("path", help="local file to transfer")
    sp.add_argument("--name", help="on-watch filename (default = basename)")
    sp.set_defaults(_files_preview="file")

    # flash-firmware — DRY-RUN by default (parse + validate + print plan, never transmits). A real
    # flash needs --force-flash AND RECOVERY_PROVEN (#17); it is registered with its OWN handler
    # (not the generic one) and deliberately has NO ``_client`` default, so cli._run never auto-
    # connects for it — the default path cannot touch the radio (see the FIRMWARE FLASH section).
    sp = subparsers.add_parser(
        "flash-firmware",
        help="validate a FA-EE-EB-DE OTA image + preview the flash plan (DRY-RUN; "
             "--force-flash gated on wired recovery #17)")
    sp.add_argument("image", help="path to a FA-EE-EB-DE .ota firmware image")
    sp.add_argument("--chunk", type=int, default=CHUNK_MAX,
                    help=f"D2 payload bytes per chunk (default/max {CHUNK_MAX})")
    sp.add_argument("--force-flash", dest="force_flash", action="store_true",
                    help="ARM a real flash. Refused until wired recovery (#17) is proven; a "
                         "CRC-invalid image is refused regardless. Default is dry-run.")
    sp.add_argument("--probe", action="store_true",
                    help="READ-ONLY on-watch probe: after the offline dry-run, connect + bind, "
                         "send ONLY the D3 state query, print the watch's OTA state, disconnect. "
                         "Never sends firmware (D1/D2/D4); exempt from --force-flash / #17.")
    sp.set_defaults(func=_flash)
