/* ═══════════════════════════════════════════════════════════════════════════
 *  gtx2-watch-card.js — live round-watch Lovelace card for the Starmax GTX2
 *  (THREE-WATCH build — daily · spare · watch3 — routed via the gtx2 integration)
 * ═══════════════════════════════════════════════════════════════════════════
 *  Recreates the round tri-ring activity face from the project site
 *  (smartwatch/site/index.html) as a live HA card, and adds a row of SAFE
 *  one-tap actions that act on THIS watch. Dark/light follow the active HA
 *  theme; accents match the site (#7aa2f7 / #43d17a / #f5a623 / #f77c9a / #6fd3e0).
 *
 *  WHY A CUSTOM CARD OWNS THE ACTIONS: the `gtx2.*` services take a `watch` slug
 *  (`daily`/`spare`/`watch3`); the integration owns MAC lookup + node routing +
 *  host-bridge fallback. Lovelace `tap_action` `data:` is NOT Jinja-templated, so
 *  a plain button can't inject the right slug for the card it lives in. This card
 *  injects `watch: <this card's watch>` into every `gtx2.*` call at click time —
 *  so the dashboard YAML stays PII-free (no MAC anywhere) and each watch's buttons
 *  hit the right watch.
 *
 *  Only SAFE (GREEN) actions are wired. No destructive command exists as a
 *  `gtx2.*` service, so none can be reached here by construction.
 *
 *  INSTALL: the gtx2 integration serves this file at `/gtx2-static/gtx2-watch-card.js`
 *  and registers it as a frontend module automatically. Canonical source lives in
 *  the component at `custom_components/gtx2/www/gtx2-watch-card.js` (single source of
 *  truth — no `/config/www` copy). If a stale `/local/gtx2-watch-card.js` storage
 *  resource exists from the pre-migration build, delete it (see deploy/MIGRATION.md
 *  Step 5b) so the card isn't double-loaded.
 *
 *  USAGE:
 *    - type: custom:gtx2-watch-card
 *      name: Spare GTX2
 *      watch: spare                      # daily | spare | watch3 — the integration owns the MAC
 *      entities:
 *        connected: binary_sensor.gtx2_spare_connected
 *        present:   binary_sensor.gtx2_spare_present
 *        heart_rate: sensor.gtx2_spare_heart_rate
 *        spo2: sensor.gtx2_spare_spo2
 *        steps: sensor.gtx2_spare_steps
 *        calories: sensor.gtx2_spare_calories
 *        distance: sensor.gtx2_spare_distance
 *        room: sensor.gtx2_spare_room
 *        rssi: sensor.gtx2_spare_link_rssi
 *        active_face: sensor.gtx2_spare_active_face
 *      goals: { steps: 8000, calories: 600, distance: 6 }
 *      actions: [ …see below… ]
 *
 *  actions: each { label, icon?, service, kind?, data? }
 *    service = a `gtx2.*` service (or a `button.*` entity). For `gtx2.*` the card
 *    adds `watch:` automatically. kind: "notify" also attaches title/body from the
 *    notify text entities (notify_title_entity / notify_body_entity, default
 *    text.gtx2_notify_title / text.gtx2_notify_body).
 * ═══════════════════════════════════════════════════════════════════════════ */

const PALETTE = {
  accent: "#7aa2f7", ok: "#43d17a", warn: "#f5a623", heart: "#f77c9a", spo2: "#6fd3e0",
};

class Gtx2WatchCard extends HTMLElement {
  setConfig(config) {
    this._config = {
      name: "GTX2",
      watch: "daily",            // slug the integration routes on (daily|spare|watch3)
      entities: {},              // explicit entity_id map
      actions: [],               // [{label, icon, service, kind, data}]
      note: null,                // if set → render note face instead of rings
      goals: { steps: 8000, calories: 600, distance: 6 },
      notify_title_entity: "text.gtx2_notify_title",
      notify_body_entity: "text.gtx2_notify_body",
      ...config,
    };
    this._built = false;
  }

  set hass(hass) { this._hass = hass; this._render(); }
  getCardSize() { return this._config.note ? 4 : 5; }

  static getStubConfig() {
    return {
      name: "Spare GTX2",
      watch: "spare",
      entities: {
        connected: "binary_sensor.gtx2_spare_connected",
        present: "binary_sensor.gtx2_spare_present",
        heart_rate: "sensor.gtx2_spare_heart_rate",
        spo2: "sensor.gtx2_spare_spo2",
        steps: "sensor.gtx2_spare_steps",
        calories: "sensor.gtx2_spare_calories",
        distance: "sensor.gtx2_spare_distance",
        room: "sensor.gtx2_spare_room",
        rssi: "sensor.gtx2_spare_link_rssi",
        active_face: "sensor.gtx2_spare_active_face",
      },
      actions: [
        { label: "Find", service: "gtx2.buzz" },
        { label: "Sync", service: "gtx2.read_health" },
      ],
    };
  }

  /* ---- data helpers ---- */
  _ent(key) { return (this._config.entities || {})[key] || null; }
  _s(id) { const e = id && this._hass && this._hass.states[id]; return e ? e.state : undefined; }
  _num(id, d = 0) { const v = parseFloat(this._s(id)); return Number.isFinite(v) ? v : d; }
  _on(id) { return this._s(id) === "on"; }
  _has(id) { return !!(id && this._hass && this._hass.states[id]); }
  _live(id) { const v = this._s(id); return v != null && !["unknown", "unavailable", "none", ""].includes(v); }

  /* ---- SAFE actions only; the integration owns MAC/routing — card injects the watch slug ---- */
  _invoke(action) {
    if (!this._hass || !action || !action.service) return;
    const [domain, service] = action.service.split(".");
    const data = { ...(action.data || {}) };
    if (domain === "gtx2") data.watch = this._config.watch;        // integration owns MAC lookup
    if (action.kind === "notify") {
      if (this._live(this._config.notify_title_entity)) data.title = this._s(this._config.notify_title_entity);
      if (this._live(this._config.notify_body_entity)) data.body = this._s(this._config.notify_body_entity);
    }
    if (domain === "button") this._hass.callService("button", "press", { entity_id: action.service });
    else this._hass.callService(domain, service, data);
  }
  /* is an action callable right now? gtx2.* → the service must be registered (it is NOT an entity,
     so _has() would always disable it); button.* → the entity must exist; else → service registered. */
  _avail(a) {
    if (!a || !a.service || !this._hass) return false;
    const [domain, service] = a.service.split(".");
    if (domain === "gtx2") return !!(this._hass.services.gtx2 && this._hass.services.gtx2[service]);
    if (domain === "button") return this._has(a.service);
    return !!(this._hass.services[domain] && this._hass.services[domain][service]);
  }
  _moreInfo(entityId) {
    const ev = new Event("hass-more-info", { bubbles: true, composed: true });
    ev.detail = { entityId };
    this.dispatchEvent(ev);
  }

  /* ---- render ---- */
  _render() {
    if (!this._hass) return;
    if (!this._built) this._build();
    this._panels.innerHTML = "";
    this._panels.appendChild(this._watchPanel());
  }
  _build() {
    const root = this.attachShadow ? (this.shadowRoot || this.attachShadow({ mode: "open" })) : this;
    const card = document.createElement("ha-card");
    card.innerHTML = `<style>${this._css()}</style><div class="wrap"></div>`;
    this._panels = card.querySelector(".wrap");
    root.innerHTML = "";
    root.appendChild(card);
    this._built = true;
  }

  _watchPanel() {
    const el = document.createElement("div");
    el.className = "watch";

    const connId = this._ent("connected");
    const presId = this._ent("present");
    const isConn = this._has(connId) ? this._on(connId) : null;
    const isPres = this._has(presId) ? this._on(presId) : null;
    const live = isConn === true || isPres === true;
    const pending = !!connId && !this._has(connId) && !this._config.note;   // spare node not up yet

    const name = this._config.name || "GTX2";
    // meta line prefers active_face (real device-state — the current dial). firmware
    // is only a build STAMP (not a semantic version — see the "firmware build ≠
    // version" finding), so it is deliberately NOT surfaced as an identity here.
    const subtitle = this._live(this._ent("active_face")) ? this._s(this._ent("active_face")) : "";
    const room = this._s(this._ent("room"));
    const roomTxt = this._live(this._ent("room")) ? room : (live ? "locating…" : "away");
    const rssiV = this._num(this._ent("rssi"), null);

    const pill = isConn === true ? "linked" : isConn === false ? "no link"
      : isPres === true ? "present" : isPres === false ? "away" : "—";

    // status stats (only for entities that are actually configured)
    const stats = [];
    stats.push(`<div class="stat"><span class="k">Room</span><span class="v">${this._esc(roomTxt)}</span></div>`);
    if (this._ent("spo2")) stats.push(`<div class="stat"><span class="k">SpO₂</span><span class="v spo2">${this._fmt(this._ent("spo2"), "%")}</span></div>`);
    if (this._ent("heart_rate")) stats.push(`<div class="stat"><span class="k">Heart</span><span class="v heart">${this._fmt(this._ent("heart_rate"), "")}</span></div>`);
    if (this._ent("rssi")) stats.push(`<div class="stat"><span class="k">Link</span><span class="v">${rssiV == null ? "—" : Math.round(rssiV) + " dBm"}</span></div>`);
    else if (presId) stats.push(`<div class="stat"><span class="k">Presence</span><span class="v">${isPres ? "in range" : "away"}</span></div>`);

    const faceHtml = this._config.note
      ? `<div class="note-face" aria-label="${this._esc(name)} — ${this._esc(this._config.note)}">
           <div class="glyph">⌚</div><div class="note-cap">on-demand</div></div>`
      : `<canvas class="face" width="360" height="360" aria-label="${this._esc(name)} activity rings"></canvas>`;

    el.innerHTML = `
      <div class="head" role="button" tabindex="0" aria-label="${this._esc(name)} — details">
        <div class="dot ${live ? "live" : "off"}" aria-hidden="true"></div>
        <div class="id">
          <b>${this._esc(name)}</b>
          <span class="meta">${this._esc(subtitle || (pending ? "awaiting control node" : "GTX2"))}</span>
        </div>
        <div class="badges"><span class="pill ${live ? "on" : "gone"}">${pill}</span></div>
      </div>
      <div class="body">
        ${faceHtml}
        <div class="side">${stats.join("")}</div>
      </div>
      ${this._config.note ? `<div class="foot-note">${this._esc(this._config.note)}</div>` : (pending ? `<div class="foot-note">Rings light up once the dedicated <code>gtx2-spare</code> node is placed.</div>` : "")}
      <div class="ctl">
        ${(this._config.actions || []).map((a, i) =>
          `<button class="b${i === 0 ? " prim" : ""}" data-i="${i}" ${this._avail(a) ? "" : "disabled"} title="${this._esc(a.label)}">${a.icon ? `<span class="ico">${this._esc(a.icon)}</span>` : ""}${this._esc(a.label)}</button>`
        ).join("")}
      </div>`;

    el.querySelectorAll("button.b").forEach((btn) => {
      const a = this._config.actions[+btn.dataset.i];
      btn.addEventListener("click", () => this._invoke(a));
    });
    const head = el.querySelector(".head");
    const openId = connId || presId || this._ent("room");
    const open = () => { if (this._has(openId)) this._moreInfo(openId); };
    head.addEventListener("click", open);
    head.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });

    if (!this._config.note) {
      const cv = el.querySelector("canvas.face");
      requestAnimationFrame(() => this._drawFace(cv, live));
    }
    return el;
  }

  _drawFace(cv, connected) {
    const ctx = cv.getContext("2d");
    const DPR = Math.min(window.devicePixelRatio || 1, 2);
    const S = 180;
    cv.width = S * DPR; cv.height = S * DPR;
    cv.style.width = S + "px"; cv.style.height = S + "px";
    ctx.scale(DPR, DPR);
    const C = S / 2;

    const steps = this._num(this._ent("steps"));
    const cal = this._num(this._ent("calories"));
    const dist = this._num(this._ent("distance"));
    const hr = this._num(this._ent("heart_rate"));
    const g = this._config.goals;

    ctx.fillStyle = "#000";
    ctx.beginPath(); ctx.arc(C, C, C, 0, Math.PI * 2); ctx.fill();

    const dim = connected ? 1 : 0.45;
    const ring = (r, pct, color) => {
      ctx.globalAlpha = 1;
      ctx.beginPath(); ctx.strokeStyle = "rgba(255,255,255,0.10)"; ctx.lineWidth = 11; ctx.lineCap = "round";
      ctx.arc(C, C, r, 0, Math.PI * 2); ctx.stroke();
      ctx.globalAlpha = dim;
      ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = 11; ctx.lineCap = "round";
      ctx.arc(C, C, r, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * Math.min(pct || 0, 1)); ctx.stroke();
      ctx.globalAlpha = 1;
    };
    ring(70, steps / g.steps, PALETTE.accent);
    ring(54, cal / g.calories, PALETTE.warn);
    ring(38, dist / g.distance, PALETTE.ok);

    const txt = (s, y, size, color, weight = 600) => {
      ctx.fillStyle = color; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.font = `${weight} ${size}px ui-monospace, "SF Mono", Menlo, monospace`;
      ctx.fillText(s, C, y);
    };
    txt(steps.toLocaleString(), C - 4, 34, "#fff", 700);
    txt("STEPS", C + 16, 9, "#8b98a9", 600);
    txt(`♥ ${hr || "—"}   ${Math.round(cal)} kcal`, C + 40, 10, "#8b98a9", 500);
  }

  _fmt(id, unit = "") {
    const v = this._s(id);
    if (v == null || ["unknown", "unavailable"].includes(v)) return "—";
    const n = parseFloat(v);
    return Number.isFinite(n) ? `${Math.round(n)}${unit}` : this._esc(v);
  }
  _esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  _css() {
    return `
      ha-card{padding:14px 16px;border-radius:16px}
      .watch{padding:4px 0}
      .head{display:flex;align-items:center;gap:10px;cursor:pointer;border-radius:10px;padding:4px}
      .head:focus-visible{outline:2px solid ${PALETTE.accent};outline-offset:2px}
      .head .dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto}
      .dot.live{background:${PALETTE.ok};box-shadow:0 0 0 3px ${PALETTE.ok}22}
      .dot.off{background:var(--disabled-text-color,#7e8ba3)}
      .id{flex:1;min-width:0}
      .id b{display:block;font-size:16px;font-weight:640;color:var(--primary-text-color);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      .id .meta{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;color:var(--secondary-text-color)}
      .badges{display:flex;gap:6px;flex:0 0 auto}
      .pill{font-family:ui-monospace,Menlo,monospace;font-size:10px;letter-spacing:.06em;text-transform:uppercase;padding:3px 8px;border-radius:999px;font-weight:600}
      .pill.on{color:${PALETTE.accent};background:${PALETTE.accent}1a}
      .pill.gone{color:var(--secondary-text-color);background:var(--divider-color)}
      .body{display:flex;gap:18px;align-items:center;margin-top:12px;flex-wrap:wrap}
      .face{border-radius:50%;box-shadow:0 18px 34px -18px #000a, inset 0 0 0 3px #05070a;flex:0 0 auto}
      .note-face{width:180px;height:180px;border-radius:50%;flex:0 0 auto;display:flex;flex-direction:column;
        align-items:center;justify-content:center;gap:4px;background:radial-gradient(circle at 50% 38%, #12161d, #05070a);
        box-shadow:0 18px 34px -18px #000a, inset 0 0 0 3px #05070a;border:1px solid var(--divider-color)}
      .note-face .glyph{font-size:56px;line-height:1;filter:grayscale(.2) opacity(.9)}
      .note-face .note-cap{font-family:ui-monospace,Menlo,monospace;font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--secondary-text-color)}
      .side{flex:1 1 170px;min-width:160px;display:grid;grid-template-columns:1fr 1fr;gap:8px 14px}
      .stat{display:flex;flex-direction:column;gap:1px}
      .stat .k{font-family:ui-monospace,Menlo,monospace;font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--secondary-text-color)}
      .stat .v{font-family:ui-monospace,Menlo,monospace;font-size:16px;font-weight:600;color:var(--primary-text-color);font-variant-numeric:tabular-nums}
      .stat .v.spo2{color:${PALETTE.spo2}} .stat .v.heart{color:${PALETTE.heart}}
      .foot-note{margin:12px 2px 0;font-size:12px;line-height:1.4;color:var(--secondary-text-color)}
      .foot-note code{font-family:ui-monospace,Menlo,monospace;font-size:11px}
      .ctl{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
      .b{font:inherit;font-size:13px;font-weight:600;padding:8px 12px;border-radius:10px;cursor:pointer;display:inline-flex;align-items:center;gap:6px;
         border:1px solid var(--divider-color);background:var(--secondary-background-color);color:var(--primary-text-color);
         transition:border-color .15s,transform .06s}
      .b .ico{font-size:14px}
      .b:hover{border-color:${PALETTE.accent}} .b:active{transform:translateY(1px)}
      .b:focus-visible{outline:2px solid ${PALETTE.accent};outline-offset:2px}
      .b[disabled]{opacity:.4;cursor:not-allowed}
      .b.prim{background:${PALETTE.accent}1a;border-color:${PALETTE.accent};color:${PALETTE.accent}}
    `;
  }
}

customElements.define("gtx2-watch-card", Gtx2WatchCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "gtx2-watch-card",
  name: "GTX2 Watch Card",
  description: "Live round-watch render + per-watch SAFE controls for the Starmax GTX2.",
  preview: true,
});
console.info("%c GTX2-WATCH-CARD %c three-watch gtx2.* ", "background:#7aa2f7;color:#0e1217;font-weight:700", "color:#7aa2f7");
