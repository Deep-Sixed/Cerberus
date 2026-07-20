/* Cerberus admin console — read + live-probe only.
   All data enters the DOM via textContent/createElement (never innerHTML), and
   every network call is a same-origin GET through getJSON(url), or the CSRF'd
   same-origin probe POST. No config mutation from here: the lifecycle stays
   git + the /admin API. */
"use strict";

const ENDPOINTS = {
  health: "/health",
  status: "/admin/status",
  config: "/admin/config/active",
  events: "/admin/events",
  providers: "/admin/providers",
};
const PROVIDERS_BASE = "/admin/providers";

async function getJSON(url) {
  const response = await fetch(url); // data reads are plain GETs
  if (!response.ok) throw new Error(url + " " + response.status);
  return response.json();
}

// The provider probe spends the provider's quota, so it is an operational POST
// carrying a custom header — a cross-site form/img cannot produce either.
async function probeProvider(url) {
  const response = await fetch(url, { method: "POST", headers: { "X-Cerberus-CSRF": "1" } });
  if (!response.ok) throw new Error(url + " " + response.status);
  return response.json();
}

const text = (v) => String(v ?? "—");

function el(tag, opts, ...kids) {
  const n = document.createElement(tag);
  if (opts) {
    if (opts.class) n.className = opts.class;
    if (opts.text !== undefined) n.textContent = opts.text;
    if (opts.onClick) n.addEventListener("click", opts.onClick);
    if (opts.attrs) for (const [k, v] of Object.entries(opts.attrs)) n.setAttribute(k, v);
  }
  for (const k of kids) if (k != null) n.append(k);
  return n;
}

// document.createElement can't produce valid <svg>/<circle> (wrong namespace) —
// the sidebar icons work because they're parsed from static HTML; anything built
// at runtime needs createElementNS. Same stroke-width/viewBox convention as the
// sidebar icons, so icon-only controls built in JS don't visually fork from them.
const SVG_NS = "http://www.w3.org/2000/svg";
function svgIcon(viewBox, shapes) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "nicon");
  svg.setAttribute("viewBox", viewBox);
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.6");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("aria-hidden", "true");
  for (const [tag, attrs] of shapes) {
    const shape = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) shape.setAttribute(k, v);
    svg.append(shape);
  }
  return svg;
}

// ---- state ----
let STATE = { health: {}, status: {}, config: {}, events: [], providers: [] };

async function refresh() {
  try {
    const [health, status, config, events, providers] = await Promise.all([
      getJSON(ENDPOINTS.health), getJSON(ENDPOINTS.status), getJSON(ENDPOINTS.config),
      getJSON(ENDPOINTS.events), getJSON(ENDPOINTS.providers),
    ]);
    STATE = { health, status, config, events: events.events || [], providers: providers.providers || [] };
    renderTop();
    renderActive();
  } catch (err) {
    document.getElementById("top-status").replaceChildren(el("span", { class: "pill bad", text: "unreachable" }));
  }
}

function renderTop() {
  const { health, status, events } = STATE;
  const fusion = status.fusion ? status.fusion.state : "unknown";
  document.getElementById("side-health").replaceChildren(
    el("span", { class: health.status === "ok" ? "pill good" : "pill bad", text: text(health.status) })
  );
  document.getElementById("top-status").replaceChildren(
    el("span", { class: "pill accent", text: "cfg " + text(health.config_version) }),
    el("span", { class: "pill", text: "release " + text(status.release_id) }),
    el("span", { class: fusion === "healthy" ? "pill good" : "pill", text: "fusion: " + fusion })
  );
  // the most recent routing event's own timestamp — not a client-side "now"
  const latest = (events || [])[0];
  document.getElementById("last-activity").textContent = latest && latest.timestamp
    ? "Latest activity " + new Date(latest.timestamp).toLocaleString()
    : "";
}

// ---- views ----
const VIEWS = { overview: renderOverview, providers: renderProviders, routing: renderRouting, events: renderEvents };
let ACTIVE = "overview";

function renderActive() { VIEWS[ACTIVE](); }

// Attention tiles — every value traces to a field already fetched this
// refresh; nothing here is inferred or fabricated. Status lives in the
// tile's top-border tone, never a full background wash.
function computeAttention() {
  const { health, status, providers, events } = STATE;
  const cooled = (health.cooldowns || []).length;
  const unconfigured = providers.filter((p) => !p.configured).length;
  const failed = events.filter((e) => e.outcome !== "success").length;
  const fusionReady = status.fusion && status.fusion.state === "configured";
  return [
    tile("Providers cooled down", cooled, cooled ? "throttled, next candidate takes over" : "all providers live", cooled ? "warn" : "good"),
    tile("Unconfigured providers", unconfigured, unconfigured ? "missing an api key" : "every provider has a key", unconfigured ? "danger" : "good"),
    tile("Failed routing", failed, `of ${events.length} recent events`, failed ? "warn" : "good"),
    tile("Fusion worker", fusionReady ? "Ready" : "Off", `${((status.fusion && status.fusion.aliases) || []).length} alias(es)`, fusionReady ? "good" : "info"),
  ];
}

// A thin colored-segment strip of the most recent routing outcomes, oldest
// to newest left-to-right — real event data, capped to what's fetched.
function renderOutcomeStrip(events) {
  if (!events.length) return el("p", { class: "muted", text: "No routing events yet." });
  const recent = events.slice(0, 30).slice().reverse();
  const strip = el("div", { class: "stripchart" });
  for (const e of recent) {
    strip.append(el("div", {
      class: "seg " + (e.outcome === "success" ? "good" : "bad"),
      attrs: { title: `${e.outcome} · ${text(e.alias)}` },
    }));
  }
  return strip;
}

function renderOverview() {
  const v = document.getElementById("view-overview");
  const { config, providers, events, health } = STATE;
  const aliasCount = Object.keys(config.aliases || {}).length;
  const configured = providers.filter((p) => p.configured).length;
  const cooled = providers.filter((p) => p.cooled_down).length;

  const head = el("div", { class: "pagehead-extra" },
    el("p", { class: "muted",
      text: `${configured}/${providers.length} providers configured · ${cooled} cooled down · ${aliasCount} alias(es) · ${events.length} recent events` }),
    el("button", { class: "btn-ghost", onClick: refresh, attrs: { title: "Refresh now", "aria-label": "Refresh now" } },
      svgIcon("0 0 18 18", [["circle", { cx: "9", cy: "9", r: "6", "stroke-dasharray": "26 12" }]]))
  );

  const summaryTiles = el("div", { class: "tiles" }, ...computeAttention());

  const stripSection = el("div", null,
    el("h2", { class: "section", text: "Recent routing outcomes" }),
    renderOutcomeStrip(events)
  );

  const cooldowns = (health.cooldowns || []);
  const cdSection = el("div", null,
    el("h2", { class: "section", text: "Active cooldowns" }),
    cooldowns.length
      ? table(["Scope", "Target", "Reason", "Remaining"], cooldowns.map((c) => [
          text(c.scope), codeText(`${c.provider}/${c.credential || ""}/${c.model || ""}`),
          text(c.reason), Math.round(c.seconds_remaining) + "s"])
        )
      : el("p", { class: "muted", text: "None — every provider is live." })
  );
  v.replaceChildren(head, summaryTiles, stripSection, cdSection);
}

// tone: 'good' | 'warn' | 'danger' | 'info' — colors the tile's top border,
// same vocabulary as .pill's good/warn/bad/accent
function tile(label, n, sub, tone) {
  return el("div", { class: "tile" + (tone ? " " + tone : "") },
    el("small", { text: label }), el("div", { class: "n", text: String(n) }),
    el("small", { text: sub }));
}

function renderProviders() {
  const v = document.getElementById("view-providers");
  const grid = el("div", { class: "grid" });
  for (const p of STATE.providers) grid.append(providerCard(p));
  v.replaceChildren(grid);
}

// Deliberately text-only, no percentage bar: a 429's *applied* cooldown is
// min(upstream Retry-After, quota_cooldown_seconds) (dispatch.py), and the
// admin API only exposes the configured ceiling, not what was actually
// applied — a bar computed against the ceiling would often render near-full
// for a cooldown that just started. seconds_remaining itself is exact.
function cooldownNote(providerName) {
  const cd = (STATE.health.cooldowns || []).find((c) => c.provider === providerName);
  if (!cd) return null;
  return el("small", { text: `${Math.round(cd.seconds_remaining)}s remaining · ${text(cd.reason)}` });
}

function providerCard(p) {
  const badge = !p.configured
    ? el("span", { class: "pill bad", text: "missing key" })
    : p.cooled_down
      ? el("span", { class: "pill warn", text: "cooled down" })
      : el("span", { class: "pill good", text: "configured" });
  const models = el("div", { class: "models" });
  for (const m of p.models) {
    models.append(el("span", { class: m.cost_tier === "paid" ? "tag paid" : "tag", text: m.id }));
  }
  const probe = el("span", { class: "probe muted", text: "" });
  const testBtn = el("button", {
    class: "btn", text: "Test",
    onClick: async () => {
      probe.textContent = "testing…";
      probe.className = "probe muted";
      try {
        const r = await probeProvider(PROVIDERS_BASE + "/" + encodeURIComponent(p.name) + "/test");
        if (r.ok) { probe.textContent = `ok ${r.status} · ${r.latency_ms}ms (${r.probe})`; probe.className = "probe ok"; }
        else { probe.textContent = "failed: " + text(r.reason || r.status); probe.className = "probe badc"; }
      } catch (e) { probe.textContent = "error"; probe.className = "probe badc"; }
    },
  });
  const parts = [
    el("h3", null, el("span", { text: p.name }), badge),
    el("div", { class: "kv" }, codeText(p.base_url)),
    el("div", { class: "kv", text: "key: " + p.credential_envs.join(", ") }),
    models,
  ];
  const note = p.cooled_down ? cooldownNote(p.name) : null;
  if (note) parts.push(note);
  parts.push(el("div", null, testBtn, probe));
  return el("div", { class: "card" }, ...parts);
}

function renderRouting() {
  const v = document.getElementById("view-routing");
  const aliases = STATE.config.aliases || {};
  const rows = Object.entries(aliases).map(([name, a]) => {
    const chain = el("div", { class: "chain" });
    (a.candidates || []).forEach((c, i) => {
      if (i > 0) chain.append(el("span", { class: "arrow", text: "→" }));
      chain.append(el("code", { text: `${c.provider}/${c.model}` }));
    });
    return [codeText(name), pillMode(a.mode), chain];
  });
  v.replaceChildren(
    el("h2", { class: "section", text: "Aliases & failover chains" }),
    table(["Alias", "Mode", "Candidates (failover order)"], rows)
  );
}

function pillMode(mode) {
  const cls = mode === "free" ? "pill good" : mode === "fusion" ? "pill accent" : "pill";
  return el("span", { class: cls, text: text(mode) });
}

function renderEvents() {
  const v = document.getElementById("view-events");
  const rows = STATE.events.map((e) => [
    el("span", { class: e.outcome === "success" ? "ok" : "warnc", text: text(e.outcome) }),
    text(e.identity), codeText(text(e.alias)),
    codeText(e.provider ? `${e.provider}/${e.model}` : "—"),
    text((e.exclusions || []).length),
    e.latency_ms != null ? Math.round(e.latency_ms) + "ms" : "—",
    codeText(text(e.config_version)),
  ]);
  v.replaceChildren(
    el("h2", { class: "section", text: "Recent routing events (newest first)" }),
    rows.length
      ? table(["Outcome", "Identity", "Alias", "Selected", "Excl.", "Latency", "Config"], rows)
      : el("p", { class: "muted", text: "No events yet." })
  );
}

// ---- helpers ----
function codeText(s) { return el("code", { text: text(s) }); }
function table(headers, rows) {
  const thead = el("thead", null, el("tr", null, ...headers.map((h) => el("th", { text: h }))));
  const tbody = el("tbody");
  for (const r of rows) tbody.append(el("tr", null, ...r.map((c) => el("td", null, typeof c === "object" ? c : String(c)))));
  return el("table", null, thead, tbody);
}

// ---- nav ----
function show(view) {
  ACTIVE = view;
  document.querySelectorAll("#side a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  document.querySelectorAll(".view").forEach((s) => (s.hidden = s.id !== "view-" + view));
  document.getElementById("title").textContent =
    { overview: "Overview", providers: "Providers", routing: "Model Routing", events: "Events" }[view];
  renderActive();
}
document.querySelectorAll("#side a").forEach((a) => a.addEventListener("click", () => show(a.dataset.view)));

refresh();
setInterval(refresh, 5000);
