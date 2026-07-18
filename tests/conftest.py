"""Shared test fixtures. Offline only — no BLE, no broker.

A synthetic MAC (never a real device address) and a fake BLE client stand in for the watch.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from starmax_client.protobuf import ProtobufWriter

from gtx2_bridge.config import BridgeConfig
from gtx2_bridge.dispatch import Dispatcher

# ---------------------------------------------------------------------------
# gtx2 custom-component pure-logic import shim.
#
# `from custom_components.gtx2 import logic` (and `.const`) must work WITHOUT
# homeassistant installed (repo convention: pure-pytest, no HA). But the real
# `custom_components/gtx2/__init__.py` imports homeassistant at module top, so
# letting Python execute it would crash collection. We pre-register the package
# with an explicit __path__ (pointing at the real dir) but WITHOUT running its
# __init__.py — so submodules like `logic`/`const` import normally (their
# relative `from .const import …` resolves against this __path__) while the
# HA-heavy package init never runs. Idempotent; shared by every gtx2cc test.
# ---------------------------------------------------------------------------
_GTX2_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "gtx2"
if _GTX2_DIR.is_dir():
    if "custom_components" not in sys.modules:
        _cc = types.ModuleType("custom_components")
        _cc.__path__ = [str(_GTX2_DIR.parent)]
        sys.modules["custom_components"] = _cc
    if "custom_components.gtx2" not in sys.modules:
        _pkg = types.ModuleType("custom_components.gtx2")
        _pkg.__path__ = [str(_GTX2_DIR)]
        _pkg.__package__ = "custom_components.gtx2"
        sys.modules["custom_components.gtx2"] = _pkg

SYNTHETIC_MAC = "AA:BB:CC:11:22:33"      # PII-free; distinct from the placeholder AA:BB:CC:DD:EE:FF


class FakeClient:
    """Records outbound frames; answers reads (0x16 dial-list, others a trivial ack)."""

    def __init__(self, active: str = "custom_id_25001.bin", mtu: int = 244) -> None:
        self.sent = []
        self.responses = []
        self.requests = []
        self._mtu_payload = mtu
        self._active = active
        self.disconnected = False
        self._seq = 0

    def next_seq(self) -> int:
        self._seq = (self._seq % 255) + 1
        return self._seq

    async def send_raw(self, frame, response: bool = False) -> None:
        self.sent.append(bytes(frame))
        self.responses.append(response)

    async def request(self, frame, opcode, timeout: float = 5.0):
        self.requests.append((bytes(frame), opcode))
        if opcode == 0x16:                       # dial-list reply shape (see test_dials.py)
            payload = (ProtobufWriter()
                       .message(10, ProtobufWriter().varint(1, 1).varint(2, 1)
                                .varint(3, len(self._active)).string(4, self._active).to_bytes())
                       .string(14, self._active).to_bytes())
        else:
            payload = b"\x08\x01"                # generic non-empty ack
        return SimpleNamespace(opcode=opcode, payload=payload)

    async def disconnect(self) -> None:
        self.disconnected = True


@pytest.fixture
def synthetic_mac() -> str:
    return SYNTHETIC_MAC


@pytest.fixture
def make_dispatcher():
    """Return ``make(active=…) -> (dispatcher, fake_client)`` with an injected fake connect."""
    def _make(active: str = "custom_id_25001.bin", config: BridgeConfig = None):
        fake = FakeClient(active=active)

        async def _connect(mac, **kw):
            fake.connected_to = mac
            return fake

        disp = Dispatcher(config or BridgeConfig(), connect=_connect)
        return disp, fake
    return _make
