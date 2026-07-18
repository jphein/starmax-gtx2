"""The notification-face renderer must emit a VALID, MINIMAL native dial blob."""
from __future__ import annotations

import pytest

from starmax_client import dialfmt, dialtranscode

from gtx2_bridge import faces


def test_build_notification_blob_parses_as_a_valid_dial():
    blob = faces.build_notification_blob("Front Door", "Motion detected", footer="14:32")
    parsed = dialfmt.parse_blob(blob)                 # raises DialFormatError if malformed
    assert parsed.name == "NOTIFY"
    # minimal asset set: manifest + file table + background + preview (no live glyph fonts)
    assert parsed.asset_names == ["dial.json", "file.json", "BG_0565.bmp", "preview_0565.bmp"]


def test_background_is_rgb565_and_full_screen():
    blob = faces.build_notification_blob("Hi")
    parsed = dialfmt.parse_blob(blob)
    atype, w, h, raw = dialtranscode.decode_image(parsed.get("BG_0565.bmp"))
    assert atype == dialtranscode.ASSET_RGB565     # 2 B/px, not RGBA — the small-blob choice
    assert (w, h) == (faces.W, faces.H) == (466, 466)
    assert len(raw) == 466 * 466 * 2               # decodes back to full RGB565


def test_blob_is_small_relative_to_a_full_dial():
    """A solid-bg notification (LZ4-crushed) must be far smaller than a ~231 KB stock dial."""
    blob = faces.build_notification_blob("Alarm", "Garage door left open for 20 minutes")
    assert len(blob) < 60_000, f"blob {len(blob)}B is not minimal — push would be slow"


def test_manifest_is_valid_json_and_names_the_background():
    import json
    blob = faces.build_notification_blob("Hi", name="TESTFACE")
    parsed = dialfmt.parse_blob(blob)
    manifest = json.loads(parsed.get("dial.json"))
    assert manifest["name"] == "TESTFACE"
    assert manifest["resolution_ratio"] == "466x466" and manifest["platform"] == "ats3085s"
    bg = manifest["item"][0]
    assert bg["type"] == "background" and bg["picture"] == "BG_0565.bmp"


def test_long_body_wraps_without_error():
    body = "This is a very long notification body that must wrap across multiple lines " * 3
    blob = faces.build_notification_blob("Long", body)
    assert dialfmt.parse_blob(blob).name == "NOTIFY"


def test_dial_name_is_sanitised_to_ascii_and_length():
    blob = faces.build_notification_blob("Hi", name="a-name-with-émojis-🚨-and-way-too-many-chars")
    parsed = dialfmt.parse_blob(blob)
    assert parsed.name.isascii() and 0 < len(parsed.name) <= 29


def test_missing_icon_raises_faceerror():
    with pytest.raises(faces.FaceError):
        faces.build_notification_blob("Hi", icon="/no/such/icon.png")


def test_bad_colour_raises_faceerror():
    with pytest.raises(faces.FaceError):
        faces.build_notification_blob("Hi", bg="not-a-colour")


# --------------------------------------------------------------------------- grid-watts gauge face
def test_grid_face_parses_with_live_widgets():
    import json
    blob = faces.build_grid_face_blob(1185, max_w=6000)
    parsed = dialfmt.parse_blob(blob, verify_crc=True)   # == the watch's header CRC-32 check
    dj = json.loads(parsed.get("dial.json"))
    bound = {(it["widget"], it["type"]) for it in dj["item"]}
    # baked bg + the five live native bindings the showcase face overlays
    assert ("icon", "background") in bound
    for t in ("hour", "min", "date", "heart", "step", "battery"):
        assert ("text", t) in bound, f"missing live widget {t}"


def test_grid_face_shares_one_stat_font():
    """heart/step/battery share the 'Stat' glyph font (leanness) — not three folders."""
    import json
    blob = faces.build_grid_face_blob(1185)
    fonts = {e["name"] for e in json.loads(dialfmt.parse_blob(blob).get("file.json"))["item"]}
    assert fonts == {"Clock", "Date", "Stat"}, fonts   # exactly three digit fonts


@pytest.mark.parametrize("watts,expect_col", [
    (1185, faces._GRID_IMPORT),    # normal draw -> import colour
    (-3200, faces._GRID_IMPORT),   # magnitude-only v1: sign ignored on the face -> still import colour
    (5, faces._GRID_IDLE),         # ~zero -> neutral idle
])
def test_grid_gauge_magnitude_only_colour(watts, expect_col):
    """v1 is magnitude-only: |watts| picks import vs idle; the sign never selects export (no green)."""
    img = faces.render_grid_gauge(watts, max_w=12000).convert("RGB")
    cols = {c for _, c in img.getcolors(maxcolors=100000)}
    assert expect_col in cols
    assert faces._GRID_EXPORT not in cols   # export colour never rendered in v1


# ---------------------------------------------------------------- flat STATIC grid face
@pytest.mark.parametrize("watts", [0, 8, 934, 1158, 2500, 6000, 11800, 15000])
def test_grid_static_blob_valid_and_compact(watts):
    """Valid container + compact. NB: at the HW-safe cap=512 the face is ~9-10 KB — OVER the 8 KB
    /local/ ceiling, so it ships via the chunked D-plane path (Track B). We only assert it stays
    well under the ~35 KB live-face range (a static face has no font folders)."""
    blob = faces.build_grid_static_blob(watts)
    assert dialfmt.parse_blob(blob).name == "GRIDWATTS"    # valid native container
    assert len(blob) <= 14000, f"{watts} W -> {len(blob)} B is unexpectedly large for a static face"


def test_grid_static_blob_is_bg_only_no_live_widgets():
    """Static face: no glyph-font folders / live-widget items (that's the byte lever + the reason
    it fits < 8 KB). Values are baked; 'live' is a re-push, not a native binding."""
    import json
    parsed = dialfmt.parse_blob(faces.build_grid_static_blob(1158))
    assert json.loads(parsed.get("file.json"))["item"] == []          # zero font folders
    items = json.loads(parsed.get("dial.json"))["item"]
    assert [it.get("type") for it in items] == ["background"]          # bg-only


def test_grid_static_blob_is_deterministic():
    """Watts-only (no clock/time/random) → pure watts-in/bytes-out, so the VM-local component can
    cache/executor it safely and re-push only on value change."""
    assert faces.build_grid_static_blob(1158) == faces.build_grid_static_blob(1158)


def _max_lz4_match(lz4_block: bytes) -> int:
    """Longest match length in a raw LZ4 block (for the HW match-cap invariant)."""
    i, n, mm = 0, len(lz4_block), 0
    while i < n:
        tok = lz4_block[i]; i += 1
        ll = tok >> 4
        if ll == 15:
            while lz4_block[i] == 255:
                ll += 255; i += 1
            ll += lz4_block[i]; i += 1
        i += ll
        if i >= n:
            break
        i += 2
        ml = tok & 0xf
        if ml == 15:
            while lz4_block[i] == 255:
                ml += 255; i += 1
            ml += lz4_block[i]; i += 1
        mm = max(mm, ml + 4)
    return mm


@pytest.mark.parametrize("watts", [0, 934, 1838, 6000, 11800, 15000])
def test_grid_static_blob_caps_lz4_match_length(watts):
    """HW-CRITICAL: the watch's minimal LZ4 decoder garbles matches longer than
    GRID_STATIC_MAX_MATCH — a near-solid face makes ~178 KB matches uncapped. Every image asset's
    LZ4 must stay <= the cap (proven clean on hardware at cap=2048, 2026-07-15)."""
    parsed = dialfmt.parse_blob(faces.build_grid_static_blob(watts))
    for asset_name in ("BG_0565.bmp", "preview_0565.bmp"):
        a = parsed.get(asset_name)
        a = a.encode() if isinstance(a, str) else a
        assert _max_lz4_match(a[4:]) <= faces.GRID_STATIC_MAX_MATCH, \
            f"{watts} W {asset_name}: match > cap → will render garbled on the watch"


def test_grid_static_blob_pixels_survive_the_cap():
    """The match-cap re-encode must be LOSSLESS: the capped BG asset decodes to the exact rendered
    pixels (cap only changes the compression structure, never the image)."""
    parsed = dialfmt.parse_blob(faces.build_grid_static_blob(1838))
    a = parsed.get("BG_0565.bmp"); a = a.encode() if isinstance(a, str) else a
    atype, w, h, raw = dialtranscode.decode_image(a)
    src = dialtranscode.decode_image(
        dialtranscode.encode_image("BG_0565.bmp", faces._bmp_bytes(faces.render_grid_static(1838))))
    assert (atype, w, h, raw) == src        # capped == uncapped after decode → pixel-identical


def test_cap_lz4_matches_is_lossless_and_bounded():
    """cap_lz4_matches on an arbitrary LZ4 block: decodes byte-identically + honours the cap."""
    import lz4.block
    raw = (bytes([5, 7, 10]) * 60000)                        # a long near-flat run
    block = lz4.block.compress(raw, store_size=False)
    capped = faces.cap_lz4_matches(block, 512)
    assert lz4.block.decompress(capped, uncompressed_size=len(raw)) == raw
    assert _max_lz4_match(capped) <= 512


# ---------------------------------------------------------------- LIVE-kW grid face (set_time driven)
def test_grid_live_blob_valid_capped_and_has_live_widgets():
    """The live-kW container is valid + capped + carries the live widgets: real HH:MM clock (Clk) +
    the integer-kW hero on the `day` RTC field (Kw) + a small HR BPM on `heart` (its own small `Hr`
    font) + the battery %% arc. No tenths/`second` widget (JP integer design). Ships via Track B, so —
    unlike the static face — it CAN carry glyph fonts."""
    import json
    blob = faces.build_grid_live_blob()
    parsed = dialfmt.parse_blob(blob, verify_crc=True)         # == the watch's header CRC-32 check
    assert parsed.name == "GRIDKW"
    dj = json.loads(parsed.get("dial.json"))
    bound = {(it["widget"], it["type"]) for it in dj["item"]}
    assert ("icon", "background") in bound
    for t in ("hour", "min", "day", "heart"):                 # HH:MM clock + integer-kW hero on `day` + HR BPM
        assert ("text", t) in bound, f"missing live widget {t}"
    assert ("text", "second") not in bound                    # tenths widget dropped (integer kW)
    assert ("text", "date") not in bound                      # NOT `date` (compound field garbles numbers)
    assert ("arc", "battery") in bound                        # battery %% edge-ring (sensor-driven, no push)
    fonts = {e["name"] for e in json.loads(parsed.get("file.json"))["item"]}
    assert fonts == {"Clk", "Hr", "Kw"}, fonts                # clock (white) + kW (red) + HR (small white)
    # every image asset (bg + the ~30 glyph PNGs across the three digit fonts) must honour the HW match cap
    for asset_name in parsed.asset_names:
        if asset_name.endswith((".bmp", ".png")):
            a = parsed.get(asset_name); a = a.encode() if isinstance(a, str) else a
            assert _max_lz4_match(a[4:]) <= faces.GRID_STATIC_MAX_MATCH, \
                f"{asset_name}: match > cap → renders garbled on the watch"
    assert len(blob) <= 45_000, f"{len(blob)} B is unexpectedly large for the live-kW face"


def test_grid_live_blob_is_deterministic_and_value_independent():
    """The face art is built ONCE and updated via set_time (not re-pushed), so the blob must be pure
    (no now()/watts baked in) → identical bytes across builds. This is what lets the value change
    with a tiny set_time frame instead of a ~33 KB re-push."""
    assert faces.build_grid_live_blob() == faces.build_grid_live_blob()


def test_grid_live_capped_bg_is_pixel_lossless():
    """The post-process cap (_cap_blob_assets, applied to the FINISHED build_dial_face container)
    must be lossless: the capped background decodes to the exact rendered pixels."""
    parsed = dialfmt.parse_blob(faces.build_grid_live_blob())
    a = parsed.get("BG_0565.bmp"); a = a.encode() if isinstance(a, str) else a
    src = dialtranscode.decode_image(
        dialtranscode.encode_image("BG_0565.bmp", faces._bmp_bytes(faces.render_grid_live())))
    assert dialtranscode.decode_image(a) == src               # capped == uncapped after decode


def test_grid_live_compact_blob_single_font_capped_and_small():
    """The COMPACT lander: all four widgets share ONE white glyph font (14 assets, minclock-class),
    which ~halves the container vs the full two-font face — the guaranteed-installable artifact when
    free-flash is the ceiling. Must stay valid + capped + a strict reduction of the full face."""
    import json
    blob = faces.build_grid_live_compact_blob()
    parsed = dialfmt.parse_blob(blob, verify_crc=True)
    assert parsed.name == "GRIDKW"
    fonts = {e["name"] for e in json.loads(parsed.get("file.json"))["item"]}
    assert fonts == {"Clk"}, fonts                                    # ONE shared font (the byte lever)
    bound = {(it["widget"], it["type"]) for it in json.loads(parsed.get("dial.json"))["item"]}
    assert ("icon", "background") in bound
    for t in ("hour", "min", "date", "second"):                      # same live bindings as the full face
        assert ("text", t) in bound, f"missing live widget {t}"
    assert len(parsed.asset_names) == 14                             # bg + 10 glyphs + dial/file.json + preview
    for asset_name in parsed.asset_names:
        if asset_name.endswith((".bmp", ".png")):
            a = parsed.get(asset_name); a = a.encode() if isinstance(a, str) else a
            assert _max_lz4_match(a[4:]) <= faces.GRID_STATIC_MAX_MATCH, \
                f"{asset_name}: match > cap → renders garbled on the watch"
    assert len(blob) <= 16_500, f"{len(blob)} B — compact lander should be ~14-16 KB"
    assert len(blob) < len(faces.build_grid_live_blob())             # strictly smaller than the full face


@pytest.mark.parametrize("watts,day", [
    (300, 1),           # <500 W: int(0.8)=0 → floored to 1 (RTC day can't be 0)
    (500, 1),           # int(0.5+0.5)=int(1.0)=1
    (0, 1),             # zero → floored to 1
    (600, 1),           # int(1.1)=1
    (1146, 1),          # the on-glass value: int(1.646)=1 → "1 kW" (matches JP's spare readout)
    (1500, 2),          # half-up: int(1.5+0.5)=int(2.0)=2
    (1838, 2),          # int(2.338)=2 (integer — no more tenths)
    (2500, 3),          # HALF-UP: int(2.5+0.5)=int(3.0)=3 (NOT banker's 2 — the cross-impl fix)
    (6000, 6),
    (11800, 12),        # int(12.3)=12 (2-digit hero)
    (-3200, 3),         # magnitude-only: sign ignored → int(3.7)=3
])
def test_grid_live_encode_maps_watts_to_set_time(watts, day):
    """encode_grid_live is the CANONICAL watts->set_time spec the HA automation feed
    both mirror: day = max(1, int(kW + 0.5)) INTEGER HALF-UP (NOT Python round/banker's), second = 0
    (unused), hour/minute = the real clock."""
    import datetime
    now = datetime.datetime(2026, 7, 16, 21, 33)
    e = faces.encode_grid_live(watts, now=now)
    assert e["day"] == day
    assert e["second"] == 0                                   # tenths field freed → always 0
    assert (e["hour"], e["minute"]) == (21, 33)               # real clock preserved


# ---------------------------------------------------------------- diagnostic binding-map face (#28)
def test_binding_map_blob_exercises_every_binding():
    """The diagnostic face (task #28) carries EVERY probed binding as a labelled live text widget, so
    a HW push + distinct set_time values reveals which widget = which field. Shares colour-fonts (4
    folders, not 17) + is a valid capped container."""
    import json
    blob = faces.build_binding_map_blob()
    parsed = dialfmt.parse_blob(blob, verify_crc=True)
    bound = {it["type"] for it in json.loads(parsed.get("dial.json"))["item"] if it["widget"] == "text"}
    expected = {"hour", "min", "second", "date", "day", "month", "week", "hourhi", "hourlo",
                "minhi", "minlo", "step", "heart", "distance", "calorie", "battery", "steplist"}
    assert expected <= bound, f"missing bindings: {expected - bound}"     # all 17 exercised
    fonts = {e["name"] for e in json.loads(parsed.get("file.json"))["item"]}
    assert fonts == {"Clk", "Cy", "Am", "Gr"}, fonts                      # shared colour-fonts, not 17
    for asset_name in parsed.asset_names:                                 # HW match cap on every asset
        if asset_name.endswith((".bmp", ".png")):
            a = parsed.get(asset_name); a = a.encode() if isinstance(a, str) else a
            assert _max_lz4_match(a[4:]) <= faces.GRID_STATIC_MAX_MATCH, f"{asset_name}: match > cap"
    assert len(blob) <= 45_000, f"{len(blob)} B is unexpectedly large for the debug face"
