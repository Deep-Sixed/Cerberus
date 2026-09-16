/* Cerberus admin console — read + live-probe, plus one narrow write path.
   All data enters the DOM via textContent/createElement (never innerHTML).
   Reads are plain same-origin GETs through getJSON(url). The only writes are
   the provider probe and the config editor's stage/validate/activate loop —
   both funnel through the single CSRF'd postJSON(url, body) below, an
   allow-listed subset of operational fields (never secrets, never identity,
   access-control, or server bindings — the backend enforces that boundary). */
"use strict";

const ENDPOINTS = {
  // diagnostics live behind the admin boundary; public /health is liveness only
  health: "/admin/health",
  status: "/admin/status",
  config: "/admin/config/active",
  schema: "/admin/config/schema",
  events: "/admin/events",
  providers: "/admin/providers",
  stage: "/admin/config/stage",
  validate: "/admin/validate",
  activate: "/admin/activate",
};
const PROVIDERS_BASE = "/admin/providers";

async function getJSON(url) {
  const response = await fetch(url); // data reads are plain GETs
  if (!response.ok) throw new Error(url + " " + response.status);
  return response.json();
}

// Every write (provider probe, stage, validate, activate) goes through this
// one function — an operational POST carrying a custom header a cross-site
// form/img cannot produce. Never throws on a non-2xx: callers need the JSON
// body even on a 422 (it carries {"...": false, "error": "..."}).
async function postJSON(url, body) {
  const options = { method: "POST", headers: { "X-Cerberus-CSRF": "1" } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(url, options);
  return { ok: response.ok, status: response.status, json: await response.json() };
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
    // fusion_status() emits "configured" / "not_configured" — never "healthy"
    el("span", { class: fusion === "configured" ? "pill good" : "pill", text: "fusion: " + fusion })
  );
  // the most recent routing event's own timestamp — not a client-side "now"
  const latest = (events || [])[0];
  document.getElementById("last-activity").textContent = latest && latest.timestamp
    ? "Latest activity " + new Date(latest.timestamp).toLocaleString()
    : "";
}

// ---- views ----
// "config" is deliberately not in VIEWS: it owns fields the user may be
// mid-editing, so the 5s auto-refresh must never blindly re-render it (that
// would silently wipe unsaved input). It loads once on entry (loadConfigView)
// and again only after a successful Apply.
const VIEWS = { overview: renderOverview, providers: renderProviders, routing: renderRouting, events: renderEvents };
let ACTIVE = "overview";

function renderActive() {
  if (ACTIVE === "config") return;
  VIEWS[ACTIVE]();
}

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
    tile("Fusion backend", fusionReady ? "Ready" : "Off", `${((status.fusion && status.fusion.aliases) || []).length} alias(es)`, fusionReady ? "good" : "info"),
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
        const r = await postJSON(PROVIDERS_BASE + "/" + encodeURIComponent(p.name) + "/test");
        if (!r.ok) { probe.textContent = "error " + r.status; probe.className = "probe badc"; return; }
        const body = r.json; // the probe's own outcome, distinct from the HTTP status
        if (body.ok) { probe.textContent = `ok ${body.status} · ${body.latency_ms}ms (${body.probe})`; probe.className = "probe ok"; }
        else { probe.textContent = "failed: " + text(body.reason || body.status); probe.className = "probe badc"; }
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

// ---- config editor ----
// Narrow, allow-listed field editing (provider cooldown windows, health-probe
// mode). The allow-list is enforced server-side (admin_fields.py); this view
// only renders whatever the schema endpoint actually returns, so it can never
// offer an edit the backend would reject. Secrets are shown, locked: Cerberus
// runs in a container with no path to the KeePassXC vault, so there is no
// write mechanism this form could honestly wire up for them.

async function loadConfigView() {
  setConfigMessage("");
  try {
    const schema = await getJSON(ENDPOINTS.schema);
    renderConfigFields(schema);
    updateDirtyState();
  } catch (e) {
    setConfigMessage("Could not load config schema", "error");
  }
}

function renderConfigFields(schema) {
  const v = document.getElementById("view-config");
  const bySection = new Map(schema.sections.map((s) => [s.id, []]));
  for (const f of schema.fields) {
    if (!bySection.has(f.section)) bySection.set(f.section, []);
    bySection.get(f.section).push(f);
  }
  const sections = schema.sections.map((section) => {
    const grid = el("div", { class: "field-grid" });
    for (const f of bySection.get(section.id) || []) grid.append(renderField(f));
    return el("div", { class: "settings-section" },
      el("div", { class: "section-heading" }, el("h3", { text: section.label }), el("p", { text: section.description })),
      grid);
  });
  v.replaceChildren(...sections);
}

function renderField(f) {
  const label = el("label", null, el("span", { text: f.label }));
  if (f.locked) label.append(el("span", { class: "field-source", text: "locked" }));
  const input = fieldInput(f);
  input.id = "field-" + f.key;
  input.dataset.key = f.key;
  input.dataset.original = String(f.value);
  input.disabled = f.locked;
  input.addEventListener("input", updateDirtyState);
  input.addEventListener("change", updateDirtyState);
  const wrap = el("div", { class: "field" }, label, input);
  if (f.description) wrap.append(el("div", { class: "field-description", text: f.description }));
  return wrap;
}

function fieldInput(f) {
  if (f.type === "select") {
    const select = el("select");
    for (const opt of f.options || []) select.append(el("option", { text: opt, attrs: { value: opt } }));
    select.value = f.value;
    return select;
  }
  const input = el("input", { attrs: { type: f.type === "number" ? "number" : "text" } });
  input.value = f.value ?? "";
  return input;
}

function changedFieldValues() {
  const values = {};
  document.querySelectorAll("#view-config [data-key]").forEach((input) => {
    if (input.disabled) return;
    if (input.value !== input.dataset.original) values[input.dataset.key] = input.value;
  });
  return values;
}

// Recomputed on every field input, so it must double as the in-flight lock —
// otherwise editing a field while Apply's request is outstanding re-enables
// the button (count > 0 again) and a second, overlapping apply can fire.
let APPLYING = false;

function updateDirtyState() {
  const count = Object.keys(changedFieldValues()).length;
  document.getElementById("dirtyState").textContent =
    count === 0 ? "No changes" : `${count} unsaved change${count === 1 ? "" : "s"}`;
  document.getElementById("applyButton").disabled = count === 0 || APPLYING;
}

function setConfigMessage(message, kind) {
  const area = document.getElementById("configMessage");
  area.textContent = message;
  area.className = "message-area" + (kind ? " " + kind : "");
}

// stage (build + write a candidate) -> validate (bind version to checksum,
// the immutability guarantee) -> [activate]. Reused identically by both
// buttons; Apply just takes the extra activate step validate stops short of.
async function stageAndValidate(updates) {
  const staged = await postJSON(ENDPOINTS.stage, { updates });
  if (!staged.ok) return { ok: false, message: staged.json.error || `Could not stage (${staged.status})` };
  const validated = await postJSON(ENDPOINTS.validate, { path: staged.json.path });
  if (!validated.ok || !validated.json.valid) {
    return { ok: false, message: validated.json.error || "Invalid" };
  }
  return { ok: true, path: staged.json.path, version: staged.json.version };
}

async function validateConfig() {
  const updates = changedFieldValues();
  if (!Object.keys(updates).length) { setConfigMessage("No changes to validate", ""); return; }
  try {
    const result = await stageAndValidate(updates);
    setConfigMessage(
      result.ok ? `Valid — would become ${result.version}` : result.message,
      result.ok ? "ok" : "error"
    );
  } catch (e) {
    setConfigMessage("Network error — could not reach server", "error");
  }
}

async function applyConfig() {
  const updates = changedFieldValues();
  if (!Object.keys(updates).length || APPLYING) return;
  APPLYING = true;
  document.getElementById("applyButton").disabled = true;
  try {
    const staged = await stageAndValidate(updates);
    if (!staged.ok) { setConfigMessage(staged.message, "error"); return; }
    const activated = await postJSON(ENDPOINTS.activate, { path: staged.path });
    if (!activated.ok) { setConfigMessage(activated.json.error || "Could not activate", "error"); return; }
    await loadConfigView(); // fresh values from the now-active config, dirty state clears
    await refresh(); // reflect the new config_version in the topbar and other views
    setConfigMessage(`Applied — now ${activated.json.active_version}`, "ok"); // after reload — loadConfigView() clears the message area first
  } catch (e) {
    setConfigMessage("Network error — apply failed", "error");
  } finally {
    APPLYING = false;
    updateDirtyState();
  }
}

// ---- nav ----
function show(view) {
  // re-clicking Config while already there, mid-edit, would otherwise silently
  // discard unsaved input via the unconditional loadConfigView() call below
  if (view === "config" && ACTIVE === "config" && Object.keys(changedFieldValues()).length) return;
  ACTIVE = view;
  document.querySelectorAll("#side a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  document.querySelectorAll(".view").forEach((s) => (s.hidden = s.id !== "view-" + view));
  document.getElementById("title").textContent =
    { overview: "Overview", providers: "Providers", routing: "Model Routing", events: "Events", config: "Config" }[view];
  document.getElementById("config-actionbar").hidden = view !== "config";
  if (view === "config") loadConfigView();
  else renderActive();
}
document.querySelectorAll("#side a").forEach((a) => a.addEventListener("click", () => show(a.dataset.view)));
document.getElementById("validateButton").addEventListener("click", validateConfig);
document.getElementById("applyButton").addEventListener("click", applyConfig);

refresh();
setInterval(refresh, 5000);
