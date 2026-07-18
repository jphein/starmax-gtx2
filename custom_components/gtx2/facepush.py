"""Gauge face push: vendored render -> roam-safe CHUNKED delivery -> confirm-install gate + re-push.

At the HW-safe LZ4 cap=512 the gauge is ~9-10 KB — OVER the /local/ TLS OOM ceiling — so it ships as
sequenced `esphome.<holder>_<watch>_push_dial_chunk` calls to the ONE holding node (design ruling
2026-07-15: /local/ is dead for the gauge).

ROAM SAFETY: all N chunks must reach the SAME node — pin the holder at seq 0, re-check before each
chunk, restart from seq 0 on a mid-burst roam (a node rejects an orphan it never saw seq 0 for, so a
bad roam drops a push, never corrupts).

CONFIRM-INSTALL GATE (the chunked-transport contract): after delivery, the holding node re-reads (0x16)
and emits sensor.gtx2_<node>_<watch>_last_install = "<dial_id>:<status>:<crc16hex>" (status ok|fail).
We wait ~CONFIRM_TIMEOUT_S for "<dial>:ok" (crc ignored); on fail/timeout we RE-PUSH from seq 0 —
retry ownership is OURS (the node verifies + emits, it does NOT retry). If the sensor entity does not
exist (pre-reflash — it ships with the node reflash) we log once and treat the push as delivered-but-
unconfirmed WITHOUT retrying (a sensor that can't appear never will).

Screen-break keeps its /local/ url path (pre-rendered <8 KB blobs) in screen_break.py — this module
is the GAUGE path only. gridwatts stays OFF; the switch flip / re-enable is owned elsewhere.
"""
from __future__ import annotations

import asyncio
import logging
from functools import partial

from homeassistant.core import HomeAssistant

from . import logic
from .render import build_grid_static_blob

_LOGGER = logging.getLogger(__name__)

CHUNK_SANITY_MAX = 64 * 1024        # generous sanity cap (chunking is how >8 KB ships)
INSTALL_SENSOR_FMT = "sensor.gtx2_{node}_{watch}_last_install"
CONFIRM_TIMEOUT_S = 10.0
CONFIRM_POLL_S = 0.5
MAX_PUSH_ATTEMPTS = 3               # total delivery+confirm attempts before giving up (we own retry)


class _Roamed(Exception):
    def __init__(self, frm: str, to: str | None) -> None:
        self.frm, self.to = frm, to


async def _deliver_chunks(hass: HomeAssistant, hub, watch: str, dial_id: int, chunks: list,
                          max_restarts: int) -> str | None:
    """Roam-safe sequenced delivery. Returns the node prefix all chunks reached, or None on failure
    (no holder / gave up roaming / node service error) — last_result is set on failure."""
    for attempt in range(max_restarts + 1):
        pinned = hub.data[watch]["holder"]
        if not pinned:
            hub.set_last_result("push_face: no online holder")
            return None
        service = f"{pinned}_{watch}_push_dial_chunk"
        try:
            for c in chunks:
                if hub.data[watch]["holder"] != pinned:
                    raise _Roamed(pinned, hub.data[watch]["holder"])
                await hass.services.async_call(
                    "esphome", service, {"dial_id": int(dial_id), **c}, blocking=True)
        except _Roamed as r:
            _LOGGER.warning("push_face %s roamed %s->%s mid-push (attempt %d/%d) — restarting",
                            watch, r.frm, r.to, attempt + 1, max_restarts + 1)
            continue
        except Exception as err:  # noqa: BLE001 — e.g. push_dial_chunk not on this node yet
            _LOGGER.warning("push_face %s: chunk stream to %s failed: %s", watch, pinned, err)
            hub.set_last_result(f"push_face: delivery failed via {pinned} ({err})")
            return None
        else:
            return pinned
    hub.set_last_result("push_face: gave up (roaming)")
    return None


async def confirm_install_gate(hass: HomeAssistant, node: str, watch: str, dial_id: int) -> str:
    """Wait ~CONFIRM_TIMEOUT_S for the node's last_install sensor to confirm `dial_id`.

    Returns 'ok' | 'fail' | 'timeout' | 'absent'. 'absent' = the sensor entity does not exist
    (pre-reflash) — the caller must NOT retry. 'timeout' = the entity exists but never confirmed in
    time — the caller re-pushes. crc is ignored (contract); we match dial_id + status only.

    NOTE: for a same-dial re-push the sensor may briefly still read the PREVIOUS push's "<dial>:ok"
    until the node re-verifies; the settle poll below narrows that window. If the chunked-transport layer does
    not clear/pend the sensor at push-receipt, a fast same-dial re-push could confirm on a stale ok —
    flagged for review (a node-side property, not resolvable here without a freshness token).
    """
    eid = INSTALL_SENSOR_FMT.format(node=node, watch=watch)
    if hass.states.get(eid) is None:
        return "absent"                                   # entity unregistered -> pre-reflash
    polls = max(1, int(CONFIRM_TIMEOUT_S / CONFIRM_POLL_S))
    for _ in range(polls):
        await asyncio.sleep(CONFIRM_POLL_S)               # settle first: let the node emit the result
        st = hass.states.get(eid)
        cls = logic.classify_install(st.state if st is not None else None, dial_id)
        if cls == logic.INSTALL_OK:
            return "ok"
        if cls == logic.INSTALL_FAIL:
            return "fail"
        if cls == logic.INSTALL_ABSENT:
            return "absent"                               # entity vanished mid-wait
    return "timeout"


async def push_face(hass: HomeAssistant, hub, watch: str, watts: int, dial_id: int,
                    max_w: int, max_restarts: int = 1) -> None:
    """Render the gauge -> chunk -> deliver roam-safe -> confirm install (re-push on fail/timeout).

    Reports delivery + confirmation via hub.last_result. On a pre-reflash node (no install sensor) it
    delivers once and reports unconfirmed (no retry-loop)."""
    blob = await hass.async_add_executor_job(
        partial(build_grid_static_blob, float(watts), max_w=int(max_w)))
    if len(blob) > CHUNK_SANITY_MAX:
        _LOGGER.warning("push_face %s: blob %dB exceeds the %dB chunk sanity cap — not pushed",
                        watch, len(blob), CHUNK_SANITY_MAX)
        hub.set_last_result(f"push_face: blob {len(blob)}B exceeds sanity cap — not pushed")
        return
    chunks = logic.chunk_blob(blob)
    holder0 = hub.data[watch]["holder"]
    if holder0 is None:
        hub.set_last_result("push_face: no online holder")
        return
    # Hold the holder's radio lock for the WHOLE push (stream + confirm + retries) so no second
    # dial-push and no routed read/weather contends on this node while the install is in flight.
    # (Keyed by the holder at start; a mid-push roam is rare and streams to the new node under the
    # retry loop — the common same-node contention is what this serializes.)
    async with hub.node_lock(holder0):
        outcome = "timeout"
        for attempt in range(MAX_PUSH_ATTEMPTS):
            node = await _deliver_chunks(hass, hub, watch, dial_id, chunks, max_restarts)
            if node is None:
                return                                    # delivery failure already reported
            outcome = await confirm_install_gate(hass, node, watch, dial_id)
            if outcome == "ok":
                hub.set_last_result(
                    f"push_face: {len(blob)}B installed on {watch} via {node} (confirmed)")
                return
            if outcome == "absent":
                _LOGGER.info("push_face %s: no install sensor (%s) — delivered but unconfirmed "
                             "(pre-reflash); not retrying", watch,
                             INSTALL_SENSOR_FMT.format(node=node, watch=watch))
                hub.set_last_result(
                    f"push_face: {len(blob)}B pushed to {watch} via {node} (unconfirmed — no install sensor)")
                return
            _LOGGER.warning("push_face %s: install %s via %s (attempt %d/%d)%s", watch, outcome, node,
                            attempt + 1, MAX_PUSH_ATTEMPTS,
                            " — re-pushing" if attempt + 1 < MAX_PUSH_ATTEMPTS else "")
        hub.set_last_result(
            f"push_face: install unconfirmed after {MAX_PUSH_ATTEMPTS} attempts (last: {outcome})")
