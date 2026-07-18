"""Config: env parsing, topic layout, placeholder-MAC guard."""
from __future__ import annotations

from gtx2_bridge.config import BridgeConfig, PLACEHOLDER_MAC, mac_slug


def test_mac_slug():
    assert mac_slug("AA:BB:CC:12:34:56") == "aabbcc123456"
    assert mac_slug("aa-bb-cc") == "aabbcc"


def test_default_is_placeholder_and_refused():
    c = BridgeConfig()
    assert c.mac == PLACEHOLDER_MAC and c.uses_placeholder_mac()


def test_topics_layout():
    c = BridgeConfig(topic_root="gtx2")
    t = c.topics
    assert t["cmd"] == "gtx2/cmd"
    assert t["result"] == "gtx2/result"
    assert t["registry"] == "gtx2/registry"
    assert t["availability"] == "gtx2/bridge/availability"
    wt = c.watch_topics("aabbcc112233")
    assert wt["state"] == "gtx2/aabbcc112233/state"
    assert wt["health"] == "gtx2/aabbcc112233/health"


def test_from_env_overrides():
    env = {"GTX2_MAC": "AA:BB:CC:11:22:33", "GTX2_MQTT_HOST": "192.0.2.10",
           "GTX2_MQTT_PORT": "8883", "GTX2_MQTT_TLS": "true", "GTX2_DIAL_ID": "25005",
           "GTX2_TOPIC_ROOT": "watch", "GTX2_SCAN_INTERVAL": "30"}
    c = BridgeConfig.from_env(env)
    assert c.mac == "AA:BB:CC:11:22:33" and not c.uses_placeholder_mac()
    assert c.mqtt_host == "192.0.2.10" and c.mqtt_port == 8883 and c.mqtt_tls is True
    assert c.dial_id == 25005 and c.topic_root == "watch" and c.scan_interval == 30.0
    assert c.topics["cmd"] == "watch/cmd"
