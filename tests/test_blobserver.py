"""Plain-HTTP blob server (render-only bridge role, #20). Offline — real localhost socket, no BLE."""
import os
import threading
import urllib.error
import urllib.request

import pytest

from gtx2_bridge import blobserver
from starmax_client import dialfmt


@pytest.fixture
def server(tmp_path):
    srv = blobserver.make_server("127.0.0.1", 0, blob_dir=str(tmp_path))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", str(tmp_path)
    finally:
        srv.shutdown()
        srv.server_close()


def _get(url):
    return urllib.request.urlopen(url, timeout=5)


def test_healthz(server):
    base, _ = server
    assert _get(f"{base}/healthz").read() == b"ok"


def test_face_bin_renders_valid_blob(server):
    base, _ = server
    r = _get(f"{base}/face.bin?title=Grid&body=1.8kW+export&footer=19:07")
    blob = r.read()
    assert r.headers["Content-Type"] == "application/octet-stream"
    dialfmt.parse_blob(blob)  # valid native dial container the watch installs
    assert 4000 < len(blob) < 60000


def test_face_requires_title(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(f"{base}/face.bin")
    assert e.value.code == 400


def test_static_round_trip(server):
    base, tmp = server
    blob = _get(f"{base}/face.bin?title=Hi").read()
    with open(os.path.join(tmp, "custom_id_25001.bin"), "wb") as fh:
        fh.write(blob)
    assert _get(f"{base}/blobs/custom_id_25001.bin").read() == blob


@pytest.mark.parametrize("bad", ["/blobs/../etc/passwd", "/blobs/x.txt", "/blobs/.hidden.bin"])
def test_path_and_type_guard(server, bad):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(f"{base}{bad}")
    assert e.value.code in (400, 404)


def test_unknown_path_404(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(f"{base}/nope")
    assert e.value.code == 404


def test_gauge_bin_renders_valid_blob(server):
    base, _ = server
    r = _get(f"{base}/gauge.bin?w=1185&max=6000")
    blob = r.read()
    assert r.headers["Content-Type"] == "application/octet-stream"
    parsed = dialfmt.parse_blob(blob)          # valid native dial container
    assert parsed.name == "GRIDWATTS"


def test_gauge_bin_defaults_and_signed(server):
    base, _ = server
    dialfmt.parse_blob(_get(f"{base}/gauge.bin?w=-3200").read())   # export, default max, valid


@pytest.mark.parametrize("bad", ["w=abc", "w=1&max=0", "w=1&max=nope"])
def test_gauge_bin_bad_params_400(server, bad):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(f"{base}/gauge.bin?{bad}")
    assert e.value.code == 400
