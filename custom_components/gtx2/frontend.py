"""Serve the bundled gtx2 watch-card as a static path + auto-register the Lovelace resource.

Canonical card location: custom_components/gtx2/www/gtx2-watch-card.js (single source of truth; the
dashboard repo dir points here per DEPLOY notes). Served at /gtx2-static/gtx2-watch-card.js so it
survives updates without a manual /local/ copy. The Lovelace resource is added programmatically only
when Lovelace runs in storage mode; YAML mode is log-and-skip (add the resource by hand there).
"""
from __future__ import annotations

import logging
import os

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

CARD_FILENAME = "gtx2-watch-card.js"
CARD_URL = "/gtx2-static/gtx2-watch-card.js"


async def async_register_frontend(hass: HomeAssistant) -> None:
    card_path = os.path.join(os.path.dirname(__file__), "www", CARD_FILENAME)
    if not os.path.exists(card_path):
        _LOGGER.warning("gtx2 watch-card not bundled at %s — skipping resource registration", card_path)
        return

    # Static path — new StaticPathConfig API (HA >= 2024.7) with a fallback for older cores.
    try:
        from homeassistant.components.http import StaticPathConfig
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, card_path, False)])
    except (ImportError, AttributeError):
        try:
            hass.http.register_static_path(CARD_URL, card_path, False)
        except Exception as err:  # noqa: BLE001 — never fail setup over a card resource
            _LOGGER.warning("gtx2: could not register static path for the card: %s", err)
            return

    await _ensure_lovelace_resource(hass)


async def _ensure_lovelace_resource(hass: HomeAssistant) -> None:
    lovelace = hass.data.get("lovelace")
    resources = getattr(lovelace, "resources", None)
    if resources is None and isinstance(lovelace, dict):
        resources = lovelace.get("resources")
    if resources is None:
        _LOGGER.debug("gtx2: Lovelace resources unavailable — add the card resource manually")
        return
    try:
        if hasattr(resources, "loaded") and not resources.loaded:
            await resources.async_load()
            resources.loaded = True
        # storage-mode only: YAML-mode resource stores are read-only (no async_create_item).
        if getattr(resources, "store", None) is None:
            _LOGGER.debug("gtx2: Lovelace in YAML mode — add the card resource manually")
            return
        for item in resources.async_items():
            if item.get("url") == CARD_URL:
                return
        await resources.async_create_item({"res_type": "module", "url": CARD_URL})
        _LOGGER.info("gtx2: registered Lovelace card resource %s", CARD_URL)
    except Exception as err:  # noqa: BLE001 — best-effort; card still works via manual add
        _LOGGER.debug("gtx2: could not auto-register the Lovelace resource (%s)", err)
