"""Notification / telephony / media command group (Track-B standalone, group "notify").

Covers the phone->watch "notify" surface plus parsing of the watch->phone control pushes.
Sources: the vendor APK-derived schema (``Notify.*`` messages; internal RE notes not shipped here)
and the capture-verified wire spec (docs/protocol-spec.md). Reuses the shared core (framing +
protobuf) and the base builders; does not rewrite them.

Provenance (be honest — tags mirror the audit's [CAP]/[SCHEMA]):
  * [CAP] VERIFIED against captures/:
      - 0x11 detailed notification  (protocol-spec §3.4)  build  -- reused from base
      - 0x13 summary notification   (protocol-spec §3.5)  build  -- reused from base
      - 0x10 control push (media / find-phone / records-available)  parse  (§7, §9.4, §9.5)
  * [SCHEMA] UNVERIFIED — schema-derived, NOT in our capture, so the wire opcode/flag is a best
    inference and MUST be confirmed with a real-watch capture. The protobuf *payloads* are
    schema-correct; only the enclosing opcode is unconfirmed (protocol-spec §1.4: SDK REV_TYPE
    != wire opcode; and in the captured session these used classic-BT HFP/HID — §8/§9.6):
      - incoming-call notify  (schema Notify.PhoneControl)
      - music-state push      (schema Notify.MusicControl)   -- "follow the phone" (state only)
      - camera control        (schema Notify.CameraControl)
"""
from __future__ import annotations

import argparse
import enum
from dataclasses import dataclass
from typing import Dict, Optional

from starmax_client import framing
from starmax_client.protobuf import ProtobufWriter, parse as pb_parse
# Reuse the capture-verified 0x11/0x13 builders from base (single source of truth).
from starmax_client.commands.base import (
    build_notification_detailed,
    build_notification_summary,
    OP_NOTIFY_DETAILED,
    OP_NOTIFY_SUMMARY,
)

GROUP = "notify"

# --------------------------------------------------------------------------- opcodes
OP_CONTROL_PUSH = 0x10       # watch->app control channel [CAP] (protocol-spec §7/§9.4/§9.5)

# UNVERIFIED wire opcodes — schema-derived, NOT capture-confirmed. The prefix keeps them from
# being mistaken for observed values; a real-watch capture must confirm the true opcode/flag.
#
# DECOMPILE UPDATE (internal opcode-resolution RE): these guesses were seeded from the Java
# com.starmax.bluetoothsdk SDK, which is a DIFFERENT Starmax watch family (0xDA-framed,
# CRC-16/ARC) — NOT the GTX2 (0xC1, CRC-CCITT-FALSE). So the values here are wrong-family, and the
# real GTX2 opcodes live in the Dart creek_blue_manage plugin (Dart-AOT-only). More importantly:
#   * incoming-call is classic-BT HFP on the GTX2 — there is NO C1 call opcode at all (§8).
#   * the watch->app MUSIC/media *control* (play/pause/next/prev) that IS an HA trigger rides the
#     captured 0x10 control push (see parse_control_push / OP_CONTROL_PUSH below), NOT these sends.
# Keep all three --force-gated; do not treat them as resolved.
UNVERIFIED_OP_PHONE_CONTROL = 0x14   # Notify.PhoneControl  (incoming call)  -- GUESS (call=HFP, no C1 op)
UNVERIFIED_OP_MUSIC_CONTROL = 0x15   # Notify.MusicControl  (music state)    -- GUESS (wrong-family)
UNVERIFIED_OP_CAMERA_CONTROL = 0x1D  # Notify.CameraControl; wire opcode UNRESOLVED — placeholder.
# NB: was 0x04, which COLLIDES with the real [CAP] feature-bitmap opcode (settings.OP_FEATURE_BITMAP)
# — sending it there would write a feature bitmap, not a camera control. Moved to an unused
# placeholder (0x1d, after sport 0x1a / gps 0x1b / nfc 0x1c) so it can never hit 0x04; still
# UNVERIFIED and --force-gated for live sends (commands.GATED_COMMANDS). Confirm via a capture.


# =========================================================================== BUILD (schema-derived)
class CallType(enum.IntEnum):
    """PhoneControl.type values (inferred; not capture-confirmed)."""
    INCOMING = 1
    ACCEPTED = 2
    ENDED = 3
    MISSED = 4


def build_incoming_call(caller: str, *, call_type: CallType = CallType.INCOMING,
                        status: int = 1, seq: int = 0) -> bytes:
    """Notify the watch of a phone call so it can show caller-ID. [SCHEMA, UNVERIFIED]

    Schema: ``Notify.PhoneControl {1:int32 status, 2:enum type, 3:string value}``; ``value``
    carries the caller name/number. Payload is schema-correct; the wire opcode is a best guess
    (calls used classic-BT HFP in the capture — protocol-spec §8 — so no C1 call frame exists).
    """
    payload = (ProtobufWriter()
               .varint(1, status)
               .varint(2, int(call_type))
               .string(3, caller)
               .to_bytes())
    return framing.build_command(UNVERIFIED_OP_PHONE_CONTROL, payload, flag=0, seq=seq)


class MusicType(enum.IntEnum):
    """MusicControl.type values. Mirrors the watch->app media actions (§9.4); inferred."""
    PLAY = 1
    PAUSE = 2
    NEXT = 3
    PREV = 4
    VOLUME_UP = 5
    VOLUME_DOWN = 6


def build_music_state(music_type: MusicType, *, status: int = 1, seq: int = 0) -> bytes:
    """Push current music STATE to the watch ("follow the phone"). [SCHEMA, UNVERIFIED]

    Schema: ``Notify.MusicControl {1:int32 status, 2:enum type}``. NOTE: the GTX2 schema has NO
    rich now-playing metadata (no title/artist/album fields exist in any of the 138 messages) —
    only this play-state enum. So metadata push is limited to playback STATE. Wire opcode UNVERIFIED.
    """
    payload = (ProtobufWriter()
               .varint(1, status)
               .varint(2, int(music_type))
               .to_bytes())
    return framing.build_command(UNVERIFIED_OP_MUSIC_CONTROL, payload, flag=0, seq=seq)


class CameraType(enum.IntEnum):
    """CameraControl.type. From SDK STlPhotoContrl (0x01 enter, 0x02 exit); 0x03 shutter inferred."""
    ENTER = 1
    EXIT = 2
    SHUTTER = 3


def build_camera_control(camera_type: CameraType, *, status: int = 1, seq: int = 0) -> bytes:
    """Control the watch camera-remote UI. [SCHEMA, UNVERIFIED]

    Schema: ``Notify.CameraControl {1:int32 status, 2:enum type}``; type from the SDK
    ``STlPhotoContrl`` enum. NOTE: in the capture the shutter came FROM the watch as classic-BT
    HID (protocol-spec §9.6), not C1 — so this phone->watch C1 form is schema-derived, UNVERIFIED.
    """
    payload = (ProtobufWriter()
               .varint(1, status)
               .varint(2, int(camera_type))
               .to_bytes())
    return framing.build_command(UNVERIFIED_OP_CAMERA_CONTROL, payload, flag=0, seq=seq)


# =========================================================================== PARSE (watch->app 0x10)
class ControlKind(enum.Enum):
    MEDIA = "media"                          # watch controls phone playback (§9.4)
    FIND_PHONE = "find_phone"                # watch rings the phone (§9.5)
    RECORDS_AVAILABLE = "records_available"  # "new records" ping (§7)
    UNKNOWN = "unknown"


class MediaAction(enum.IntEnum):
    """0x10 f2 media actions — verified on-device (matches the GB coordinator fix, §9.4).

    Two play/pause buttons: f2=1 and a bare frame with no f2 both mean play/pause; f2=2 =
    previous, f2=3 = next. (The earlier 2=next / 3=previous guess was reversed on real hardware.)"""
    PLAY_PAUSE = 1
    PREV = 2
    NEXT = 3


@dataclass
class ControlPush:
    """A decoded watch->app 0x10 control push."""
    kind: ControlKind
    f1: int
    f2: Optional[int]
    media_action: Optional[MediaAction] = None
    records_count: Optional[int] = None
    raw: bytes = b""


def parse_control_push(data: bytes) -> ControlPush:
    """Parse a watch->app 0x10 control push (payload OR whole 0xC1 frame). [CAP §7/§9.4/§9.5]

    Classifies by f1: 1 = media control (f2 = action), 2/4 = find-phone, 3 = records ping
    (f2 = count). Media-action and find-phone codes are tentative per the spec.
    """
    if data[:1] == bytes([framing.SOF]):
        fr = framing.parse_frame(data)
        if fr.opcode != OP_CONTROL_PUSH:
            raise ValueError(f"not a 0x10 control push (opcode 0x{fr.opcode:02x})")
        payload = fr.payload
    else:
        payload = data

    fields = {f: v for f, _w, v in pb_parse(payload)}
    f1 = fields.get(1)
    f2 = fields.get(2)

    if f1 == 1:
        # Two play/pause buttons: f2=1 and a bare frame with no f2 (parsed as None) both map to
        # PLAY_PAUSE; f2=2 = PREV, f2=3 = NEXT (matches the GB coordinator's verified mapping).
        if f2 is None:
            action = MediaAction.PLAY_PAUSE
        elif f2 in (a.value for a in MediaAction):
            action = MediaAction(f2)
        else:
            action = None
        return ControlPush(ControlKind.MEDIA, f1, f2, media_action=action, raw=payload)
    if f1 in (2, 4):
        return ControlPush(ControlKind.FIND_PHONE, f1, f2, raw=payload)
    if f1 == 3:
        return ControlPush(ControlKind.RECORDS_AVAILABLE, f1, f2, records_count=f2, raw=payload)
    return ControlPush(ControlKind.UNKNOWN, f1 if f1 is not None else -1, f2, raw=payload)


# =========================================================================== enable sequence
async def enable_notifications(client, *, profile=None, delay: float = 0.3) -> None:
    """Send the vendor notification-ENABLE exchange before pushing a ``0x11`` (capture-derived).

    Order, exactly as the app does on connect (from ``notif-real``, internal RE notes):

      1. ``0x04`` feature bitmap (``08011002``) — the notification/feature enable.
      2. ``0x03`` profile+toggles bundle — the per-category notification switches (all on).

    ``client`` needs async ``send_raw`` + ``next_seq`` (real :class:`StarmaxClient` or a fake).
    ``profile`` defaults to neutral placeholder biometrics; pass a real one to avoid overwriting
    the watch's stored profile (the ``0x03`` bundle carries profile+goals alongside the toggles).

    ⚠️ This is the LE-side enable only. On the vendor app the watch also requires **classic-BT /
    HFP companion presence** to actually DISPLAY notifications (internal RE notes); an LE-only host
    sends the enable but cannot be that companion, so
    notifications may still not render. Kept correct + ready for if companion presence is solved.
    """
    import asyncio
    from starmax_client.commands.settings import (
        build_feature_bitmap, build_user_profile, UserProfile)
    p = profile or UserProfile(height_cm=170, weight_kg=65.0, birth_year=1990)
    await client.send_raw(build_feature_bitmap(seq=client.next_seq()))
    if delay:
        await asyncio.sleep(delay)
    await client.send_raw(build_user_profile(p, seq=client.next_seq()))
    if delay:
        await asyncio.sleep(delay)


# =========================================================================== registry
COMMANDS: Dict[str, object] = {
    "notify-detailed": build_notification_detailed,   # reused from base (wire 0x11) [CAP]
    "notify-summary": build_notification_summary,      # reused from base (wire 0x13) [CAP]
    "call": build_incoming_call,                       # [SCHEMA, UNVERIFIED]
    "music": build_music_state,                        # [SCHEMA, UNVERIFIED]
    "camera": build_camera_control,                    # [SCHEMA, UNVERIFIED]
}

PARSERS = {
    "control-push": parse_control_push,   # media / find-phone / records-available (0x10) [CAP]
}


# --------------------------------------------------------------------------- CLI wiring
def _mk_handler(build, opcode: int, *, expect_reply: bool = False):
    """Wrap a per-command builder into an async CLI handler (matches settings.py).

    On ``--dry-run`` (or no wired client) it prints the hex frame and returns without sending.
    """
    async def _handler(args) -> int:
        frame = build(args)
        if getattr(args, "dry_run", False) or getattr(args, "_client", None) is None:
            print(frame.hex())
            return 0
        client = args._client
        if expect_reply:
            fr = await client.request(frame, opcode, timeout=5.0)
            print(fr.payload.hex() if fr is not None else "(no reply)")
        else:
            await client.send_raw(frame)
            print("sent")
        return 0
    return _handler


def _enum_arg(enum_cls):
    """argparse type: case-insensitive enum-member-name -> member."""
    def _conv(s: str):
        try:
            return enum_cls[s.upper()]
        except KeyError:
            raise argparse.ArgumentTypeError(
                f"choose one of {', '.join(m.name.lower() for m in enum_cls)}")
    return _conv


def register(subparsers, client=None) -> None:
    """Add the notify/telephony/media subcommands to ``subparsers``. B5 auto-discovers this.

    Every subcommand supports ``--dry-run`` (print the hex frame, don't transmit) and ``--seq``.
    """
    def _add(name: str, help_: str) -> argparse.ArgumentParser:
        sp = subparsers.add_parser(name, help=help_)
        sp.add_argument("--dry-run", action="store_true", help="print the hex frame, don't send")
        sp.add_argument("--seq", type=lambda s: int(s, 0), default=0, help="frame seq (default 0)")
        sp.add_argument("--force", action="store_true",
                        help="send even if the wire opcode/payload is unverified (docs/command-audit.md)")
        sp.set_defaults(_client=client)
        return sp

    sp = _add("notify-detailed", "push a detailed notification (wire 0x11) [CAP]")
    sp.add_argument("title")
    sp.add_argument("--body", default="")
    sp.set_defaults(func=_mk_handler(
        lambda a: build_notification_detailed(a.title, a.body, seq=a.seq), OP_NOTIFY_DETAILED))

    sp = _add("notify-summary", "push a summary/count line (wire 0x13) [CAP]")
    sp.add_argument("text")
    sp.set_defaults(func=_mk_handler(
        lambda a: build_notification_summary(a.text, seq=a.seq), OP_NOTIFY_SUMMARY))

    sp = _add("call", "notify an incoming/updated call, show caller-ID [SCHEMA, UNVERIFIED]")
    sp.add_argument("caller", help="caller name or number to display")
    sp.add_argument("--type", type=_enum_arg(CallType), default=CallType.INCOMING,
                    help="one of: incoming, accepted, ended, missed")
    sp.set_defaults(func=_mk_handler(
        lambda a: build_incoming_call(a.caller, call_type=a.type, seq=a.seq),
        UNVERIFIED_OP_PHONE_CONTROL))

    sp = _add("music", "push music playback state [SCHEMA, UNVERIFIED]")
    sp.add_argument("action", type=_enum_arg(MusicType),
                    help="one of: play, pause, next, prev, volume_up, volume_down")
    sp.set_defaults(func=_mk_handler(
        lambda a: build_music_state(a.action, seq=a.seq), UNVERIFIED_OP_MUSIC_CONTROL))

    sp = _add("camera", "camera-remote control [SCHEMA, UNVERIFIED]")
    sp.add_argument("action", type=_enum_arg(CameraType), help="one of: enter, exit, shutter")
    sp.set_defaults(func=_mk_handler(
        lambda a: build_camera_control(a.action, seq=a.seq), UNVERIFIED_OP_CAMERA_CONTROL))
