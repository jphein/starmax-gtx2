"""Regression guard for render.py's LZ4-vs-cramjam compression seam (HAOS musl fallback).

Empirical finding (2026-07-15, prompted during the render-corruption hunt): cramjam's
``compress_block(store_size=False)`` is byte-identical to ``lz4.block.compress(store_size=False)``
on the LARGE background asset, but the tiny 48x48 preview stream differs by a few bytes (different
end-of-block match choices — both valid LZ4). The invariant that matters on-watch and is pinned
here: **decoded pixels + asset headers are identical on both paths**, and the background asset is
byte-identical. Skips unless BOTH libs are importable (cramjam is a runtime-only dep on HAOS).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

lz4block = pytest.importorskip("lz4.block")
cramjam = pytest.importorskip("cramjam")

RENDER = Path(__file__).parent.parent / "custom_components" / "gtx2" / "render.py"
WATTS = [0, 14, 500, 1158, 5000, 11800, 20000]


def _load(name: str, *, block_lz4: bool):
    if block_lz4:
        sys.modules["lz4"] = None
        sys.modules["lz4.block"] = None
    else:
        sys.modules.pop("lz4", None)
        sys.modules.pop("lz4.block", None)
    spec = importlib.util.spec_from_file_location(name, RENDER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def paths():
    r_lz4 = _load("render_seam_lz4", block_lz4=False)
    r_cj = _load("render_seam_cj", block_lz4=True)
    sys.modules.pop("lz4", None)
    sys.modules.pop("lz4.block", None)
    return r_lz4, r_cj


def test_background_block_byte_identical(paths):
    r_lz4, r_cj = paths
    for w in WATTS:
        raw = r_lz4._rgb565_le(r_lz4.render_grid_static(w))
        assert lz4block.compress(raw, store_size=False) == bytes(
            cramjam.lz4.compress_block(raw, store_size=False)
        ), f"bg block diverged at {w} W"


def test_decoded_containers_identical(paths):
    sys.path.insert(0, str(Path(__file__).parent.parent / "client"))
    from starmax_client import dialfmt, dialtranscode

    r_lz4, r_cj = paths
    for w in WATTS:
        a_assets = dialfmt.parse_blob(r_lz4.build_grid_static_blob(w)).assets
        b_assets = dialfmt.parse_blob(r_cj.build_grid_static_blob(w)).assets
        assert [a.name for a in a_assets] == [b.name for b in b_assets]
        for a, b in zip(a_assets, b_assets):
            if a.data == b.data:
                continue
            assert a.name == "preview_0565.bmp", (
                f"only the preview stream may byte-differ; {a.name} diverged at {w} W")
            assert dialtranscode.decode_image(a.data) == dialtranscode.decode_image(b.data), (
                f"preview decoded pixels diverged at {w} W")
