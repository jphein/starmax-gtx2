"""Tests for the ZIP -> native-blob dial transcoder.

Skipped unless the ``transcode`` extra (Pillow + lz4) is importable. The committed vectors are
tiny + PII-free; a byte-level integration check against the real source ZIP runs only if that
(gitignored) file is present locally.
"""
from __future__ import annotations

import io
import json
import os
import zipfile

import pytest

pytest.importorskip("PIL")
pytest.importorskip("lz4.block")

from PIL import Image  # noqa: E402

from starmax_client import dialfmt  # noqa: E402
from starmax_client import dialtranscode as T  # noqa: E402
from starmax_client.commands import dials  # noqa: E402


# --------------------------------------------------------------------------- header field
@pytest.mark.parametrize("w,h", [(14, 40), (466, 466), (256, 256), (1, 1), (2047, 2047)])
def test_field_pack_unpack_roundtrip(w, h):
    assert T.unpack_field(T.pack_field(w, h)) == (w, h)


def test_field_matches_known_capture_value():
    # Number_0 (14x40) packed to 0x050038 in the real capture -> bytes 38 00 05
    assert T.pack_field(14, 40) == bytes.fromhex("380005")


def test_asset_type_for():
    assert T.asset_type_for("Number_0_8888.png") == T.ASSET_RGBA8888
    assert T.asset_type_for("BG_0565.bmp") == T.ASSET_RGB565
    with pytest.raises(T.TranscodeError):
        T.asset_type_for("weird.gif")


# --------------------------------------------------------------------------- image codec
def test_encode_decode_rgba_roundtrip():
    im = Image.new("RGBA", (5, 7), (10, 20, 30, 200))
    im.putpixel((0, 0), (255, 0, 0, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    asset = T.encode_image("X_8888.png", buf.getvalue())
    assert asset[0] == T.ASSET_RGBA8888
    atype, w, h, raw = T.decode_image(asset)
    assert (w, h) == (5, 7)
    assert raw == im.convert("RGBA").tobytes()


def test_encode_decode_rgb565_roundtrip():
    im = Image.new("RGB", (6, 4), (255, 128, 0))
    buf = io.BytesIO()
    im.save(buf, "BMP")
    asset = T.encode_image("Bg_0565.bmp", buf.getvalue())
    assert asset[0] == T.ASSET_RGB565
    atype, w, h, raw = T.decode_image(asset)
    assert (w, h) == (6, 4)
    assert raw == T._rgb565_le(im)
    assert len(raw) == 6 * 4 * 2


# --------------------------------------------------------------------------- full ZIP transcode
def _make_dial_zip(name="TEST_DIAL", with_app_preview=True, with_slot=False):
    prefix = "3/firmware/" if with_slot else "firmware/"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(prefix + "dial.json", json.dumps(
            {"name": name, "resolution_ratio": "466x466", "platform": "ats3085s", "item": []}))
        zf.writestr(prefix + "file.json", '{"item":[]}')
        b = io.BytesIO(); Image.new("RGB", (8, 8), (255, 0, 0)).save(b, "BMP")
        zf.writestr(prefix + "BG_0565.bmp", b.getvalue())
        b = io.BytesIO(); Image.new("RGBA", (4, 6), (0, 255, 0, 128)).save(b, "PNG")
        zf.writestr(prefix + "Number/Number_0_8888.png", b.getvalue())
        if with_app_preview:
            b = io.BytesIO(); Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(b, "PNG")
            zf.writestr(prefix + "app_preview.png", b.getvalue())
    return buf.getvalue()


def test_transcode_zip_produces_valid_container():
    blob = T.transcode_zip(_make_dial_zip())
    parsed = dialfmt.parse_blob(blob)               # valid native container (crc checked)
    assert parsed.name == "TEST_DIAL"
    assert parsed.asset_names == ["dial.json", "file.json", "BG_0565.bmp", "Number_0_8888.png"]
    assert "app_preview.png" not in parsed.asset_names          # phone-side asset dropped
    assert json.loads(parsed.get("dial.json"))["name"] == "TEST_DIAL"   # json verbatim
    # image assets decode back to the pixels we put in
    _, w, h, _ = T.decode_image(parsed.get("Number_0_8888.png"))
    assert (w, h) == (4, 6)


def test_transcode_flattens_slot_dir():
    blob = T.transcode_zip(_make_dial_zip(with_slot=True))
    parsed = dialfmt.parse_blob(blob)
    assert "Number_0_8888.png" in parsed.asset_names  # basename, slot/firmware prefix stripped


def test_transcode_rejects_non_zip():
    with pytest.raises(T.TranscodeError):
        T.transcode_zip(b"not a zip")


def test_load_dial_blob_transcodes_a_zip(tmp_path):
    p = tmp_path / "face.bin"
    p.write_bytes(_make_dial_zip())
    blob = dials.load_dial_blob(str(p))          # dial-push path accepts a ZIP now
    assert dialfmt.parse_blob(blob).name == "TEST_DIAL"


def test_transcoded_zip_is_pushable(tmp_path):
    p = tmp_path / "face.bin"
    p.write_bytes(_make_dial_zip())
    blob = dials.load_dial_blob(str(p))
    frames = dials.plan_dial_push(blob, 25001)
    assert frames[0][0] == 0xD3 and frames[-1][0] == 0xD4


# --------------------------------------------------------------------------- real-dial integration
REAL_ZIP = os.path.join(os.path.dirname(__file__), "..", "..",
                        "scratch", "full-impl", "cwr05g23687.bin")


@pytest.mark.skipif(not os.path.isfile(REAL_ZIP), reason="source dial ZIP not present")
def test_transcode_real_dial_all_assets_decode_to_source():
    zb = open(REAL_ZIP, "rb").read()
    blob = T.transcode_zip(zb)
    parsed = dialfmt.parse_blob(blob)
    assert parsed.name == "CWR05G_23687" and len(parsed.assets) == 24
    assert "app_preview.png" not in parsed.asset_names
    zf = zipfile.ZipFile(io.BytesIO(zb))
    src = {n.split("/")[-1]: zf.read(n) for n in zf.namelist() if n.split("/")[-1]}
    for a in parsed.assets:
        if a.name.endswith(".json"):
            assert a.data == src[a.name]
            continue
        atype, w, h, raw = T.decode_image(a.data)
        im = Image.open(io.BytesIO(src[a.name]))
        expected = im.convert("RGBA").tobytes() if atype == T.ASSET_RGBA8888 else T._rgb565_le(im)
        assert (w, h) == im.size and raw == expected, f"{a.name} pixels differ"
