"""Tests for the notify/telephony/media command group.

Byte-exact where a real capture exists: the watch->app 0x10 control pushes are verbatim
frames extracted from captures/final-*.log (PII-free control frames — only f1/f2 control
codes, no personal content). Build-side notification tests are structural + PII-free
(synthetic text). Schema-derived (UNVERIFIED) builders are checked for schema-correct
payloads and for using the UNVERIFIED_* opcode constants.
"""
import argparse
import asyncio

from starmax_client import framing, protobuf as pb
from starmax_client.commands import notify as N


# --- real captured watch->app 0x10 control pushes (byte-exact vectors) ---------------
MEDIA_PLAYPAUSE = "c180000100100f00000000080110014c67"   # f1=1 f2=1     media play/pause (§9.4)
MEDIA_PLAYPAUSE2 = "c180000100100d000000000801cd09"      # f1=1 (no f2)  the OTHER play/pause button
MEDIA_PREV      = "c180000100100f00000000080110022f57"   # f1=1 f2=2     media PREVIOUS (verified on-device)
MEDIA_NEXT      = "c180000100100f00000000080110030e47"   # f1=1 f2=3     media NEXT (verified on-device)
FIND_PHONE_F2   = "c180000100100f00000000080210027f0e"   # f1=2 f2=2  find-phone (§9.5)
FIND_PHONE_F4   = "c180000100100d0000000008046859"       # f1=4       find-phone (2-byte payload)
RECORDS_9       = "c180000100100f00000000080310092488"   # f1=3 f2=9  records-available (§7)
RECORDS_15      = "c180000100100f000000000803100fe2e8"   # f1=3 f2=15
RECORDS_8       = "c180000100100f00000000080310080598"   # f1=3 f2=8

ALL_PUSHES = [MEDIA_PLAYPAUSE, MEDIA_PLAYPAUSE2, MEDIA_NEXT, MEDIA_PREV, FIND_PHONE_F2,
              FIND_PHONE_F4, RECORDS_9, RECORDS_15, RECORDS_8]


# ---------------------------------------------------------------- byte-exact parse (VERIFIED)
def test_captured_pushes_pass_crc():
    """Every real 0x10 push parses and its CRC-16 verifies (proves codec + vectors)."""
    for hexstr in ALL_PUSHES:
        fr = framing.parse_frame(bytes.fromhex(hexstr))
        assert fr.opcode == N.OP_CONTROL_PUSH
        assert fr.crc_ok is True, f"CRC failed for {hexstr}"


def test_parse_media_pushes():
    for hexstr, action in [(MEDIA_PLAYPAUSE, N.MediaAction.PLAY_PAUSE),
                           (MEDIA_PLAYPAUSE2, N.MediaAction.PLAY_PAUSE),  # bare frame (no f2)
                           (MEDIA_NEXT, N.MediaAction.NEXT),
                           (MEDIA_PREV, N.MediaAction.PREV)]:
        ev = N.parse_control_push(bytes.fromhex(hexstr))
        assert ev.kind is N.ControlKind.MEDIA and ev.f1 == 1 and ev.media_action is action


def test_parse_find_phone():
    for hexstr, f1 in [(FIND_PHONE_F2, 2), (FIND_PHONE_F4, 4)]:
        ev = N.parse_control_push(bytes.fromhex(hexstr))
        assert ev.kind is N.ControlKind.FIND_PHONE and ev.f1 == f1


def test_parse_records_available():
    for hexstr, count in [(RECORDS_9, 9), (RECORDS_15, 15), (RECORDS_8, 8)]:
        ev = N.parse_control_push(bytes.fromhex(hexstr))
        assert ev.kind is N.ControlKind.RECORDS_AVAILABLE and ev.records_count == count


def test_parse_accepts_payload_or_frame():
    frame = bytes.fromhex(MEDIA_NEXT)
    payload = framing.parse_frame(frame).payload
    assert N.parse_control_push(frame).f2 == N.parse_control_push(payload).f2 == 3


def test_parse_rejects_wrong_opcode():
    import pytest
    wrong = N.build_notification_detailed("x", seq=1)   # a 0x11 frame
    with pytest.raises(ValueError):
        N.parse_control_push(wrong)


# ---------------------------------------------------------------- build: notifications (VERIFIED, from base)
def test_notification_detailed_structure():
    fr = framing.parse_frame(N.build_notification_detailed("Hello", "World", seq=0x2d))
    assert fr.opcode == N.OP_NOTIFY_DETAILED and fr.flag == 0
    f = {k: v for k, _w, v in pb.parse(fr.payload)}
    assert f[1] == 1 and f[2] == 2 and f[3] == 6 and f[4] == 100 and f[5] == 0
    assert f[6] == b"Hello" and f[7] == b"World"


def test_notification_summary_structure():
    fr = framing.parse_frame(N.build_notification_summary("3 messages", seq=0x44))
    assert fr.opcode == N.OP_NOTIFY_SUMMARY and fr.flag == 1
    f = {k: v for k, _w, v in pb.parse(fr.payload)}
    assert f[1] == 2 and f[2] == 0 and f[3] == 2 and f[4] == 34 and f[5] == b"3 messages"


def test_notification_detailed_utf8():
    fr = framing.parse_frame(N.build_notification_detailed("Café ☕", seq=1))
    f = {k: v for k, _w, v in pb.parse(fr.payload)}
    assert f[6].decode("utf-8") == "Café ☕"


# ---------------------------------------------------------------- build: schema-derived (UNVERIFIED)
def test_incoming_call_payload_matches_schema():
    """Notify.PhoneControl {1:status, 2:type, 3:value}. Payload schema-correct; opcode UNVERIFIED."""
    fr = framing.parse_frame(N.build_incoming_call("Alex Doe", call_type=N.CallType.INCOMING, seq=3))
    assert fr.opcode == N.UNVERIFIED_OP_PHONE_CONTROL
    f = {k: v for k, _w, v in pb.parse(fr.payload)}
    assert f[1] == 1 and f[2] == int(N.CallType.INCOMING) and f[3] == b"Alex Doe"


def test_music_state_payload_matches_schema():
    """Notify.MusicControl {1:status, 2:type} — schema has NO metadata fields."""
    fr = framing.parse_frame(N.build_music_state(N.MusicType.PAUSE, seq=4))
    assert fr.opcode == N.UNVERIFIED_OP_MUSIC_CONTROL
    f = {k: v for k, _w, v in pb.parse(fr.payload)}
    assert f[1] == 1 and f[2] == int(N.MusicType.PAUSE)
    assert set(f) == {1, 2}   # no phantom title/artist


def test_camera_control_payload_matches_schema():
    fr = framing.parse_frame(N.build_camera_control(N.CameraType.SHUTTER, seq=5))
    assert fr.opcode == N.UNVERIFIED_OP_CAMERA_CONTROL
    f = {k: v for k, _w, v in pb.parse(fr.payload)}
    assert f[1] == 1 and f[2] == int(N.CameraType.SHUTTER)


def test_app_to_watch_builds_carry_no_crc():
    """All builders are app->watch commands: LEN == total, no CRC trailer (§1.1)."""
    for frame in [N.build_notification_detailed("x", seq=1),
                  N.build_notification_summary("y", seq=1),
                  N.build_incoming_call("z", seq=1),
                  N.build_music_state(N.MusicType.PLAY, seq=1),
                  N.build_camera_control(N.CameraType.ENTER, seq=1)]:
        fr = framing.parse_frame(frame)
        assert fr.crc_ok is None and fr.length_field == len(frame)


# ---------------------------------------------------------------- registry / CLI contract
def test_commands_dict_are_callable_builders():
    assert set(N.COMMANDS) == {"notify-detailed", "notify-summary", "call", "music", "camera"}
    assert all(callable(fn) for fn in N.COMMANDS.values())


def test_group_attribute():
    assert N.GROUP == "notify"


def test_register_adds_subcommands():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    N.register(sub, client=None)
    assert set(N.COMMANDS).issubset(set(sub.choices))


def test_register_dry_run_prints_hex(capsys):
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    N.register(sub, client=None)
    args = p.parse_args(["notify-summary", "2 msgs", "--dry-run"])
    rc = asyncio.run(args.func(args))
    assert rc == 0
    out = capsys.readouterr().out.strip()
    fr = framing.parse_frame(bytes.fromhex(out))   # bare hex line round-trips to a frame
    assert fr.opcode == N.OP_NOTIFY_SUMMARY


def test_enum_cli_parsing():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    N.register(sub, client=None)
    args = p.parse_args(["music", "next", "--dry-run"])
    assert args.action is N.MusicType.NEXT


# ---------------------------------------------------------------- GB-coordinator parity (issue #10)
def _media_action_for(f2):
    """Build a minimal 0x10 media payload (f1=1 [+ f2]) and return the parsed media_action."""
    w = pb.ProtobufWriter().varint(1, 1)
    if f2 is not None:
        w.varint(2, f2)
    return N.parse_control_push(w.to_bytes()).media_action


def test_media_map_matches_gb_coordinator():
    """0x10 media map == StarmaxSupport.handleMediaControl (on-device verified):
    bare frame (no f2) and f2=1 -> PLAYPAUSE; f2=2 -> PREVIOUS; f2=3 -> NEXT; else -> None.
    GB reads an absent f2 as -1; parse_control_push represents the bare frame as f2=None.
    Guards against a regression back to the reversed 2=next/3=prev guess."""
    assert _media_action_for(None) is N.MediaAction.PLAY_PAUSE   # bare frame (GB action -1)
    assert _media_action_for(1) is N.MediaAction.PLAY_PAUSE
    assert _media_action_for(2) is N.MediaAction.PREV
    assert _media_action_for(3) is N.MediaAction.NEXT
    assert _media_action_for(9) is None                          # unhandled -> None
    assert (int(N.MediaAction.PREV), int(N.MediaAction.NEXT)) == (2, 3)


def test_summary_frame_keeps_trailing_empty_f6():
    """The real vendor 0x13 frame ends with an empty field 6 (`...2a <text> 32 00`, spec §3.5);
    the standalone builder reproduces it byte-for-byte. Fidelity guard — NOTE: GB's
    StarmaxMessages.buildSummaryNotification omits this trailing f6, so it is 2 bytes shorter
    than the captured frame; the standalone client stays faithful to the capture."""
    fr = framing.parse_frame(N.build_notification_summary("4 new messages", seq=0))
    fields = pb.parse(fr.payload)
    assert (6, 2, b"") in fields                       # field 6, length-delimited, empty
    assert fr.payload.endswith(bytes([0x32, 0x00]))    # trailing `32 00`


def test_detailed_layout_matches_gb_field_map():
    """0x11 detailed == StarmaxMessages.buildDetailedNotification field map
    (f1=1, f2=2, f3=6, f4=id/count, f5=0, f6=title, f7=body)."""
    fr = framing.parse_frame(N.build_notification_detailed("T", "B", count=7, seq=0))
    f = {k: v for k, _w, v in pb.parse(fr.payload)}
    assert f[1] == 1 and f[2] == 2 and f[3] == 6 and f[4] == 7 and f[5] == 0
    assert f[6] == b"T" and f[7] == b"B"


# ---------------------------------------------------------------- notification-enable (0x04+0x03)
# Fix for "notify sends but the watch ignores it": the vendor app runs a 0x04 feature bitmap +
# 0x03 profile/toggles bundle before any 0x11 (capture-derived, notif-enable-finding.md). These
# assert our notify path now emits that enable exchange in order. (Display ALSO needs classic-BT
# companion presence per notif-companion-verdict.md — out of scope for the LE frame ordering.)
class _FakeClient:
    def __init__(self):
        self.sent = []
        self._seq = 0

    def next_seq(self):
        self._seq += 1
        return self._seq

    async def send_raw(self, frame, response=False):
        self.sent.append(bytes(frame))

    async def disconnect(self):
        pass


def _ops(frames):
    return [framing.parse_frame(f, direction=framing.DIR_APP_TO_WATCH).opcode for f in frames]


def test_feature_bitmap_is_0x04_08011002():
    from starmax_client.commands.settings import build_feature_bitmap, OP_FEATURE_BITMAP
    fr = framing.parse_frame(build_feature_bitmap(), direction=framing.DIR_APP_TO_WATCH)
    assert fr.opcode == OP_FEATURE_BITMAP == 0x04
    assert fr.payload.hex() == "08011002"           # {f1:1, f2:2} — byte-exact vs the capture


def test_enable_notifications_sends_0x04_then_0x03():
    c = _FakeClient()
    asyncio.run(N.enable_notifications(c, delay=0))
    assert _ops(c.sent) == [0x04, 0x03]
    assert framing.parse_frame(c.sent[0], direction=framing.DIR_APP_TO_WATCH).payload.hex() == "08011002"


def test_cmd_notify_default_sends_only_0x11(monkeypatch):
    # Opt-in enable: plain `notify` (no --enable) sends ONLY the 0x11 — no 0x03 profile side-effect.
    from starmax_client import cli
    c = _FakeClient()

    async def _fake_conn(addr):
        return c

    async def _fake_addr(args):
        return "AA:BB:CC:DD:EE:FF"

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(cli, "_connect_and_bind", _fake_conn)
    monkeypatch.setattr(cli, "_resolve_address", _fake_addr)
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    ns = argparse.Namespace(title="hi", body=None, summary=False, enable=False,
                            address=None, name=None)
    rc = asyncio.run(cli.cmd_notify(ns))
    assert rc == 0
    assert _ops(c.sent) == [0x11]                    # default: no enable, no profile write


def test_cmd_notify_enable_flag_emits_full_exchange(monkeypatch):
    # --enable opts in to the 0x04+0x03 exchange before the 0x11.
    from starmax_client import cli
    c = _FakeClient()

    async def _fake_conn(addr):
        return c

    async def _fake_addr(args):
        return "AA:BB:CC:DD:EE:FF"

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(cli, "_connect_and_bind", _fake_conn)
    monkeypatch.setattr(cli, "_resolve_address", _fake_addr)
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    ns = argparse.Namespace(title="hi", body=None, summary=False, enable=True,
                            address=None, name=None)
    rc = asyncio.run(cli.cmd_notify(ns))
    assert rc == 0
    assert _ops(c.sent) == [0x04, 0x03, 0x11]        # --enable: exchange THEN the notification
