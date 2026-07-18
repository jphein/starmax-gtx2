"""MQTT transport — the HA-native front door to the dispatcher + registry.

MQTT is the transport because it is HA-native (no custom integration), decouples HA from the
BLE host, and suits the multi-second dial-push (fire a command, read back a retained result).

Pure, unit-tested helpers (``decode_command``) are separated from the paho wiring so the routing
logic needs no broker. The paho client is imported lazily (only ``serve`` needs it); everything
else — CLI, tests — works without it.

Concurrency: one asyncio loop owns the BLE work; paho runs its network loop on its own thread and
hands inbound messages to the loop via ``run_coroutine_threadsafe``. A single ``asyncio.Lock``
serialises watch access (single-owner BLE).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from .config import BridgeConfig, mac_slug
from .dispatch import Dispatcher
from .registry import Registry, scan_watches

log = logging.getLogger("gtx2_bridge.mqtt")


def decode_command(raw: bytes) -> dict:
    """Parse a ``<root>/cmd`` payload into a normalised command spec.

    Accepts JSON ``{"command": …, "mac": …, "params": {…}, "confirm": bool, "dry_run": bool}``.
    A bare string payload (e.g. an HA button sending just ``"find"``) is treated as that command
    with no params. Returns ``{}`` (falsy ``command``) on unparseable input.
    """
    text = raw.decode("utf-8", "replace").strip() if isinstance(raw, (bytes, bytearray)) else str(raw)
    if not text:
        return {}
    if text[0] in "{[":
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return {}
        if not isinstance(obj, dict):
            return {}
        return {
            "command": obj.get("command"),
            "mac": obj.get("mac"),
            "params": obj.get("params") or {},
            "confirm": bool(obj.get("confirm", False)),
            "dry_run": bool(obj.get("dry_run", False)),
        }
    # bare command name
    return {"command": text, "mac": None, "params": {}, "confirm": False, "dry_run": False}


class MqttBridge:
    """Run the bridge as an MQTT service: subscribe to commands, publish registry + results."""

    def __init__(self, config: BridgeConfig, dispatcher: Optional[Dispatcher] = None,
                 registry: Optional[Registry] = None) -> None:
        self.config = config
        self.dispatcher = dispatcher or Dispatcher(config)
        self.registry = registry or Registry()
        self._mqtt = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ble_lock = asyncio.Lock()

    # ------------------------------------------------------------------ paho wiring
    def _make_client(self):
        import paho.mqtt.client as mqtt

        cli = mqtt.Client(client_id=self.config.client_id,
                          callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        if self.config.mqtt_user:
            cli.username_pw_set(self.config.mqtt_user, self.config.mqtt_pass)
        if self.config.mqtt_tls:
            cli.tls_set()
        cli.will_set(self.config.topics["availability"], "offline", retain=True)
        cli.on_connect = self._on_connect
        cli.on_message = self._on_message
        return cli

    def _on_connect(self, client, _userdata, _flags, reason_code, _props=None) -> None:
        log.info("MQTT connected (%s); subscribing %s", reason_code, self.config.topics["cmd"])
        client.subscribe(self.config.topics["cmd"])
        client.publish(self.config.topics["availability"], "online", retain=True)

    def _on_message(self, _client, _userdata, msg) -> None:
        spec = decode_command(msg.payload)
        if not spec.get("command"):
            log.warning("ignoring malformed command on %s: %r", msg.topic, msg.payload[:80])
            return
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._handle(spec), self._loop)

    # ------------------------------------------------------------------ handlers
    async def _handle(self, spec: dict) -> None:
        async with self._ble_lock:                      # single-owner BLE
            result = await self.dispatcher.handle(
                spec["command"], mac=spec.get("mac"), params=spec.get("params"),
                dry_run=spec.get("dry_run", False), confirm=spec.get("confirm", False))
        self._publish(self.config.topics["result"], result, retain=True)
        self._publish_watch_state(spec.get("mac") or self.config.mac, result)

    def _publish_watch_state(self, mac: str, result: dict) -> None:
        res = result.get("result") if isinstance(result, dict) else None
        if not isinstance(res, dict):
            return
        topics = self.config.watch_topics(mac_slug(mac))
        health = res.get("health")
        if isinstance(health, dict) and health:
            self._publish(topics["health"], health, retain=True)
        state = {k: v for k, v in res.items() if k != "health"}
        if state:
            self._publish(topics["state"], state, retain=True)

    async def _scan_once(self) -> None:
        try:
            found = await scan_watches(timeout=min(self.config.scan_interval, 8.0))
        except Exception as e:  # noqa: BLE001 - scanning must never kill the service
            log.warning("scan failed: %s", e)
            return
        self.registry.merge_scan(found)
        self._publish(self.config.topics["registry"], self.registry.as_payload(), retain=True)

    def _publish(self, topic: str, payload, retain: bool = False) -> None:
        if self._mqtt is None:
            return
        data = payload if isinstance(payload, str) else json.dumps(payload, default=str)
        self._mqtt.publish(topic, data, retain=retain)

    # ------------------------------------------------------------------ run
    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._mqtt = self._make_client()
        self._mqtt.connect(self.config.mqtt_host, self.config.mqtt_port)
        self._mqtt.loop_start()
        log.info("gtx2-bridge serving: broker %s:%d root %s",
                 self.config.mqtt_host, self.config.mqtt_port, self.config.topic_root)
        try:
            while True:
                await self._scan_once()
                await asyncio.sleep(self.config.scan_interval)
        finally:
            self._publish(self.config.topics["availability"], "offline", retain=True)
            self._mqtt.loop_stop()
            self._mqtt.disconnect()
