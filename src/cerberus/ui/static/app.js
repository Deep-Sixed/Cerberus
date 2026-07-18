/* Cerberus admin console — read + live-probe only.
   All data enters the DOM via textContent/createElement (never innerHTML), and
   every network call is a same-origin GET through getJSON(url). No config
   mutation from here: the lifecycle stays git + the /admin API. */
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
  const response = await fetch(url); // GET only — no method/body options anywhere
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
  const { health, status } = STATE;
  const fusion = status.fusion ? status.fusion.state : "unknown";
  document.getElementById("side-health").replaceChildren(
    el("span", { class: health.status === "ok" ? "pill good" : "pill bad", text: text(health.status) })
  );
  document.getElementById("top-status").replaceChildren(
    el("span", { class: "pill accent", text: "cfg " + text(health.config_version) }),
    el("span", { class: "pill", text: "release " + text(status.release_id) }),
    el("span", { class: fusion === "healthy" ? "pill good" : "pill", text: "fusion: " + fusion })
  );
}

// ---- views ----
const VIEWS = { overview: renderOverview, providers: renderProviders, routing: renderRouting, events: renderEvents };
let ACTIVE = "overview";

function renderActive() { VIEWS[ACTIVE](); }

function renderOverview() {
  const v = document.getElementById("view-overview");
  const { config, providers, events, health } = STATE;
  const aliasCount = Object.keys(config.aliases || {}).length;
  const configured = providers.filter((p) => p.configured).length;
  const cooled = providers.filter((p) => p.cooled_down).length;
  const tiles = el("div", { class: "tiles" },
    tile("Providers", providers.length, `${configured} configured`),
    tile("Aliases", aliasCount, "routing policies"),
    tile("Cooled down", cooled, cooled ? "provider(s) throttled" : "all live", cooled ? "warnc" : "ok"),
    tile("Events", events.length, "recent routing")
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
  v.replaceChildren(tiles, cdSection);
}

function tile(label, n, sub, cls) {
  return el("div", { class: "tile" },
    el("small", { text: label }), el("div", { class: "n" + (cls ? " " + cls : ""), text: String(n) }),
    el("small", { text: sub }));
}

function renderProviders() {
  const v = document.getElementById("view-providers");
  const grid = el("div", { class: "grid" });
  for (const p of STATE.providers) grid.append(providerCard(p));
  v.replaceChildren(grid);
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
        const r = await getJSON(PROVIDERS_BASE + "/" + encodeURIComponent(p.name) + "/test");
        if (r.ok) { probe.textContent = `ok ${r.status} · ${r.latency_ms}ms`; probe.className = "probe ok"; }
        else { probe.textContent = "failed: " + text(r.reason || r.status); probe.className = "probe badc"; }
      } catch (e) { probe.textContent = "error"; probe.className = "probe badc"; }
    },
  });
  return el("div", { class: "card" },
    el("h3", null, el("span", { text: p.name }), badge),
    el("div", { class: "kv" }, codeText(p.base_url)),
    el("div", { class: "kv", text: "key: " + p.credential_envs.join(", ") }),
    models,
    el("div", null, testBtn, probe));
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
