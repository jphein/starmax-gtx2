"""The four watch actions, built on the verified ``starmax_client`` library.

Split into two halves so everything is offline-testable:

  * ``plan_*`` — PURE frame builders (no BLE, ``seq=0``). Used by ``--dry-run`` and unit tests.
  * ``do_*``   — async live drivers that send the frames over a connected ``StarmaxClient``.

Only [CAP] (capture-verified) commands are used:
  buzz    -> base.build_find_device            (0x18)
  time    -> base.build_set_time               (0x02)
  weather -> settings.build_feature_bitmap (0x04) + base.build_weather (0x12)
  notify  -> dials.push_dial                   (D-plane dial-push; auto-activates on D4)
"""
from __future__ import annotations

import asyncio
import datetime as _dt
from typing import List, Optional

from starmax_client import framing
from starmax_client.commands import base, dials, files
from starmax_client.commands.base import Weather
from starmax_client.commands.settings import build_feature_bitmap

from . import faces

CHUNK_MAX = files.CHUNK_MAX


# =============================================================================
# frame planning (pure, offline — seq=0)
# =============================================================================
def plan_buzz() -> List[bytes]:
    """[CAP 0x18] start-buzz then stop-buzz. The live driver spaces them by --duration."""
    return [base.build_find_device(True), base.build_find_device(False)]


def plan_set_time(when: _dt.datetime) -> List[bytes]:
    """[CAP 0x02] one set-time frame for a tz-aware ``when``."""
    return [base.build_set_time(when)]


def plan_weather(weather: Weather, *, enable: bool = True) -> List[bytes]:
    """[CAP] optional 0x04 feature-enable (display gate) then the 0x12 weather push."""
    frames: List[bytes] = []
    if enable:
        frames.append(build_feature_bitmap())
    frames.append(base.build_weather(weather))
    return frames


def plan_notification_push(blob: bytes, dial_id: int, *, chunk: int = CHUNK_MAX) -> List[bytes]:
    """[CAP] the full D3/D1/D2*/D4 dial-push sequence for a rendered notification blob."""
    return dials.plan_dial_push(blob, dial_id, chunk=chunk)


def frame_summary(frame: bytes) -> str:
    """``op=0x18 flag=0 15B`` — a one-line description of a C1 frame (for dry-run output)."""
    try:
        fr = framing.parse_frame(bytes(frame), direction=framing.DIR_APP_TO_WATCH)
        return f"op=0x{fr.opcode:02x} flag={fr.flag} {len(bytes(frame))}B"
    except Exception:  # noqa: BLE001 - D-plane frames (0xD1..0xD4) aren't C1 frames
        return f"raw[0]=0x{frame[0]:02x} {len(bytes(frame))}B"


# =============================================================================
# live drivers (async, over a connected StarmaxClient)
# =============================================================================
async def do_buzz(client, *, duration: float = 5.0) -> dict:
    """Ring the watch for ``duration`` seconds, then stop. ``duration<=0`` = fire-and-stop only."""
    await client.send_raw(base.build_find_device(True, seq=client.next_seq()))
    if duration > 0:
        await asyncio.sleep(duration)
    await client.send_raw(base.build_find_device(False, seq=client.next_seq()))
    return {"buzzed_s": max(duration, 0.0)}


async def do_set_time(client, when: _dt.datetime) -> dict:
    """Set the watch clock to ``when`` (tz-aware). Returns whether the 0x02 was acked."""
    ack = await client.request(base.build_set_time(when, seq=client.next_seq()),
                               base.OP_SET_TIME, timeout=5.0)
    return {"time": when.isoformat(), "acked": ack is not None}


async def do_weather(client, weather: Weather, *, enable: bool = True) -> dict:
    """Push weather. Sends the 0x04 feature-enable first by default (display gate)."""
    if enable:
        await client.send_raw(build_feature_bitmap(seq=client.next_seq()))
    await client.send_raw(base.build_weather(weather, seq=client.next_seq()))
    return {"city": weather.city, "temp": weather.temp_current,
            "hi": weather.temp_max, "lo": weather.temp_min, "condition": weather.condition}


async def do_notification(client, blob: bytes, *, dial_id: int, chunk: int = CHUNK_MAX,
                          confirm: bool = True) -> dict:
    """Push a pre-rendered notification blob as ``custom_id_<dial_id>.bin`` (auto-activates)."""
    result = await dials.push_dial(client, blob, dial_id=dial_id, chunk=chunk, confirm=confirm)
    result["dial_id"] = dial_id
    result["blob_bytes"] = len(blob)
    return result


# =============================================================================
# live connection (mirrors cli._connect_and_bind — connect + accountless 0x01 bind)
# =============================================================================
async def connect_and_bind(mac: str, *, connect_timeout: float = 20.0):
    """Connect to the watch and run the accountless bind handshake. Returns a ``StarmaxClient``.

    Live only (imports the BLE transport). Caller MUST ``await client.disconnect()``.
    """
    from starmax_client.transport import StarmaxClient

    client = StarmaxClient(mac, connect_timeout=connect_timeout)
    await client.connect()
    await client.request(base.build_bind(seq=client.next_seq()), base.OP_BIND, timeout=8.0)
    return client
