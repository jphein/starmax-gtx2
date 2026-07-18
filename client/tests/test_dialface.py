"""Tests for the live-face builder (:mod:`starmax_client.dialface`).

Offline, no BLE. Skipped unless the ``transcode`` extra (Pillow + lz4) is importable. Follows the
byte-parity discipline of the dial-push tests: a built container must parse + pass the header
CRC-32 the watch verifies, and must round-trip byte-identically (``rebuild == build``). Glyph
pixels come from the host font, so we assert *container-assembly* parity + build determinism, not
a pinned cross-machine byte constant.
"""
from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("PIL")
pytest.importorskip("lz4.block")

from PIL import Image  # noqa: E402

from starmax_client import dialface, dialfmt  # noqa: E402
from starmax_client.commands import dials  # noqa: E402
from starmax_client.protobuf import ProtobufWriter  # noqa: E402


def _bg(color=(20, 24, 40)):
    b = io.BytesIO()
    Image.new("RGB", (466, 466), color).save(b, "PNG")
    return b.getvalue()


# demo-shaped face: custom bg + analog hands + date text + step arc + step text
_DEMO_WIDGETS = [
    {"type": "hour", "widget": "pointer"},
    {"type": "min", "widget": "pointer"},
    {"type": "date", "widget": "text", "x": 210, "y": 70, "w": 60, "h": 34, "color": "#8FB7FF"},
    {"type": "step", "widget": "arc", "x": 169, "y": 300, "w": 128, "h": 128,
     "start_angle": 225, "end_angle": 135, "fgcolor": "#00E5FF", "color": "#00E5FF", "arc_width": 8},
    {"type": "step", "widget": "text", "x": 198, "y": 350, "w": 90, "h": 34, "color": "#00E5FF"},
]


# --------------------------------------------------------------------------- validation
def test_validate_accepts_demo():
    dialface.validate_widgets(_DEMO_WIDGETS)  # no raise


@pytest.mark.parametrize("bad,match", [
    ({"type": "nope", "widget": "text", "x": 0, "y": 0, "w": 10, "h": 10}, "unknown type"),
    ({"type": "hour", "widget": "blink", "x": 0, "y": 0, "w": 10, "h": 10}, "unknown widget"),
    ({"type": "date", "widget": "text", "x": 500, "y": 0, "w": 10, "h": 10}, "out of bounds"),
    ({"type": "date", "widget": "text", "x": 0, "y": 0, "w": 999, "h": 10}, "out of bounds"),
    ({"type": "date", "widget": "text", "x": 0, "y": 0, "w": 10}, "missing geometry"),
    ({"type": "date", "widget": "text", "x": 0, "y": 0, "w": 10, "h": 10, "color": "red"}, "RRGGBB"),
    ({"type": "other", "widget": "icon", "x": 0, "y": 0, "w": 10, "h": 10}, "requires 'picture'"),
    ({"type": "week", "x": 0, "y": 0, "w": 10, "h": 10}, "ambiguous"),
])
def test_validate_rejects(bad, match):
    with pytest.raises(dialface.DialFaceError, match=match):
        dialface.validate_widgets([bad])


def test_validate_rejects_empty():
    with pytest.raises(dialface.DialFaceError):
        dialface.validate_widgets([])


# --------------------------------------------------------------------------- build -> container
def test_build_produces_valid_container():
    blob = dialface.build_dial_face(_bg(), _DEMO_WIDGETS, name="LucidLive")
    parsed = dialfmt.parse_blob(blob, verify_crc=True)   # == the watch's header CRC-32 check
    assert parsed.name == "LucidLive"
    assert "dial.json" in parsed.asset_names and "file.json" in parsed.asset_names
    assert "BG_0565.bmp" in parsed.asset_names
    # analog hand sprites + a digit font were emitted
    assert "hour_hand_8888.png" in parsed.asset_names
    assert "min_hand_8888.png" in parsed.asset_names
    assert any(n.startswith("Date_") and n.endswith("_8888.png") for n in parsed.asset_names)
    assert any(n.startswith("Step_") and n.endswith("_8888.png") for n in parsed.asset_names)


def test_build_dial_json_declares_live_widgets():
    blob = dialface.build_dial_face(_bg(), _DEMO_WIDGETS, name="LucidLive")
    dj = json.loads(dialfmt.parse_blob(blob).get("dial.json"))
    items = dj["item"]
    assert items[0] == {"widget": "icon", "type": "background", "x": 0, "y": 0,
                        "w": 466, "h": 466, "picture": "BG_0565.bmp"}
    by = {(it["widget"], it["type"]) for it in items}
    assert ("pointer", "hour") in by and ("pointer", "min") in by
    assert ("text", "date") in by and ("arc", "step") in by and ("text", "step") in by
    # pointer pivot convention: pivot lands at canvas centre (233,233)  [CAP]-derived
    hour = next(it for it in items if it["widget"] == "pointer" and it["type"] == "hour")
    assert hour["x"] + hour["centerx"] == 233 and hour["y"] + hour["centery"] == 233
    # file.json enumerates the rendered fonts
    fj = json.loads(dialfmt.parse_blob(blob).get("file.json"))
    fonts = {e["name"] for e in fj["item"]}
    assert {"Date", "Step"} <= fonts


def test_build_is_byte_parity_and_deterministic():
    """Byte-parity discipline: build -> parse -> rebuild is byte-identical, and two builds of the
    same spec are identical (container assembly is deterministic; independent of glyph pixels)."""
    a = dialface.build_dial_face(_bg(), _DEMO_WIDGETS, name="LucidLive")
    b = dialface.build_dial_face(_bg(), _DEMO_WIDGETS, name="LucidLive")
    assert a == b, "build is not deterministic"
    assert dialfmt.rebuild_blob(dialfmt.parse_blob(a)) == a, "container is not byte-stable"


def test_build_header_matches_proven_install_opaque_region():
    """The opaque header region [0x22:0x28] stays byte-identical to the proven CWR05G_23687
    install (const_a=06 04, const_b=00 04) — we don't synthesize unobserved wire bytes."""
    blob = dialface.build_dial_face(_bg(), _DEMO_WIDGETS, name="LucidLive")
    assert blob[0x22:0x24] == b"\x06\x04"     # const_a
    assert blob[0x26:0x28] == b"\x00\x04"     # const_b


def test_build_aod_populates_fade_item():
    blob = dialface.build_dial_face(
        _bg(), _DEMO_WIDGETS, name="LucidLive",
        aod=[{"type": "hour", "widget": "pointer"}, {"type": "min", "widget": "pointer"}])
    dj = json.loads(dialfmt.parse_blob(blob).get("dial.json"))
    assert dj["fade_item"], "fade_item should be populated when aod is given"
    assert {it["type"] for it in dj["fade_item"] if it["widget"] == "pointer"} == {"hour", "min"}


def test_build_rejects_bad_name():
    with pytest.raises(dialface.DialFaceError):
        dialface.build_dial_face(_bg(), _DEMO_WIDGETS, name="x" * 40)   # too long for the 30B field


def test_preview_size_shrinks_container():
    """A smaller preview_size yields a valid, strictly smaller container (the on-watch thumbnail
    is a real slice of the blob — matters on the memory-constrained node push path)."""
    big = dialface.build_dial_face(_bg(), _DEMO_WIDGETS, name="LucidLive")               # default 256
    small = dialface.build_dial_face(_bg(), _DEMO_WIDGETS, name="LucidLive", preview_size=64)
    assert len(small) < len(big)
    p = dialfmt.parse_blob(small, verify_crc=True)     # still passes the watch's header CRC-32
    assert "preview_0565.bmp" in p.asset_names


@pytest.mark.parametrize("bad", [0, 8, 999, 2.0, "128"])
def test_build_rejects_bad_preview_size(bad):
    with pytest.raises(dialface.DialFaceError, match="preview_size"):
        dialface.build_dial_face(_bg(), _DEMO_WIDGETS, name="LucidLive", preview_size=bad)


# --------------------------------------------------------------------------- push wrapper
class _FakeClient:
    def __init__(self, active="custom_id_25040.bin", mtu=244):
        self.sent, self.responses, self._mtu_payload, self._active = [], [], mtu, active

    async def send_raw(self, frame, response=False):
        self.sent.append(bytes(frame)); self.responses.append(response)

    async def request(self, frame, opcode):
        payload = (ProtobufWriter()
                   .message(10, ProtobufWriter().varint(1, 1).varint(2, 1)
                            .varint(3, len(self._active)).string(4, self._active).to_bytes())
                   .string(14, self._active).to_bytes())
        return SimpleNamespace(opcode=opcode, payload=payload)


def test_push_dial_face_builds_and_streams():
    client = _FakeClient(active="custom_id_25040.bin")
    result = asyncio.run(dials.push_dial_face(
        client, _bg(), _DEMO_WIDGETS, dial_id=25040, name="LucidLive"))
    assert client.sent[0][0] == 0xD3 and client.sent[1][0] == 0xD1 and client.sent[-1][0] == 0xD4
    assert any(f[0] == 0xD2 for f in client.sent[2:-1])
    assert all(client.responses), "D-plane frames must be write-with-response"
    assert result["confirmed"] is True and result["active_dial"] == "custom_id_25040.bin"
