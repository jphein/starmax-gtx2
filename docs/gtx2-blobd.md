# gtx2-blobd — plain-HTTP blob serving for the nodes (#27)

The render-only host-bridge (its #20 permanent role: **render + serve**, no command plane) serves
rendered watch-face dial blobs over **plain HTTP** so the ESP32-C3 nodes fetch them without TLS.

## Why

A C3 node fetching a rendered face over **HTTPS** crashes when the blob is **>~15 KB**: the mbedtls
TLS working set (~40 KB) + the `http_request` body buffer + `push_dial_blob`'s copy + 2 live BLE
links exhaust the heap (it auto-recovers). That caps rich faces and blocks the grid-watts gauge.
Fetching over **plain `http://`** removes the ~40 KB TLS transient — the dominant cost — so the
safe blob ceiling rises well past 15 KB.

## Endpoints (`gtx2-bridge serve-blobs`, GET-only)

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | liveness → `200 ok` |
| `GET /face.bin?title=&body=&footer=&bg=&fg=&accent=&name=` | **render-on-fetch** — renders the face and returns the native dial `.bin` (`application/octet-stream`). Stateless/always-fresh; the dynamic value (e.g. grid watts) rides in the query. |
| `GET /blobs/<name>.bin` | **static** — serve a pre-rendered blob from `GTX2_BLOB_DIR` (basename-only, `.bin` required, path-traversal guarded). |

Config (env): `GTX2_BLOB_HOST` (`0.0.0.0`), `GTX2_BLOB_PORT` (`8088`), `GTX2_BLOB_DIR` (optional).

## Deploy (render lane — not auto-deployed)

Install the bridge's systemd unit (`gtx2-blobd.service`) → `~/.config/systemd/user/`, then
`systemctl --user enable --now gtx2-blobd`. Runs from the reused client venv. *(Adapt paths to
your setup.)*

## Node-side switch

Point the node's `http_request` at the bridge's plain-HTTP endpoint on the render host
(`<render-host>` = the machine running `serve-blobs`):

```
# before (crashes >~15 KB): https://<render-host>:8123/local/gtx2/<blob>.bin
# after:                    http://<render-host>:8088/face.bin?title=Front+Door&body=Motion&footer=19:07
#   or static:              http://<render-host>:8088/blobs/custom_id_25001.bin
```

Use the `http://` scheme (no TLS / no `verify_ssl`). Body → `push_dial_blob`.

**⚠️ Cross-subnet:** if the nodes and the render host sit on **different VLANs/subnets**, a node
fetching `<render-host>:8088` is an outbound cross-subnet flow that your router's inter-VLAN policy
may block. Binding `0.0.0.0` does **not** help if the render host has no interface on the node's
subnet — the fetch is inherently cross-subnet. See the firewall note below.

## Security / scope

- **LAN-only, plain HTTP, GET-only, no auth** — acceptable because blobs are rendered face pixels
  (no secrets/credentials). Path-traversal guarded; no directory listing.
- **Privacy:** the caller controls `title`/`body` — keep notification/gauge text non-PII.
- **Firewall (cross-subnet only):** if the render host and nodes are on different subnets, the
  fetch **requires** an allow rule on your router: permit **node-subnet → `<render-host>` tcp/8088**.
  Scope it tightly (that port + host only; drop the rest) so it doubles as hardening. Back up your
  firewall config before applying. The node confirms whether the flow is permitted the moment
  `:8088` is listening; if blocked, this is the exact rule to add.

## New safe ceiling

Removing the ~40 KB TLS working set clears the ~15 KB wall. The **exact** new ceiling is a
hardware measurement: fetch progressively larger blobs over `http://` on a node holding 2 watches
while watching free heap (controlnode, after the weather-fix calibration — don't compete for the
office node / ttyACM1 mid-calibration).
