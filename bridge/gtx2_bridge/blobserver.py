"""Plain-HTTP blob server for the render-only bridge (#20 render+serve role).

Nodes fetch rendered watch-face dial blobs over http:// (NOT https://) so the ESP32-C3 avoids the
~40 KB mbedtls TLS working-memory spike that crashes it on >~15 KB blobs. LAN-only, GET-only,
render-role — there is deliberately NO watch-command plane here (that is the demoted MQTT path).

Endpoints:
  GET /healthz                       -> 200 "ok"
  GET /face.bin?title=&body=&...     -> render a notification face on the fly -> native dial blob
  GET /gauge.bin?w=&max=&name=       -> render the grid-watts gauge (baked watts + live clock/date/
                                        heart/step/battery widgets) -> native dial blob
  GET /blobs/<name>.bin              -> serve a pre-rendered blob from the blob dir (static)
"""
from __future__ import annotations

import logging
import os
import posixpath
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import faces

log = logging.getLogger("gtx2_bridge.blobserver")


def _clamp(s, n):
    return (s or "")[:n]


class BlobHandler(BaseHTTPRequestHandler):
    server_version = "gtx2-blobd/1"
    blob_dir = None  # bound per-server in make_server

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, code, body=b"", ctype="application/octet-stream"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path == "/healthz":
                return self._send(200, b"ok", "text/plain")
            if u.path == "/face.bin":
                q = parse_qs(u.query)

                def g(k, d=""):
                    return _clamp(q.get(k, [d])[0], 240)

                if not g("title"):
                    return self._send(400, b"missing title", "text/plain")
                blob = faces.build_notification_blob(
                    title=g("title"), body=g("body"), footer=g("footer"),
                    bg=g("bg", "#000000"), fg=g("fg", "#FFFFFF"), accent=g("accent", "#00E5FF"),
                    name=_clamp(q.get("name", ["NOTIFY"])[0], 29))
                log.info("rendered face.bin: %d bytes (title=%r)", len(blob), g("title"))
                return self._send(200, blob)
            if u.path == "/gauge.bin":
                q = parse_qs(u.query)
                try:
                    watts = int(round(float(q.get("w", ["0"])[0])))       # signed; >=0 import, <0 export
                    max_w = int(round(float(q.get("max", ["12000"])[0])))  # JP-set 0-12kW full-scale
                except (TypeError, ValueError):
                    return self._send(400, b"w and max must be numeric", "text/plain")
                if max_w <= 0:
                    return self._send(400, b"max must be > 0", "text/plain")
                blob = faces.build_grid_face_blob(
                    watts, max_w=max_w, name=_clamp(q.get("name", ["GRIDWATTS"])[0], 29))
                log.info("rendered gauge.bin: %d bytes (w=%d max=%d)", len(blob), watts, max_w)
                return self._send(200, blob)
            if u.path.startswith("/blobs/"):
                return self._serve_static(u.path[len("/blobs/"):])
            return self._send(404, b"not found", "text/plain")
        except faces.FaceError as e:
            return self._send(400, str(e).encode(), "text/plain")
        except Exception:  # noqa: BLE001
            log.exception("blobserver error")
            return self._send(500, b"render error", "text/plain")

    do_HEAD = do_GET

    def _serve_static(self, name):
        if not self.blob_dir:
            return self._send(404, b"static disabled", "text/plain")
        safe = posixpath.basename(name)
        if safe != name or not safe.endswith(".bin") or safe.startswith("."):
            return self._send(400, b"bad name", "text/plain")
        full = os.path.join(self.blob_dir, safe)
        if not os.path.isfile(full):
            return self._send(404, b"not found", "text/plain")
        with open(full, "rb") as fh:
            return self._send(200, fh.read())


def make_server(host="0.0.0.0", port=8088, blob_dir=None):
    handler = type("BoundBlobHandler", (BlobHandler,), {"blob_dir": blob_dir})
    return ThreadingHTTPServer((host, port), handler)


def serve(host="0.0.0.0", port=8088, blob_dir=None):
    srv = make_server(host, port, blob_dir)
    log.info("gtx2-blobd: plain HTTP on %s:%d (blob_dir=%s)", host, port, blob_dir)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
