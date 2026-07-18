"""Command-line interface: python -m starmax_client <subcommand>.

Subcommands: scan, pair, set-time, notify, find, sync-health, monitor, raw-accel, crown.

All live subcommands (everything except ``scan``) connect, run the bind handshake, then
perform the action. Bind is accountless on the BLE channel (see README "Bind / auth").
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import logging
import sys
from typing import Optional

from . import commands as C
from . import protobuf as pb
from .framing import Frame
from .records import parse_health_record_header
from .transport import DEFAULT_NAME_PREFIX, StarmaxClient, scan
from . import framing
from . import rawaccel as _rawaccel
from . import crown as _crown
from .commands import command_catalog, invoke_builder, register_all
from .commands import health as _health


# --------------------------------------------------------------------------- helpers
async def _resolve_address(args) -> Optional[str]:
    if args.address:
        return args.address
    print(f"No --address given; scanning for {DEFAULT_NAME_PREFIX}* ...", file=sys.stderr)
    found = await scan(timeout=args.scan_timeout)
    if not found:
        print("No GTX2 watch found. Is it advertising / in range?", file=sys.stderr)
        return None
    name, address, rssi = found[0]
    print(f"Using {name} [{address}] rssi={rssi}", file=sys.stderr)
    return address


def _describe_bind(fr: Frame) -> None:
    """Print the useful fields of the 0x01 bind descriptor (§3.1)."""
    fields = {f: v for f, _w, v in pb.parse(fr.payload)}
    print("Bound. Device descriptor:")
    if 18 in fields and isinstance(fields[18], (bytes, bytearray)):
        try:
            print(f"  chipset/module : {fields[18].decode('utf-8', 'replace')}")
        except Exception:
            pass
    if 11 in fields and isinstance(fields[11], (bytes, bytearray)):
        fw = {f: v for f, _w, v in pb.parse(fields[11])}
        if 1 in fw:
            print(f"  firmware build : {fw[1]}")
    if 19 in fields and isinstance(fields[19], (bytes, bytearray)):
        scr = {f: v for f, _w, v in pb.parse(fields[19])}
        if 1 in scr and 2 in scr:
            print(f"  screen         : {scr[1]} x {scr[2]}")
    if 9 in fields and isinstance(fields[9], (bytes, bytearray)):
        print(f"  device MAC     : {fields[9].hex(':')}")


async def _connect_and_bind(address: str) -> Optional[StarmaxClient]:
    client = StarmaxClient(address)
    await client.connect()
    fr = await client.request(C.build_bind(seq=client.next_seq()), C.OP_BIND, timeout=8.0)
    if fr is None:
        print("Bind handshake got no reply (watch may need re-pairing at the OS level).",
              file=sys.stderr)
    else:
        _describe_bind(fr)
    return client


# --------------------------------------------------------------------------- subcommands
async def cmd_scan(args) -> int:
    found = await scan(timeout=args.scan_timeout, name_prefix=args.prefix)
    if not found:
        print("No matching devices found.")
        return 1
    print(f"{'NAME':<20} {'ADDRESS':<20} RSSI")
    for name, address, rssi in found:
        print(f"{name:<20} {address:<20} {rssi if rssi is not None else '?'}")
    return 0


async def cmd_pair(args) -> int:
    address = await _resolve_address(args)
    if not address:
        return 1
    client = await _connect_and_bind(address)
    await client.disconnect()
    return 0


# Full vendor bind-setup handshake (captured, byte-faithful): the sequence that takes a fresh /
# factory-reset watch OFF its "pair with the app" screen. Mirrors the GB coordinator's
# initializeDevice. The 0x01 bind is sent by _connect_and_bind; set-time is inserted live.
_ACTIVATE_QUERIES = [
    (0x22, "0801"),          # setting query
    (0x05, "080110001800"),  # device-state query
    (0x16, "0800"),          # dial / resource-list query
]
_ACTIVATE_FINALIZE = [
    (0x0e, "0801"),          # health-detection switch read (flag 0)
    (0x0e, "0802120408001001120408011001120408021001120408031001120408041001120408051001120408071001"),  # switch write
    (0x04, "08011002"),      # preferences
    (0x03, "0801"),          # user-profile read
    (0x03, "0802120f08af0110cc3a1800208a0f280130011a180802100118012001280230013801400148015001600168012213081e100c18f40320904e288827300738014000"),  # user-profile push (finalizer)
]


async def cmd_activate(args) -> int:
    """Connect + run the FULL setup handshake so a fresh watch leaves its pairing screen (§4)."""
    address = await _resolve_address(args)
    if not address:
        return 1
    client = await _connect_and_bind(address)  # connect + 0x01 bind (+ descriptor)
    for op, ph in _ACTIVATE_QUERIES:
        await client.send_raw(framing.build_command(op, bytes.fromhex(ph), flag=0, seq=client.next_seq()))
        await asyncio.sleep(0.15)
    await client.send_raw(C.build_set_time(_dt.datetime.now().astimezone(), seq=client.next_seq()))
    await asyncio.sleep(0.15)
    for op, ph in _ACTIVATE_FINALIZE:
        await client.send_raw(framing.build_command(op, bytes.fromhex(ph), flag=0, seq=client.next_seq()))
        await asyncio.sleep(0.15)
    print("Sent full activation handshake (0x22/0x05/0x16 -> set-time -> 0x0e -> 0x04 -> 0x03 profile).")
    print("The watch should now leave its 'pair with the app' screen.")
    await asyncio.sleep(0.5)
    await client.disconnect()
    return 0


async def cmd_set_time(args) -> int:
    address = await _resolve_address(args)
    if not address:
        return 1
    client = await _connect_and_bind(address)
    when = _dt.datetime.now().astimezone()  # local, timezone-aware
    ack = await client.request(C.build_set_time(when, seq=client.next_seq()),
                               C.OP_SET_TIME, timeout=5.0)
    print(f"Set time to {when.isoformat()} -- {'acked' if ack else 'no ack (may still apply)'}")
    await client.disconnect()
    return 0


async def cmd_notify(args) -> int:
    address = await _resolve_address(args)
    if not address:
        return 1
    client = await _connect_and_bind(address)
    # OPT-IN notification-enable exchange (0x04 feature bitmap + 0x03 profile/toggles bundle) —
    # the vendor app sends it before any 0x11. It is OFF by default because (a) the 0x03 bundle
    # writes a default profile, resetting the watch's profile/goals, and (b) notifications still
    # won't DISPLAY on LE-only anyway (classic-BT companion gate — docs/notifications.md). So
    # plain `notify` just sends the 0x11 (no side-effect); pass --enable only when a companion
    # link exists (or after porting to a context with a real profile).
    if getattr(args, "enable", False):
        from .commands.notify import enable_notifications
        await enable_notifications(client)
    if args.summary:
        frame = C.build_notification_summary(args.title, seq=client.next_seq())
    else:
        frame = C.build_notification_detailed(args.title, args.body or "",
                                              seq=client.next_seq())
    await client.send_raw(frame)
    await asyncio.sleep(0.5)  # let the write flush before disconnecting
    print(f"Pushed notification: {args.title!r}"
          + (f" / {args.body!r}" if args.body else ""))
    await client.disconnect()
    return 0


async def cmd_find(args) -> int:
    address = await _resolve_address(args)
    if not address:
        return 1
    client = await _connect_and_bind(address)
    if args.stop:
        await client.send_raw(C.build_find_device(False, seq=client.next_seq()))
        print("Sent stop-buzz.")
    else:
        await client.send_raw(C.build_find_device(True, seq=client.next_seq()))
        print(f"Buzzing watch for {args.duration}s ...")
        await asyncio.sleep(args.duration)
        await client.send_raw(C.build_find_device(False, seq=client.next_seq()))
        print("Stopped.")
    await client.disconnect()
    return 0


async def cmd_sync_health(args) -> int:
    address = await _resolve_address(args)
    if not address:
        return 1
    client = await _connect_and_bind(address)
    cats = [args.category] if args.category is not None else list(C.SYNC_CATEGORIES)
    # AUTHORITATIVE syncType labels (vendor Dart module analysis + live poll 2026-07-12).
    labels = {0: "HR/activity", 1: "stress", 2: "SpO2", 3: "sleep",
              4: "workout", 5: "activity/steps", 7: "HRV"}
    from .records import extract_activity, CAT_ACTIVITY, CAT_WORKOUT
    from .workout import parse_workout_klv
    print(f"{'CAT':<4} {'LABEL':<14} {'DATE':<12} {'BYTES':<6} PRESENT")
    for cat in cats:
        fr = await client.request(C.build_health_sync(cat, seq=client.next_seq()),
                                  C.OP_HEALTH_SYNC, timeout=5.0)
        if fr is None:
            print(f"{cat:<4} {labels.get(cat, '?'):<14} {'-':<12} {'-':<6} no-reply")
            continue
        h = parse_health_record_header(fr.payload)
        date = (f"{h.year:04d}-{h.month:02d}-{h.day:02d}"
                if h.year else "-")
        print(f"{cat:<4} {labels.get(cat, '?'):<14} {date:<12} {h.length:<6} {h.present}")
        if cat == CAT_ACTIVITY:
            # cat-5 ActivityDataModel: decode the daily totals (steps/distance/calories).
            act = extract_activity(fr.payload)
            if act is not None:
                print(f"      steps={act.steps}  distance={act.distance_m} m  "
                      f"calories={act.calories}")
        if cat == CAT_WORKOUT:
            # cat-4 workout: decode the SportHead summary (pinned fields).
            w = parse_workout_klv(fr.payload)
            if w.head is not None:
                s = w.head
                print(f"      {s.start_time}  {s.duration_s}s  steps={s.total_step}  "
                      f"cal={s.total_calories}  dist={s.total_distance_m} m  "
                      f"HR avg/max/min={s.avg_hr}/{s.max_hr}/{s.min_hr}  stride={s.stride_cm} cm")
                if not w.trail:
                    print("      (no GPS route on this firmware — trailData empty)")
        if args.raw:
            # Biometric bytes: only dumped on explicit --raw (privacy).
            print(f"      raw: {fr.payload.hex()}")
    await client.disconnect()
    return 0


# --------------------------------------------------------------------------- monitor
# Live realtime sensor stream + BLE link telemetry, wired into one verb. The realtime channel is
# SCHEMA-derived and its wire opcode is UNRESOLVED (never captured), so build_realtime_open defaults
# to the best-guess health opcode (0x0e). Running this live doubles as opcode discovery: we print
# every inbound frame's opcode so a real stream is visible even before the opcode is confirmed.
_MONITOR_SENSORS = ("accel", "hr", "steps", "spo2", "bp", "temp", "sugar")


def _monitor_open_frame(sensors, seq: int) -> bytes:
    """Build a RealTimeOpen frame enabling exactly the named sensors (empty list = close stream)."""
    want = set(sensors)
    return _health.build_realtime_open(
        gsensor="accel" in want, heart_rate="hr" in want, steps="steps" in want,
        blood_oxygen="spo2" in want, blood_pressure="bp" in want,
        temp="temp" in want, blood_sugar="sugar" in want, seq=seq)


async def cmd_monitor(args) -> int:
    """Live telemetry: realtime sensor stream (accel/HR/…) + BLE link RSSI/MTU, in one dashboard.

    EXPERIMENTAL: the realtime wire opcode is UNRESOLVED. We send the schema RealTimeOpen (best
    guess 0x0e) and print each inbound frame; if no sensor frames appear, the opcode guess is wrong
    (still useful — the 'opcodes seen' summary shows what the watch actually pushed).
    """
    sensors = list(args.sensors) if args.sensors else ["accel", "hr", "steps", "spo2"]
    if "all" in sensors:
        sensors = list(_MONITOR_SENSORS)

    if args.dry_run:                       # offline: just show the frame we'd send
        print(bytes(_monitor_open_frame(sensors, seq=0)).hex())
        return 0

    # Scan first so we can report link RSSI — a link metric measured by our adapter, never sent
    # by the watch (§telemetry). --address skips the scan (then RSSI is unavailable).
    address, scan_rssi = args.address, None
    if not address:
        found = await scan(timeout=args.scan_timeout)
        if not found:
            print("No GTX2 watch found. Is it free (disconnected from the phone) and awake?",
                  file=sys.stderr)
            return 1
        name, address, scan_rssi = found[0]
        print(f"Using {name} [{address}]", file=sys.stderr)

    client = await _connect_and_bind(address)
    mtu = getattr(client._client, "mtu_size", None)
    print(f"\n  link   RSSI {scan_rssi if scan_rssi is not None else '?'} dBm    "
          f"MTU {mtu or '?'}    streaming: {', '.join(sensors)}")
    print(f"  {'-' * 64}")

    seen_ops: dict = {}
    counter = {"n": 0}
    # Known protobuf opcodes that are NEVER realtime samples (bind/settings/pushes): parsing them
    # as RealTimeData renders garbage (live-observed: the duplicate 0x01 bind descriptor showed
    # its MAC bytes as "temp"). 0x0e and unknown opcodes stay eligible — that's the discovery path.
    known_non_realtime = {0x01, 0x02, 0x03, 0x04, 0x05, 0x07, 0x10, 0x11, 0x12, 0x13, 0x16,
                          0x18, 0x22}

    def on_frame(fr: Frame) -> None:
        seen_ops[fr.opcode] = seen_ops.get(fr.opcode, 0) + 1
        if fr.opcode in known_non_realtime:
            return
        try:
            rt = _health.parse_realtime_data(fr.payload)
        except Exception:                  # noqa: BLE001 - a non-realtime frame must never crash
            return
        if not rt:
            return
        g = rt.get("gsensors") or []
        parts = []
        if g:
            parts.append("accel " + " ".join(
                f"[{s['x']:>6} {s['y']:>6} {s['z']:>6}]" for s in g[:3]))
        for key, label in (("heart_rate", "HR"), ("steps", "steps"), ("calories", "cal"),
                           ("distance", "dist"), ("blood_oxygen", "SpO2"), ("temp", "temp"),
                           ("blood_sugar", "sugar")):
            if rt.get(key):
                parts.append(f"{label} {rt[key]}")
        if parts:
            counter["n"] += 1
            print(f"  op=0x{fr.opcode:02x} | " + "   ".join(parts))
        elif args.verbose:                 # discovery aid: show unclassified frames
            print(f"  op=0x{fr.opcode:02x} flag={fr.flag} raw {fr.payload.hex()}")

    client.add_listener(on_frame)
    await client.send_raw(_monitor_open_frame(sensors, seq=client.next_seq()))
    print(f"  streaming {args.duration:.0f}s — move your wrist!  (Ctrl-C to stop early)\n")
    try:
        await asyncio.sleep(args.duration)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    try:
        await client.send_raw(_monitor_open_frame([], seq=client.next_seq()))  # close the stream
    except Exception:                      # noqa: BLE001 - best-effort close
        pass
    print(f"\n  {'-' * 64}")
    ops = ", ".join(f"0x{op:02x}x{n}" for op, n in sorted(seen_ops.items())) or "none"
    print(f"  {counter['n']} sensor sample-frame(s);  opcodes seen: {ops}")
    if counter["n"] == 0:
        print("  (no sensor frames: the realtime opcode guess (0x0e) is likely wrong. The opcodes "
              "above are what the watch DID push — rerun with -v to see raw frames.)")
    await client.disconnect()
    return 0


# --------------------------------------------------------------------------- raw-accel [CFW]
def _print_rawaccel_batch(batch, *, show_raw: bool) -> None:
    """Print a decoded raw-accel batch: header line + up to a few samples in g (and raw if asked)."""
    print(f"  frame_seq={batch.frame_seq} rate={batch.rate_hz}Hz range=+/-{batch.range_g}g "
          f"res={batch.res_bits}bit count={batch.count} base_ts={batch.base_ts_ms}ms")
    for i, s in enumerate(batch.samples[:6]):
        raw = f"  raw[{s.x:>6} {s.y:>6} {s.z:>6}]" if show_raw else ""
        print(f"    [{i:>2}] g[{s.gx:+.3f} {s.gy:+.3f} {s.gz:+.3f}]{raw}")
    if batch.count > 6:
        print(f"    ... {batch.count - 6} more")


async def cmd_rawaccel(args) -> int:
    """[CFW] Raw LIS2DH12 accel stream over the custom-firmware 0xA0 channel.

    EXPERIMENTAL / custom-firmware only: opcode 0xA0 does not exist on any shipping GTX2 (stock
    exposes no raw-accel path — docs/custom-firmware-poc.md Part 3). Offline modes always work:
    ``--decode <hex>`` decodes a data frame, ``--dry-run`` prints the enable frame. A LIVE stream
    needs ``--force`` (and real custom firmware) — see docs/cfw-rawaccel-protocol.md.
    """
    if args.decode:                       # offline: decode a data frame (wire frame or bare payload)
        raw = bytes.fromhex(args.decode.replace(" ", "").replace(":", ""))
        if raw[:1] == bytes([framing.SOF]):
            fr = framing.parse_frame(raw, direction=framing.DIR_WATCH_TO_APP)
            if fr.opcode != _rawaccel.OP_RAW_ACCEL:
                print(f"warning: opcode 0x{fr.opcode:02x} != raw-accel 0x{_rawaccel.OP_RAW_ACCEL:02x}",
                      file=sys.stderr)
            if fr.crc_ok is False:
                print("warning: frame CRC mismatch", file=sys.stderr)
            payload = fr.payload
        else:
            payload = raw                 # treat as a bare data-frame payload (header+samples)
        _print_rawaccel_batch(_rawaccel.parse_rawaccel_frame(payload), show_raw=True)
        return 0

    enable = _rawaccel.build_rawaccel_enable(rate_hz=args.rate, range_g=args.range,
                                             res_bits=args.res, seq=0)
    if args.dry_run:                      # offline: show the enable frame we'd send
        print(enable.hex())
        return 0

    if not args.force:                    # CFW live-send gate
        print("Refusing to stream raw-accel live: opcode 0xA0 is CUSTOM-FIRMWARE only and NO "
              "shipping GTX2 runs it (stock has no raw-accel path — docs/custom-firmware-poc.md "
              "Part 3). Use --decode <hex> to decode a frame, --dry-run to preview the enable "
              "frame, or --force once custom firmware is flashed.", file=sys.stderr)
        return 2

    address = await _resolve_address(args)
    if not address:
        return 1
    client = await _connect_and_bind(address)
    seqs: List[int] = []
    n = {"frames": 0}

    def on_frame(fr: Frame) -> None:
        if fr.opcode != _rawaccel.OP_RAW_ACCEL or fr.flag != _rawaccel.FLAG_DATA:
            return
        try:
            batch = _rawaccel.parse_rawaccel_frame(fr.payload)
        except _rawaccel.RawAccelError as e:
            print(f"  (bad raw-accel frame: {e})", file=sys.stderr)
            return
        seqs.append(batch.frame_seq)
        n["frames"] += 1
        _print_rawaccel_batch(batch, show_raw=args.verbose)

    client.add_listener(on_frame)
    ack = await client.request(_rawaccel.build_rawaccel_enable(
        rate_hz=args.rate, range_g=args.range, res_bits=args.res, seq=client.next_seq()),
        _rawaccel.OP_RAW_ACCEL, timeout=5.0)
    if ack is not None and ack.flag == _rawaccel.FLAG_CONTROL:
        a = _rawaccel.parse_rawaccel_ack(ack.payload)
        print(f"  enable ack: status={a['status']} rate={a['rate_hz']}Hz "
              f"range=+/-{a['range_g']}g res={a['res_bits']}bit")
    else:
        print("  (no enable ack — the firmware may not implement 0xA0)")
    print(f"  streaming {args.duration:.0f}s — move the watch!  (Ctrl-C to stop early)\n")
    try:
        await asyncio.sleep(args.duration)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    try:
        await client.send_raw(_rawaccel.build_rawaccel_disable(seq=client.next_seq()))
    except Exception:                     # noqa: BLE001 - best-effort close
        pass
    dropped = _rawaccel.detect_drops(seqs)
    print(f"\n  {n['frames']} data frame(s); dropped ~{dropped} frame(s) (by frame_seq gaps)")
    await client.disconnect()
    return 0


# --------------------------------------------------------------------------- crown [CFW]
def _print_crown_batch(batch) -> None:
    """Print a decoded crown batch: header line + one line per event (rotation delta / button)."""
    print(f"  frame_seq={batch.frame_seq} count={batch.count} base_ts={batch.base_ts_ms}ms "
          f"net_rotation={batch.net_rotation():+d} ({'CW+' if batch.clockwise_positive else 'CCW+'})")
    for i, e in enumerate(batch.events):
        if e.is_rotation:
            print(f"    [{i:>2}] rotate {e.rotation_delta:+d}")
        else:
            print(f"    [{i:>2}] button {e.button_action or f'?{e.ev_detail}'}")


async def cmd_crown(args) -> int:
    """[CFW] Rotary-crown event stream over the custom-firmware 0xA1 channel.

    EXPERIMENTAL / custom-firmware only: opcode 0xA1 does not exist on any shipping GTX2 (stock
    handles the crown locally and emits nothing over BLE — docs/cfw-crown-protocol.md §1). Offline
    modes always work: ``--decode <hex>`` decodes a data frame, ``--dry-run`` prints the enable
    frame. A LIVE stream needs ``--force`` (and real custom firmware).
    """
    if args.decode:                       # offline: decode a data frame (wire frame or bare payload)
        raw = bytes.fromhex(args.decode.replace(" ", "").replace(":", ""))
        if raw[:1] == bytes([framing.SOF]):
            fr = framing.parse_frame(raw, direction=framing.DIR_WATCH_TO_APP)
            if fr.opcode != _crown.OP_CROWN:
                print(f"warning: opcode 0x{fr.opcode:02x} != crown 0x{_crown.OP_CROWN:02x}",
                      file=sys.stderr)
            if fr.crc_ok is False:
                print("warning: frame CRC mismatch", file=sys.stderr)
            payload = fr.payload
        else:
            payload = raw                 # treat as a bare data-frame payload (header+events)
        _print_crown_batch(_crown.parse_crown_frame(payload))
        return 0

    enable = _crown.build_crown_enable(report_rotation=not args.no_rotation,
                                       report_button=not args.no_button,
                                       coalesce_ms=args.coalesce, seq=0)
    if args.dry_run:                      # offline: show the enable frame we'd send
        print(enable.hex())
        return 0

    if not args.force:                    # CFW live-send gate
        print("Refusing to stream crown live: opcode 0xA1 is CUSTOM-FIRMWARE only and NO shipping "
              "GTX2 runs it (stock handles the crown on-device, emits nothing over BLE — "
              "docs/cfw-crown-protocol.md §1). Use --decode <hex> to decode a frame, --dry-run to "
              "preview the enable frame, or --force once custom firmware is flashed.", file=sys.stderr)
        return 2

    address = await _resolve_address(args)
    if not address:
        return 1
    client = await _connect_and_bind(address)
    seqs: List[int] = []
    n = {"frames": 0}

    def on_frame(fr: Frame) -> None:
        if fr.opcode != _crown.OP_CROWN or fr.flag != _crown.FLAG_DATA:
            return
        try:
            batch = _crown.parse_crown_frame(fr.payload)
        except _crown.CrownError as e:
            print(f"  (bad crown frame: {e})", file=sys.stderr)
            return
        seqs.append(batch.frame_seq)
        n["frames"] += 1
        _print_crown_batch(batch)

    client.add_listener(on_frame)
    ack = await client.request(_crown.build_crown_enable(
        report_rotation=not args.no_rotation, report_button=not args.no_button,
        coalesce_ms=args.coalesce, seq=client.next_seq()), _crown.OP_CROWN, timeout=5.0)
    if ack is not None and ack.flag == _crown.FLAG_CONTROL:
        a = _crown.parse_crown_ack(ack.payload)
        print(f"  enable ack: status={a['status']} rotation={a['report_rotation']} "
              f"button={a['report_button']} detents/rev={a['detents_per_rev']}")
    else:
        print("  (no enable ack — the firmware may not implement 0xA1)")
    print(f"  streaming {args.duration:.0f}s — turn/press the crown!  (Ctrl-C to stop early)\n")
    try:
        await asyncio.sleep(args.duration)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    try:
        await client.send_raw(_crown.build_crown_disable(seq=client.next_seq()))
    except Exception:                     # noqa: BLE001 - best-effort close
        pass
    dropped = _crown.detect_drops(seqs)
    print(f"\n  {n['frames']} crown frame(s); dropped ~{dropped} frame(s) (by frame_seq gaps)")
    await client.disconnect()
    return 0


async def cmd_commands(args) -> int:
    """List every auto-discovered module command, grouped by module (offline, no BLE)."""
    cat = command_catalog()
    total = sum(len(v) for v in cat.values())
    print(f"{total} module command(s) across {len(cat)} group(s):")
    for group in sorted(cat):
        print(f"\n  {group}  ({len(cat[group])})")
        for name in sorted(cat[group]):
            try:
                res = invoke_builder(cat[group][name])
                first = res if isinstance(res, (bytes, bytearray)) else res[0]
                fr = framing.parse_frame(bytes(first), direction=framing.DIR_APP_TO_WATCH)
                meta = f"op=0x{fr.opcode:02x} flag={fr.flag} {len(bytes(first))}B"
            except Exception as e:  # noqa: BLE001 - listing must never crash on one builder
                meta = f"(sample build failed: {e})"
            print(f"      {name:<24} {meta}")
    print("\n  core (interactive top-level verbs): scan  pair  set-time  notify  find  sync-health  monitor  raw-accel  crown")
    return 0


# --------------------------------------------------------------------------- argparse
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m starmax_client",
                                description="Standalone BLE client for the Starmax GTX2.")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--address", help="watch BLE address (skip scan)")
    p.add_argument("--scan-timeout", type=float, default=8.0, help="scan seconds (default 8)")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("scan", help="scan for advertising watches")
    sp.add_argument("--prefix", default=DEFAULT_NAME_PREFIX,
                    help=f"name prefix filter (default {DEFAULT_NAME_PREFIX}; '' = all)")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("pair", help="connect + bind handshake, print device descriptor")
    sp.set_defaults(func=cmd_pair)

    sp = sub.add_parser("activate",
                        help="connect + FULL setup handshake (takes a fresh watch off its pairing screen)")
    sp.set_defaults(func=cmd_activate)

    sp = sub.add_parser("set-time", help="set the watch clock to now (local tz)")
    sp.set_defaults(func=cmd_set_time)

    sp = sub.add_parser("notify", help="push a notification")
    sp.add_argument("title", help="notification title / text")
    sp.add_argument("--body", help="notification body (0x11 detailed only)")
    sp.add_argument("--summary", action="store_true",
                    help="send as 0x13 summary line instead of 0x11 detailed")
    sp.add_argument("--enable", action="store_true",
                    help="ALSO send the 0x04+0x03 notification-enable exchange first. Off by "
                         "default: it writes a default profile bundle (resets the watch profile) "
                         "and notifications won't display on LE-only anyway (classic-BT gate, "
                         "docs/notifications.md). Use only when a companion link exists.")
    sp.set_defaults(func=cmd_notify)

    sp = sub.add_parser("find", help="ring/buzz the watch")
    sp.add_argument("--duration", type=float, default=5.0, help="buzz seconds (default 5)")
    sp.add_argument("--stop", action="store_true", help="just send stop-buzz")
    sp.set_defaults(func=cmd_find)

    sp = sub.add_parser("sync-health", help="pull health/history records (dates + sizes)")
    sp.add_argument("--category", type=int, choices=C.SYNC_CATEGORIES,
                    help="single category (default: all)")
    sp.add_argument("--raw", action="store_true",
                    help="also hex-dump raw record bytes (may include biometrics)")
    sp.set_defaults(func=cmd_sync_health)

    sp = sub.add_parser("monitor",
                        help="live telemetry: realtime sensor stream (accel/HR/…) + link RSSI/MTU")
    sp.add_argument("--sensors", nargs="+", metavar="S", choices=["all", *_MONITOR_SENSORS],
                    help="sensors to stream: accel hr steps spo2 bp temp sugar | all "
                         "(default: accel hr steps spo2)")
    sp.add_argument("--duration", type=float, default=30.0, help="stream seconds (default 30)")
    sp.add_argument("--dry-run", action="store_true",
                    help="print the realtime-open frame and exit (no BLE)")
    sp.set_defaults(func=cmd_monitor)

    sp = sub.add_parser("raw-accel",
                        help="[CFW] raw LIS2DH12 accel stream (custom-firmware 0xA0; --decode/--dry-run offline)")
    sp.add_argument("--rate", type=int, choices=sorted(_rawaccel.RATE_TO_CODE), default=50,
                    help="sample rate Hz (default 50)")
    sp.add_argument("--range", type=int, choices=sorted(_rawaccel.RANGE_TO_CODE), default=8,
                    help="full-scale +/- g (default 8)")
    sp.add_argument("--res", type=int, choices=sorted(_rawaccel.RES_TO_CODE), default=12,
                    help="output resolution bits = LIS2DH12 mode (default 12=high-res)")
    sp.add_argument("--duration", type=float, default=10.0, help="live stream seconds (default 10)")
    sp.add_argument("--decode", metavar="HEX",
                    help="OFFLINE: decode a raw-accel data frame (full C1 wire frame or bare payload)")
    sp.add_argument("--dry-run", action="store_true",
                    help="print the enable frame and exit (no BLE)")
    sp.add_argument("--force", action="store_true",
                    help="stream LIVE (custom firmware required; opcode 0xA0 is unverified)")
    sp.set_defaults(func=cmd_rawaccel)

    sp = sub.add_parser("crown",
                        help="[CFW] rotary-crown events (custom-firmware 0xA1; --decode/--dry-run offline)")
    sp.add_argument("--coalesce", type=int, default=0, metavar="MS",
                    help="rotation coalescing window in ms (0 = emit each detent immediately)")
    sp.add_argument("--no-rotation", action="store_true", help="do not stream rotation events")
    sp.add_argument("--no-button", action="store_true", help="do not stream button events")
    sp.add_argument("--duration", type=float, default=15.0, help="live stream seconds (default 15)")
    sp.add_argument("--decode", metavar="HEX",
                    help="OFFLINE: decode a crown data frame (full C1 wire frame or bare payload)")
    sp.add_argument("--dry-run", action="store_true",
                    help="print the enable frame and exit (no BLE)")
    sp.add_argument("--force", action="store_true",
                    help="stream LIVE (custom firmware required; opcode 0xA1 is unverified)")
    sp.set_defaults(func=cmd_crown)

    # Auto-discover feature-group command modules (health/settings/notify/files) and let each
    # register its own subcommands. client=None at build time; main() injects a live, bound
    # client at runtime for non-dry-run invocations (see _run). A group whose register() raises
    # is skipped, never fatal.
    for _problem in register_all(sub, client=None):
        logging.getLogger("starmax_client.cli").debug("command-module: %s", _problem)

    cp = sub.add_parser("commands",
                        help="list every auto-discovered module command, grouped by module")
    cp.add_argument("--list", action="store_true", help="list commands (the default action)")
    cp.set_defaults(func=cmd_commands)
    return p


async def _run(args) -> int:
    """Dispatch a subcommand, injecting a live+bound client for module subcommands.

    Feature-group subcommands (health/settings/…) declare an ``args._client`` default of None
    and support ``--dry-run``. For a non-dry-run module subcommand we resolve the address,
    connect, run the bind handshake, and hand the connected client to the module via
    ``args._client``. Core verbs manage their own connection and are untouched.
    """
    needs_client = (hasattr(args, "_client") and getattr(args, "_client") is None
                    and not getattr(args, "dry_run", False))
    # Live-send safety guard: refuse to fire an unverified/wrong-layer command at the watch
    # without --force (--dry-run stays open). Prevents e.g. the SDK-REV settings opcodes or the
    # re-opcoded camera from being sent by accident (docs/command-audit.md).
    if needs_client and C.requires_force(getattr(args, "command", None)) \
            and not getattr(args, "force", False):
        print(f"Refusing to send '{args.command}' live: its wire opcode/payload is UNVERIFIED "
              f"(schema/inferred — see docs/command-audit.md). Re-run with --dry-run to inspect "
              f"the frame, or --force to send anyway.", file=sys.stderr)
        return 2
    client = None
    try:
        if needs_client:
            address = await _resolve_address(args)
            if not address:
                return 1
            client = await _connect_and_bind(address)
            args._client = client
        return await args.func(args)
    finally:
        if client is not None:
            await client.disconnect()


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001 - surface a clean message, full trace under -v
        if args.verbose:
            raise
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
