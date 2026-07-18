"""Bridge configuration + MQTT topic layout.

Everything is env-driven (12-factor) so the systemd unit / container on the BLE host carries
no secrets in the repo. Nothing here embeds a real MAC or broker credential — the default MAC
is the synthetic placeholder ``AA:BB:CC:DD:EE:FF`` and ``serve`` refuses to act live against it.

Topic model (multi-watch)
-------------------------
Global (bridge-wide):
  * ``<root>/cmd``                 HA → bridge : one JSON command  {command, mac, params, confirm, dry_run}
  * ``<root>/result``              bridge → HA : JSON result of the last command (retained)
  * ``<root>/registry``            bridge → HA : JSON list of every detected watch (retained)
  * ``<root>/bridge/availability`` bridge → HA : online/offline (LWT)

Per watch (keyed by MAC slug):
  * ``<root>/<slug>/state``        bridge → HA : {connected, firmware, active_dial, …} (retained)
  * ``<root>/<slug>/health``       bridge → HA : {heart_rate, spo2, steps, …} (retained)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict

# Synthetic placeholder — NEVER a real device MAC. Live sends refuse this value.
PLACEHOLDER_MAC = "AA:BB:CC:DD:EE:FF"
DEFAULT_DIAL_ID = 25001            # custom-dial id space (the vendor SDK opcode map, 5001..25000)
DEFAULT_CITY = "Anytown"           # synthetic weather label (privacy guard; base.Weather default)
DEFAULT_ROOT = "gtx2"


def mac_slug(mac: str) -> str:
    """``AA:BB:CC:12:34:56`` -> ``aabbcc123456`` — a topic-safe id (lowercase, no separators)."""
    return "".join(c for c in mac.lower() if c.isalnum())


@dataclass
class BridgeConfig:
    """Resolved bridge settings. Build one with :meth:`from_env`."""

    mac: str = PLACEHOLDER_MAC
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_pass: str = ""
    mqtt_tls: bool = False
    topic_root: str = DEFAULT_ROOT
    dial_id: int = DEFAULT_DIAL_ID
    default_city: str = DEFAULT_CITY
    scan_interval: float = 60.0        # seconds between registry scans
    client_id: str = "gtx2-bridge"
    # plain-HTTP blob server (render role, `serve-blobs`): nodes fetch face blobs over http:// to
    # dodge the ~40 KB TLS spike that crashes the C3 on >~15 KB blobs. LAN-only.
    blob_host: str = "0.0.0.0"
    blob_port: int = 8088
    blob_dir: str = ""                 # optional; enables the static /blobs/<name>.bin route

    # ------------------------------------------------------------------ topics
    @property
    def topics(self) -> Dict[str, str]:
        """Global (bridge-wide) topics."""
        r = self.topic_root
        return {
            "cmd": f"{r}/cmd",
            "result": f"{r}/result",
            "registry": f"{r}/registry",
            "availability": f"{r}/bridge/availability",
        }

    def watch_topics(self, slug: str) -> Dict[str, str]:
        """Per-watch topics for a given MAC slug."""
        r = self.topic_root
        return {"state": f"{r}/{slug}/state", "health": f"{r}/{slug}/health"}

    def uses_placeholder_mac(self) -> bool:
        return mac_slug(self.mac) == mac_slug(PLACEHOLDER_MAC)

    # ------------------------------------------------------------------ env
    @classmethod
    def from_env(cls, env=None) -> "BridgeConfig":
        e = os.environ if env is None else env

        def _bool(name: str, default: bool) -> bool:
            v = e.get(name)
            return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")

        return cls(
            mac=e.get("GTX2_MAC", PLACEHOLDER_MAC),
            mqtt_host=e.get("GTX2_MQTT_HOST", "localhost"),
            mqtt_port=int(e.get("GTX2_MQTT_PORT", "1883")),
            mqtt_user=e.get("GTX2_MQTT_USER", ""),
            mqtt_pass=e.get("GTX2_MQTT_PASS", ""),
            mqtt_tls=_bool("GTX2_MQTT_TLS", False),
            topic_root=e.get("GTX2_TOPIC_ROOT", DEFAULT_ROOT),
            dial_id=int(e.get("GTX2_DIAL_ID", str(DEFAULT_DIAL_ID))),
            default_city=e.get("GTX2_DEFAULT_CITY", DEFAULT_CITY),
            scan_interval=float(e.get("GTX2_SCAN_INTERVAL", "60")),
            client_id=e.get("GTX2_CLIENT_ID", "gtx2-bridge"),
            blob_host=e.get("GTX2_BLOB_HOST", "0.0.0.0"),
            blob_port=int(e.get("GTX2_BLOB_PORT", "8088")),
            blob_dir=e.get("GTX2_BLOB_DIR", ""),
        )
