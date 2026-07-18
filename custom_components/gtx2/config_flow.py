"""Config + options flow for the GTX2 hub.

Single-instance config entry (one click adds the hub). Everything else — watches (name/MAC; empty
MAC disables a watch), holder-scan nodes, gridwatts, and media targets — lives in the OPTIONS flow,
so real MACs never touch YAML/repo/dashboard (they stay in the config entry on the VM).
"""
from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (DEFAULT_MAX_W, DEFAULT_WATCHES, DOMAIN, GRIDKW_GATE_DEFAULT,
                    GRIDWATTS_SOURCE_DEFAULT, NODE_ROOMS, WWW_URL_BASE_DEFAULT)


class Gtx2ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Single-instance setup: one click adds the hub + its services."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="GTX2 Watch", data={})
        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "Gtx2OptionsFlow":
        return Gtx2OptionsFlow(config_entry)


class Gtx2OptionsFlow(OptionsFlow):
    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._opts: dict = dict(entry.options)

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(step_id="init",
                                    menu_options=["watches", "nodes", "gridwatts", "media"])

    def _watch_slugs(self) -> list[str]:
        """Slugs of the currently-configured watches (options → const default)."""
        return list((self._opts.get("watches") or DEFAULT_WATCHES).keys())

    # ------------------------------------------------------------------ watches
    async def async_step_watches(self, user_input=None):
        """FREE-FORM watch list — one watch per line: ``slug = Name = MAC``.

        Add a watch by adding a line; remove one by deleting its line; an empty MAC disables a watch
        (kept in the list, no entities). Symmetric with the nodes step so growing the fleet never
        needs a code edit — the hub, entities and services all rebuild from these options on save.
        """
        cur = self._opts.get("watches") or DEFAULT_WATCHES
        if user_input is not None:
            watches: dict = {}
            for line in (user_input.get("watches", "") or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = [p.strip() for p in line.split("=")]
                slug = parts[0]
                if not slug:
                    continue
                name = parts[1] if len(parts) > 1 and parts[1] else slug.replace("_", " ").title()
                mac = parts[2] if len(parts) > 2 else ""
                watches[slug] = {"name": name, "mac": mac}
            # never wipe to nothing: fall back to the three defaults if the box was cleared
            self._opts["watches"] = watches or {k: dict(v) for k, v in DEFAULT_WATCHES.items()}
            return self.async_create_entry(title="", data=self._opts)
        default_text = "\n".join(f"{slug} = {c.get('name', slug.title())} = {c.get('mac', '')}"
                                 for slug, c in cur.items())
        schema = vol.Schema({
            vol.Optional("watches", default=default_text):
                selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
        })
        return self.async_show_form(step_id="watches", data_schema=schema)

    # ------------------------------------------------------------------ nodes
    async def async_step_nodes(self, user_input=None):
        cur = self._opts.get("nodes") or NODE_ROOMS
        if user_input is not None:
            nodes = {}
            for line in (user_input.get("nodes", "") or "").splitlines():
                line = line.strip()
                if not line or "=" not in line:
                    continue
                prefix, room = line.split("=", 1)
                nodes[prefix.strip()] = room.strip()
            self._opts["nodes"] = nodes or dict(NODE_ROOMS)
            return self.async_create_entry(title="", data=self._opts)
        default_text = "\n".join(f"{k}={v}" for k, v in cur.items())
        schema = vol.Schema({
            vol.Optional("nodes", default=default_text):
                selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
        })
        return self.async_show_form(step_id="nodes", data_schema=schema)

    # ------------------------------------------------------------------ gridwatts
    async def async_step_gridwatts(self, user_input=None):
        gw = self._opts.get("gridwatts") or {}
        if user_input is not None:
            self._opts["gridwatts"] = {
                "source_entity": user_input.get("source_entity", GRIDWATTS_SOURCE_DEFAULT),
                "max_w": int(user_input.get("max_w", DEFAULT_MAX_W)),
                "target": user_input.get("target", "daily"),
                "www_url_base": user_input.get("www_url_base", WWW_URL_BASE_DEFAULT),
                "gridkw_gate": user_input.get("gridkw_gate", GRIDKW_GATE_DEFAULT),
                "gridkw_target": user_input.get("gridkw_target", user_input.get("target", "daily")),
            }
            return self.async_create_entry(title="", data=self._opts)
        schema = vol.Schema({
            vol.Optional("source_entity", default=gw.get("source_entity", GRIDWATTS_SOURCE_DEFAULT)):
                selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional("max_w", default=gw.get("max_w", DEFAULT_MAX_W)):
                vol.All(vol.Coerce(int), vol.Range(min=1000, max=50000)),
            vol.Optional("target", default=gw.get("target", "daily")):
                selector.SelectSelector(selector.SelectSelectorConfig(options=self._watch_slugs())),
            vol.Optional("www_url_base", default=gw.get("www_url_base", WWW_URL_BASE_DEFAULT)): str,
            # Live-kW RTC feature: while `gridkw_gate` is on, the scheduler leaves `gridkw_target`'s
            # clock/date/face to the live-kW automation (sole RTC writer). Defaults to the gauge target.
            vol.Optional("gridkw_gate", default=gw.get("gridkw_gate", GRIDKW_GATE_DEFAULT)): str,
            vol.Optional("gridkw_target", default=gw.get("gridkw_target", gw.get("target", "daily"))):
                selector.SelectSelector(selector.SelectSelectorConfig(options=self._watch_slugs())),
        })
        return self.async_show_form(step_id="gridwatts", data_schema=schema)

    # ------------------------------------------------------------------ media
    async def async_step_media(self, user_input=None):
        m = self._opts.get("media") or {}
        if user_input is not None:
            self._opts["media"] = {k: v for k, v in user_input.items() if v not in (None, "")}
            return self.async_create_entry(title="", data=self._opts)
        media_sel = selector.EntitySelector(selector.EntitySelectorConfig(domain="media_player"))
        schema = vol.Schema({
            vol.Optional("media_player", description={"suggested_value": m.get("media_player")}):
                media_sel,
            vol.Optional("findphone_notify", description={"suggested_value": m.get("findphone_notify")}):
                str,
            vol.Optional("findphone_speaker",
                         description={"suggested_value": m.get("findphone_speaker")}): media_sel,
            vol.Optional("tts_engine", description={"suggested_value": m.get("tts_engine")}):
                selector.EntitySelector(selector.EntitySelectorConfig(domain="tts")),
        })
        return self.async_show_form(step_id="media", data_schema=schema)
