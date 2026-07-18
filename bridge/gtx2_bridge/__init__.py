"""gtx2_bridge — a Home Assistant ⇄ Starmax GTX2 bridge (stock-firmware, [CAP] pushes only).

Exposes four HA-driven actions against the watch over BLE, all built on the verified
``starmax_client`` library (no custom firmware, no teardown):

  1. **buzz**   — ring/find the watch           (``find`` 0x18, [CAP])
  2. **notify** — render arbitrary text onto a minimal custom watch-face and push it
                  (``dial-push`` D-plane, live-validated) — sidesteps the classic-BT
                  notification wall
  3. **time**   — sync the watch clock          (``set-time`` 0x02, [CAP])
  4. **weather**— push weather from an HA entity (``weather`` 0x12 + 0x04 enable, [CAP])

This host-side bridge is the WORKING PROOF (range-limited to the BLE host). The per-room
ESPHome-C3 productionization is a separate track (see README).
"""
from __future__ import annotations

from . import _paths

_paths.ensure_starmax_on_path()

__version__ = "0.1.0"
