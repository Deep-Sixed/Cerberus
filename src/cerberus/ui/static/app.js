/* Cerberus console — the frozen console shell.

   All data enters the DOM via textContent/createElement (never innerHTML).
   Reads are plain same-origin GETs through getJSON(url). The only writes are
   the provider probe and the candidate loop (stage/validate/activate) — both
   funnel through the single CSRF'd postJSON(url, body) below, an allow-listed
   subset of operational fields (never secrets, never identity, access-control,
   or server bindings — the backend enforces that boundary).

   Console state is derived, never stored as a lifecycle flag: `operator_activated`
   from /admin/status answers "has an operator taken charge of a revision".
   Cerberus always boots with an active revision and routes from it, so the
   first-run experience is about establishing the first operator-managed
   revision — not about a gateway that cannot serve.

   Route state is not derived here at all. Eligibility, policy exclusion, health,
   cooldown and the fusion chain are routing decisions, so they arrive already
   decided from /admin/routes; this file labels them and never re-derives one. */
"use strict";

const ENDPOINTS = {
  // diagnostics live behind the admin boundary; public /health is liveness only
  health: "/admin/health",
  status: "/admin/status",
  config: "/admin/config/active",
  schema: "/admin/config/schema",
  events: "/admin/events",
  providers: "/admin/providers",
  // every route state on the Routes screen comes from here already decided:
  // eligibility, policy exclusion, health, cooldown and the fusion chain are
  // routing decisions, and a browser that re-derived them would be a second
  // router free to disagree with the real one
  routes: "/admin/routes",
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

// ---- element helpers ----

const text = (v) => String(v ?? "—");

function el(tag, opts, ...kids) {
  const n = document.createElement(tag);
  if (opts) {
    if (opts.class) n.className = opts.class;
    if (opts.text !== undefined) n.textContent = opts.text;
    if (opts.onClick) n.addEventListener("click", opts.onClick);
    if (opts.onInput) n.addEventListener("input", opts.onInput);
    if (opts.attrs) for (const [k, v] of Object.entries(opts.attrs)) n.setAttribute(k, v);
    if (opts.props) for (const [k, v] of Object.entries(opts.props)) n[k] = v;
  }
  for (const k of kids) if (k != null) n.append(k);
  return n;
}

const codeText = (v, cls) => el("code", { text: text(v), class: cls });

function table(headers, rows) {
  return el(
    "table",
    null,
    el("thead", null, el("tr", null, ...headers.map((h) => el("th", { text: h })))),
    el("tbody", null, ...rows.map((r) => el("tr", null, ...r.map((c) => el("td", null, typeof c === "object" && c !== null ? c : document.createTextNode(text(c))))))),
  );
}

function short(checksum) {
  const s = text(checksum);
  return s.startsWith("sha256:") ? s.slice(0, 15) : s;
}

function basename(path) {
  const s = text(path);
  const cut = s.lastIndexOf("/");
  return cut === -1 ? s : s.slice(cut + 1);
}

// ---- state ----

let STATE = { health: {}, status: {}, config: {}, schema: {}, events: [], providers: [], routes: {} };
// which route path the inspector is describing; never a routing decision
let SELECTION = { alias: null, ordinal: null };
let VIEW = "health";
// the candidate loop's own progress — not a lifecycle flag, just what this
// console session has done to the candidate it is looking at
let FLOW = { path: null, validated: false, checksum: null, edits: {}, message: "", tone: "", busy: false };

const NAV = [
  ["aliases", "Aliases"],
  ["routes", "Routes"],
  ["fusion", "Fusion"],
  ["providers", "Providers"],
  ["health", "Health"],
  ["audit", "Audit"],
];

const VIEW_CHROME = {
  aliases: ["Aliases", "Stable addresses callers bind to — policy, pinned path, eligibility.", "GET /admin/config/active"],
  routes: ["Routes", "Where each alias can go, and why a route path is eligible or unavailable right now.", "GET /admin/routes"],
  fusion: ["Fusion", "Cerberus holds the policy; the backend holds the deliberation.", "GET /admin/status"],
  providers: ["Providers", "Credential presence, cooldown windows, live probe.", "GET /admin/providers"],
  health: ["Health", "Liveness, telemetry delivery and cooldown state behind the admin boundary.", "GET /admin/health"],
  audit: ["Audit", "Revision registrations and activations.", "no endpoint"],
};

// Derived, never a stored lifecycle flag. Whether anything has been routed since
// boot is the other half of the console's state model, but nothing reads it until
// decision visibility lands, so it is not carried here unused.
const operatorActivated = () => STATE.status.operator_activated === true;

function aliasEntries() {
  return Object.entries(STATE.config.aliases || {});
}

// Whether an alias can be routed to right now is a routing decision, so it is
// read from the projection and never recomputed here. Credential presence was
// the wrong proxy for it: a provider can hold a valid credential and still be
// health-excluded, cooled down, or prohibited by cost policy, and a fusion alias
// can have every credential in place and no reachable backend.
function aliasRoutable(entry) {
  if (entry.fusion) return entry.fusion.readiness.available === true;
  return (entry.paths || []).some((path) => path.state === "eligible" || path.state === "standby");
}

function routableCount() {
  const aliases = projectedAliases();
  return { routable: aliases.filter(aliasRoutable).length, total: aliases.length };
}

function candidatePathCount() {
  return aliasEntries().reduce((n, [, a]) => n + (a.candidates || []).length, 0);
}

// health_snapshot() emits exactly four: disabled (no sink at all), degraded
// (delivery failing), healthy (delivered at least once), pending (a sink is
// configured but nothing has been delivered yet). Anything else is unreachable.
function telemetryState() {
  const t = STATE.health.telemetry || {};
  const status = text(t.status);
  if (status === "degraded") {
    return {
      label: t.last_status_code ? "degraded · http_" + t.last_status_code : "degraded",
      tone: "t-warn",
    };
  }
  if (status === "healthy") return { label: "healthy", tone: "t-up" };
  if (status === "pending") return { label: "pending", tone: "t-mono" };
  return { label: "disabled", tone: "t-mono" };
}

// ---- data ----

async function refresh() {
  try {
    const [health, status, config, schema, events, providers, routes] = await Promise.all([
      getJSON(ENDPOINTS.health), getJSON(ENDPOINTS.status), getJSON(ENDPOINTS.config),
      getJSON(ENDPOINTS.schema), getJSON(ENDPOINTS.events), getJSON(ENDPOINTS.providers),
      getJSON(ENDPOINTS.routes),
    ]);
    STATE = {
      health, status, config, schema, routes,
      events: events.events || [],
      providers: providers.providers || [],
    };
    render();
  } catch (err) {
    document.getElementById("revisionValue").textContent = "unreachable";
    document.getElementById("revisionValue").className = "t-bad";
  }
}

// ---- header ----

function renderHeader() {
  const active = STATE.status.active || {};
  const live = operatorActivated();
  const server = (STATE.config.server) || {};
  const { routable, total } = routableCount();

  const label = document.getElementById("revisionLabel");
  label.textContent = live ? "Active revision" : "Active revision · bootstrap";

  const value = document.getElementById("revisionValue");
  value.textContent = text(active.version);
  value.className = live ? "t-ink" : "t-warn";

  document.getElementById("revisionChecksum").textContent = short(active.checksum);

  document.getElementById("gatewayValue").textContent =
    text(server.host) + ":" + text(server.port) + " · release " + text(STATE.status.release_id);

  const routableEl = document.getElementById("routableValue");
  routableEl.textContent = routable + " of " + total + (total === 1 ? " alias" : " aliases");
  routableEl.className = "fact-plain " + (routable === 0 ? "t-bad" : routable < total ? "t-warn" : "t-up");

  const tel = telemetryState();
  const telEl = document.getElementById("telemetryValue");
  telEl.textContent = tel.label;
  telEl.className = tel.tone;
}

// ---- navigation ----

function navCount(id) {
  if (!operatorActivated()) return { label: "—", cls: "navcount navcount-dim" };
  if (id === "audit") return { label: "gap", cls: "navcount navcount-gap" };
  if (id === "aliases") return { label: String(aliasEntries().length), cls: "navcount" };
  if (id === "routes") return { label: String(projectedPathCount()), cls: "navcount" };
  if (id === "providers") return { label: String(STATE.providers.length), cls: "navcount" };
  if (id === "fusion") {
    const f = STATE.status.fusion || {};
    return { label: String((f.aliases || []).length), cls: "navcount" };
  }
  if (id === "health") return { label: "ok", cls: "navcount" };
  return { label: "—", cls: "navcount navcount-dim" };
}

function renderNav() {
  const live = operatorActivated();
  const items = NAV.map(([id, label]) => {
    const count = navCount(id);
    const locked = !live;
    const node = el(
      "button",
      {
        class: "navitem" + (locked ? " navitem-locked" : "") + (!locked && VIEW === id ? " navitem-on" : ""),
        attrs: { type: "button", id: "nav-" + id, "aria-current": !locked && VIEW === id ? "page" : "false" },
        props: locked ? { disabled: true } : {},
        onClick: locked ? undefined : () => { VIEW = id; render(); },
      },
      el("span", { class: "navlabel" }, el("span", { class: "navdot" }), el("span", { text: label })),
      el("code", { class: count.cls, text: count.label }),
    );
    return node;
  });
  document.getElementById("navlist").replaceChildren(...items);

  const identities = Object.entries(STATE.config.identities || {});
  document.getElementById("identityList").replaceChildren(
    ...(identities.length
      ? identities.map(([name, i]) =>
          el("div", { class: "identity" },
            codeText(name),
            el("small", { text: "allowed_modes: " + (i.allowed_modes || []).join(", ") }),
          ))
      : [el("small", { class: "muted", text: "none in revision" })]),
    el("small", { class: "muted", text: "unlisted callers: deny by default" }),
  );
}

// ---- first-run bring-up ----

function bringupStep(n, done, title, detail, hint, action) {
  const side = el("div", { class: "step-side" }, el("code", { class: "step-hint", text: hint }));
  if (action) side.append(action);
  return el(
    "div",
    { class: "step" + (done ? " step-done" : "") },
    el("code", { class: "step-mark" + (done ? " step-mark-done" : ""), text: done ? "✓" : String(n) }),
    el("div", null, el("div", { class: "step-title", text: title }), el("div", { class: "step-detail", text: detail })),
    side,
  );
}

function stepButton(label, onClick) {
  return el("button", {
    class: "step-action",
    attrs: { type: "button" },
    text: label,
    onClick,
    props: { disabled: FLOW.busy },
  });
}

async function runValidate(path) {
  FLOW.busy = true; FLOW.message = "Validating…"; FLOW.tone = ""; render();
  const r = await postJSON(ENDPOINTS.validate, { path });
  FLOW.busy = false;
  if (r.ok && r.json.valid) {
    FLOW.validated = true;
    FLOW.checksum = r.json.checksum;
    FLOW.message = "Valid · " + short(r.json.checksum);
    FLOW.tone = "is-up";
  } else {
    FLOW.validated = false;
    FLOW.message = text(r.json.error || r.json.reason || ("validate failed (" + r.status + ")"));
    FLOW.tone = "is-bad";
  }
  render();
}

async function runActivate(path) {
  FLOW.busy = true; FLOW.message = "Activating…"; FLOW.tone = ""; render();
  const r = await postJSON(ENDPOINTS.activate, { path });
  FLOW.busy = false;
  if (r.ok && r.json.activated) {
    FLOW = { path: null, validated: false, checksum: null, edits: {}, message: "Activated " + text(r.json.active_version), tone: "is-up", busy: false };
    await refresh();
    return;
  }
  FLOW.message = text(r.json.error || r.json.reason || ("activate failed (" + r.status + ")"));
  FLOW.tone = "is-bad";
  render();
}

function renderBringup() {
  const section = document.getElementById("bringup");
  if (operatorActivated()) {
    section.hidden = true;
    section.replaceChildren();
    return;
  }
  section.hidden = false;
  section.className = "bringup bringup-solo";  // nothing follows it during first run

  const active = STATE.status.active || {};
  const server = STATE.config.server || {};
  const path = active.source_path;
  const aliases = aliasEntries().length;
  const configuredCount = STATE.providers.filter((p) => p.configured).length;
  const validated = FLOW.validated && FLOW.path === path;

  const steps = [
    bringupStep(1, true, "Gateway started",
      "Cerberus is running from the bootstrap configuration. Review and activate it to establish your first operator-managed revision.",
      text(server.host) + ":" + text(server.port)),
    bringupStep(2, true, "Review the bootstrap configuration",
      aliases + (aliases === 1 ? " alias" : " aliases") + " · " + candidatePathCount() + " candidate paths · credentials present for " +
      configuredCount + " of " + STATE.providers.length + " providers.",
      basename(path)),
    bringupStep(3, validated, "Validate the bootstrap configuration",
      validated
        ? "Valid · " + short(FLOW.checksum) + " · version bound to its checksum."
        : "Binds the version to its checksum and resolves policy only — no network call is made.",
      "/admin/validate",
      validated ? null : stepButton("Validate configuration", () => { FLOW.path = path; runValidate(path); })),
    bringupStep(4, false, "Activate as your first operator-managed revision",
      "Activation swaps one current-revision pointer — the only moment routing changes. It ends first run: the console opens as soon as a revision is operator-managed, whether or not anything has been routed yet.",
      "/admin/activate",
      validated ? stepButton("Activate revision", () => runActivate(path)) : null),
  ];

  const facts = [
    ["Revision", text(active.version), "t-ink"],
    ["Aliases in config", String(aliases), "t-ink"],
    ["Candidate paths", String(candidatePathCount()), "t-ink"],
    ["Cooldown state", basename((STATE.config.state || {}).path) || "in-memory", "t-mono"],
    ["Telemetry", telemetryState().label, "t-mono"],
  ];

  section.replaceChildren(
    el("div", null,
      el("span", { class: "bringup-kicker", text: "Gateway booted · running the bootstrap configuration" }),
      el("h2", { text: "Bring the router up" }),
      el("p", { class: "bringup-note", text: "Routing is available according to the current bootstrap configuration." }),
      el("div", { class: "steps" }, ...steps),
      FLOW.message ? el("p", { class: "message-area " + FLOW.tone, text: FLOW.message }) : null,
    ),
    el("div", { class: "bootfacts" },
      el("span", { class: "fact-label", text: "Boot facts" }),
      el("div", { class: "bootfacts-rows" },
        ...facts.map(([k, v, tone]) => el("div", { class: "bootfact" }, el("span", { text: k }), codeText(v, tone)))),
      el("p", { class: "bootfacts-note", text: "Secrets are environment-variable names only. This console never reads or writes a credential value." }),
    ),
  );
}

// ---- candidate bar ----

function renderCandidateBar() {
  const bar = document.getElementById("candidateBar");
  const staged = FLOW.path !== null && operatorActivated();
  bar.hidden = !staged;
  if (!staged) return;

  document.getElementById("candidateState").textContent = FLOW.validated ? "Candidate validated" : "Candidate staged";
  document.getElementById("candidateDetail").textContent = basename(FLOW.path) + (FLOW.checksum ? " · " + short(FLOW.checksum) : "");

  const message = document.getElementById("candidateMessage");
  message.textContent = FLOW.message;
  message.className = "message-area " + FLOW.tone;

  const discard = document.getElementById("discardButton");
  const validate = document.getElementById("validateButton");
  const activate = document.getElementById("activateButton");
  discard.disabled = FLOW.busy;
  validate.disabled = FLOW.busy || FLOW.validated;
  activate.disabled = FLOW.busy || !FLOW.validated;
}

function wireCandidateBar() {
  document.getElementById("validateButton").addEventListener("click", () => { if (FLOW.path) runValidate(FLOW.path); });
  document.getElementById("activateButton").addEventListener("click", () => { if (FLOW.path) runActivate(FLOW.path); });
  // destructive: the staged candidate is abandoned. The staging directory sweeps
  // its own files, so this drops the console's hold on it and nothing else.
  document.getElementById("discardButton").addEventListener("click", () => {
    FLOW = { path: null, validated: false, checksum: null, edits: {}, message: "Candidate discarded.", tone: "", busy: false };
    render();
  });
}

// ---- views ----

function pending(title, body, gap) {
  return el("div", { class: "pending" + (gap ? " pending-gap" : "") },
    el("strong", { text: title }), el("p", { text: body }));
}

function viewAliases() {
  // Mode, candidate count and the cost gate are configuration, shown as the
  // revision writes them. State is not: it is joined to the same projection the
  // header counts, through the same predicate, so a row can never contradict
  // the count above it.
  const projected = {};
  for (const entry of projectedAliases()) projected[entry.alias] = entry;

  const rows = aliasEntries().map(([name, a]) => {
    const entry = projected[name];
    const routable = entry !== undefined && aliasRoutable(entry);
    return [
      codeText(name),
      el("span", { class: "pill", text: text(a.mode) }),
      String((a.candidates || []).length),
      a.allow_paid_fallback ? "paid fallback" : "free only",
      // "unavailable", never a named cause: provider health, a cooldown, cost
      // policy or fusion readiness may each be why, and the console does not
      // get to guess which
      el("span", { class: "pill " + (routable ? "up" : "bad"), text: routable ? "routable" : "unavailable" }),
    ];
  });
  return table(["Alias", "Mode", "Candidate paths", "Gate", "State"], rows);
}

function viewFusion() {
  const fusion = STATE.status.fusion || {};
  // fusion_status() emits exactly "configured" or "not_configured"; anything
  // else would be a state this console could never reach.
  const ready = fusion.state === "configured";
  return el("div", { class: "stack" },
    el("div", { class: "card" },
      el("h3", { text: "Fusion policy" }),
      el("div", { class: "card-row" }, el("span", { text: "state" }), codeText(fusion.state, ready ? "t-up" : "t-mono")),
      el("div", { class: "card-row" }, el("span", { text: "backends" }), codeText((fusion.backends || []).join(", ") || "none")),
      el("div", { class: "card-row" }, el("span", { text: "aliases" }), codeText((fusion.aliases || []).join(", ") || "none")),
    ));
}

function providerCard(p) {
  const fields = (STATE.schema.fields || []).filter((f) => !f.locked && f.key.startsWith("providers." + p.name + "."));
  const card = el("div", { class: "card" },
    el("h3", null, document.createTextNode(p.name), el("span", { class: "pill " + (p.configured ? "up" : "bad"), text: p.configured ? "configured" : "missing_credentials" })),
    el("div", { class: "card-row" }, el("span", { text: "base_url" }), codeText(p.base_url)),
    el("div", { class: "card-row" }, el("span", { text: "credential env" }), codeText((p.credential_envs || []).join(", "))),
    el("div", { class: "card-row" }, el("span", { text: "models" }), codeText(String((p.models || []).length))),
    el("div", { class: "card-row" }, el("span", { text: "cooldown" }), codeText(p.cooled_down ? "cooled" : "clear", p.cooled_down ? "t-warn" : "t-mono")),
  );
  for (const f of fields) {
    const id = "f-" + f.key;
    const current = FLOW.edits[f.key] !== undefined ? FLOW.edits[f.key] : f.value;
    const control = f.type === "select"
      ? el("select", {
          attrs: { id },
          onInput: (e) => { FLOW.edits[f.key] = e.target.value; renderCandidateBar(); renderProvidersActions(); },
        }, ...(f.options || []).map((o) => el("option", { text: o, attrs: { value: o }, props: { selected: o === current } })))
      : el("input", {
          attrs: { id, type: "number" },
          props: { value: current },
          onInput: (e) => { FLOW.edits[f.key] = Number(e.target.value); renderCandidateBar(); renderProvidersActions(); },
        });
    card.append(el("div", { class: "field" },
      el("label", { text: f.label, attrs: { for: id } }), control, el("small", { text: f.description })));
  }
  const probeOut = el("code", { class: "t-mono", text: "" });
  card.append(el("div", { class: "card-row" },
    el("button", {
      class: "secondary-button", attrs: { type: "button" }, text: "Probe",
      onClick: async () => {
        probeOut.textContent = "probing…";
        const r = await postJSON(PROVIDERS_BASE + "/" + p.name + "/test");
        probeOut.textContent = r.ok ? text(r.json.detail || "ok") : text(r.json.detail || r.json.error || r.status);
        probeOut.className = r.ok ? "t-up" : "t-bad";
      },
    }),
    probeOut));
  return card;
}

function renderProvidersActions() {
  const host = document.getElementById("stageActions");
  if (!host) return;
  const dirty = Object.keys(FLOW.edits).length > 0;
  host.replaceChildren(
    el("button", {
      class: "primary-button", attrs: { type: "button" }, text: "Stage candidate",
      props: { disabled: !dirty || FLOW.busy },
      onClick: runStage,
    }),
    el("span", { class: "muted", text: dirty ? Object.keys(FLOW.edits).length + " field(s) changed" : "No changes" }),
    FLOW.message && !dirty ? el("span", { class: "message-area " + FLOW.tone, text: FLOW.message }) : null,
  );
}

async function runStage() {
  FLOW.busy = true; renderProvidersActions();
  const r = await postJSON(ENDPOINTS.stage, { updates: FLOW.edits });
  FLOW.busy = false;
  if (r.ok && r.json.staged) {
    FLOW.path = r.json.path;
    FLOW.validated = false;
    FLOW.checksum = null;
    FLOW.message = "Staged " + text(r.json.version);
    FLOW.tone = "";
  } else {
    // 409 no_changes is the backend refusing to mint a candidate that is not a
    // change — the active revision is activated by its own path, not restaged.
    FLOW.message = text(r.json.error || r.json.reason || ("stage failed (" + r.status + ")"));
    FLOW.tone = "is-bad";
  }
  render();
}

function viewProviders() {
  return el("div", { class: "stack" },
    el("div", { class: "cards" }, ...STATE.providers.map(providerCard)),
    el("div", null,
      el("div", { class: "section-title", text: "Candidate" }),
      el("div", { class: "stage-actions", attrs: { id: "stageActions" } })),
  );
}

function viewHealth() {
  const h = STATE.health;
  const cooldowns = h.cooldowns || [];
  const cp = h.control_plane || {};
  const t = h.telemetry || {};
  return el("div", { class: "stack" },
    el("div", { class: "cards" },
      el("div", { class: "card" },
        el("h3", { text: "Service" }),
        el("div", { class: "card-row" }, el("span", { text: "status" }), codeText(h.status, "t-up")),
        el("div", { class: "card-row" }, el("span", { text: "routing" }), codeText((h.routing || {}).status)),
        el("div", { class: "card-row" }, el("span", { text: "config_version" }), codeText(h.config_version)),
        el("div", { class: "card-row" }, el("span", { text: "checksum" }), codeText(short(h.config_checksum))),
      ),
      el("div", { class: "card" },
        el("h3", { text: "Control plane" }),
        ...Object.entries(cp).map(([k, v]) => el("div", { class: "card-row" }, el("span", { text: k }), codeText(typeof v === "object" ? JSON.stringify(v) : v))),
      ),
      el("div", { class: "card" },
        el("h3", { text: "Telemetry delivery" }),
        ...Object.entries(t).map(([k, v]) => el("div", { class: "card-row" }, el("span", { text: k }), codeText(typeof v === "object" ? JSON.stringify(v) : v))),
      ),
    ),
    el("div", null,
      el("div", { class: "section-title", text: "Cooldowns" }),
      cooldowns.length
        ? table(["Provider", "Credential", "Model", "Reason", "Remaining"],
            cooldowns.map((c) => [codeText(c.provider), codeText(c.credential), codeText(c.model), text(c.reason), text(c.remaining_seconds)]))
        : pending("No cooled targets", "Every configured provider is currently eligible.")),
  );
}

function renderView() {
  const head = document.getElementById("viewhead");
  const body = document.getElementById("viewbody");

  if (!operatorActivated()) {
    head.hidden = true;
    head.replaceChildren();
    body.replaceChildren();
    return;
  }

  const [title, sub, source] = VIEW_CHROME[VIEW];
  head.hidden = false;
  head.replaceChildren(
    el("div", null, el("h1", { text: title }), el("p", { text: sub })),
    el("code", { text: source }),
  );

  const builders = {
    aliases: viewAliases,
    fusion: viewFusion,
    providers: viewProviders,
    health: viewHealth,
    routes: viewRoutes,
    audit: () => pending("No read-only audit endpoint",
      "Cerberus records revision registrations and activations, but 0.2.0 exposes no endpoint to read them. The screen stays empty until that endpoint exists rather than inventing a history.", true),
  };
  body.replaceChildren(builders[VIEW]());
  if (VIEW === "providers") renderProvidersActions();
}


// ---- routes ----

function projectedAliases() {
  return (STATE.routes && STATE.routes.aliases) || [];
}

function projectedPathCount() {
  return projectedAliases().reduce((n, a) => n + (a.paths ? a.paths.length : 0), 0);
}

// Presentation only: the server already decided the state and the reason. This
// turns its vocabulary into the operator's, and invents no verdict of its own.
const EXCLUSION_LABEL = {
  paid_fallback_prohibited: ["excluded by policy", "paid fallback prohibited for this alias"],
  cost_tier_guard: ["excluded by policy", "cost tier guard"],
  provider_down: ["unavailable", "provider health"],
  missing_credentials: ["unavailable", "credential not present"],
};

function describeState(path) {
  if (path.state === "eligible") return { short: "eligible", note: "first route path the router would attempt", tone: "t-up" };
  if (path.state === "standby") return { short: "standby", note: "attempted only if an earlier route path fails", tone: "t-mono" };
  const reason = (path.exclusion && path.exclusion.reason) || "excluded";
  if (reason.indexOf("cooldown_") === 0) {
    const scope = path.exclusion.scope ? " · scope " + path.exclusion.scope : "";
    return { short: "cooldown", note: reason.slice("cooldown_".length).replace(/_/g, " ") + scope + retryNote(path.exclusion.retry_at), tone: "t-warn" };
  }
  const label = EXCLUSION_LABEL[reason];
  return label
    ? { short: label[0], note: label[1], tone: reason === "provider_down" || reason === "missing_credentials" ? "t-bad" : "t-warn" }
    : { short: "excluded", note: reason.replace(/_/g, " "), tone: "t-warn" };
}

function retryNote(retryAt) {
  if (!retryAt) return "";
  const seconds = Math.max(0, Math.round(retryAt - Date.now() / 1000));
  if (seconds < 60) return " · retry in " + seconds + "s";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  return " · retry in " + (hours ? hours + "h " + minutes + "m" : minutes + "m");
}

function selectedAliasEntry() {
  const aliases = projectedAliases();
  return aliases.find((a) => a.alias === SELECTION.alias) || aliases[0] || null;
}

function aliasRail(aliases, current) {
  return el("div", { class: "alias-rail" }, ...aliases.map((entry, index) => {
    const on = current && entry.alias === current.alias;
    const count = entry.paths ? entry.paths.length + " route paths" : entry.fusion.panel.length + " panel members";
    return el("button", {
      class: "alias-tab" + (on ? " alias-tab-on" : ""),
      attrs: { type: "button", id: "alias-tab-" + index, "aria-pressed": on ? "true" : "false" },
      onClick: () => { SELECTION = { alias: entry.alias, ordinal: null }; render(); },
    },
      el("code", { class: "alias-name", text: entry.alias }),
      el("span", { class: "alias-meta" },
        el("span", { class: "pill", text: entry.mode === "fusion" ? "Fusion" : "dedicated" }),
        el("code", { text: count })));
  }));
}

function pathRow(entry, path) {
  const state = describeState(path);
  const on = SELECTION.alias === entry.alias && SELECTION.ordinal === path.ordinal;
  return el("button", {
    class: "path-row" + (on ? " path-row-on" : ""),
    attrs: { type: "button", id: "path-row-" + path.ordinal, "aria-pressed": on ? "true" : "false" },
    onClick: () => { SELECTION = { alias: entry.alias, ordinal: path.ordinal }; render(); },
  },
    el("code", { class: "path-ord", text: "pref " + path.ordinal }),
    el("div", { class: "path-main" },
      el("code", { class: "path-target", text: path.provider + "/" + path.model }),
      el("span", { class: "path-note", text: state.note })),
    el("code", { class: "path-cred", text: "cred " + path.credential_ref }),
    el("code", { class: "path-tier " + (path.cost_tier === "paid" ? "t-warn" : "t-mono"), text: path.cost_tier }),
    el("code", { class: "path-state " + state.tone, text: state.short }));
}

function fusionChain(entry) {
  const f = entry.fusion;
  const ready = f.readiness;
  const link = (label, nodes) => el("div", { class: "chain-step" },
    el("span", { class: "fact-label", text: label }),
    el("div", { class: "chain-nodes" }, ...nodes));
  const node = (p) => el("code", { class: "chain-node", text: p.provider + "/" + p.model });
  return el("div", { class: "stack" },
    el("p", { class: "chain-intro", text: "Cerberus composes the panel, analyst and outer model, then the backend performs the deliberation as one call. Panel members do not enter the failover loop, so they carry no eligibility or cooldown state." }),
    el("div", { class: "chain" },
      link("Panel members", f.panel.map(node)),
      el("div", { class: "chain-arrow", text: "↓" }),
      link("Analyst / judge", [node(f.analyst)]),
      el("div", { class: "chain-arrow", text: "↓" }),
      link("Outer model", [node(f.outer)])),
    el("div", { class: "card" },
      el("h3", { text: "Readiness" }),
      ...[["backend_present", "backend configured"], ["credential_present", "judge credential present"],
          ["provider_available", "judge provider available"], ["available", "can deliberate now"]]
        .map(([k, label]) => el("div", { class: "card-row" },
          el("span", { text: label }),
          codeText(ready[k] ? "yes" : "no", ready[k] ? "t-up" : "t-bad")))));
}

function inspectorRows(entry) {
  const projection = STATE.routes;
  const base = [
    ["active revision", text(projection.revision), "t-ink"],
    ["checksum", short(projection.checksum), "t-mono"],
    ["alias", entry.alias, "t-ink"],
    ["mode", entry.mode, "t-mono"],
  ];
  if (entry.fusion) {
    return base.concat([
      ["analyst", entry.fusion.analyst.provider + "/" + entry.fusion.analyst.model, "t-ink"],
      ["outer model", entry.fusion.outer.provider + "/" + entry.fusion.outer.model, "t-ink"],
      ["credential", entry.fusion.analyst.credential_ref, "t-mono"],
      ["panel size", String(entry.fusion.panel.length) + " of " + entry.fusion.max_panel_members, "t-mono"],
      ["paid panel", entry.fusion.allow_paid_panel ? "allowed" : "prohibited", "t-mono"],
      ["deliberation", entry.fusion.readiness.available ? "ready" : "unavailable",
       entry.fusion.readiness.available ? "t-up" : "t-bad"],
    ]);
  }
  const path = (entry.paths || []).find((p) => p.ordinal === SELECTION.ordinal);
  if (!path) return base.concat([["route path", "select a route path", "t-mono"]]);
  const state = describeState(path);
  return base.concat([
    ["route path", "pref " + path.ordinal, "t-ink"],
    ["provider / model", path.provider + "/" + path.model, "t-ink"],
    ["credential", path.credential_ref, "t-mono"],
    ["cost tier", path.cost_tier, path.cost_tier === "paid" ? "t-warn" : "t-mono"],
    ["paid fallback", entry.allow_paid_fallback ? "allowed" : "prohibited", "t-mono"],
    ["state", state.short, state.tone],
    ["reason", path.exclusion ? state.note : "—", path.exclusion ? state.tone : "t-mono"],
  ]);
}

function inspector(entry) {
  const rows = inspectorRows(entry);
  return el("aside", { class: "inspector", attrs: { id: "inspector", "aria-live": "polite", tabindex: "-1" } },
    el("span", { class: "fact-label", text: entry.fusion ? "Fusion inspector" : "Route path inspector" }),
    el("div", { class: "inspect-rows" },
      ...rows.map(([k, v, tone]) => el("div", { class: "inspect-row" },
        el("span", { text: k }), codeText(v, tone)))),
    el("div", { class: "inspect-identities" },
      el("span", { class: "fact-label", text: "Identity policy" }),
      ...(entry.identities.length
        ? entry.identities.map((i) => el("div", { class: "inspect-row" },
            el("span", { text: i.name }),
            codeText(i.authorized ? "authorized" : text(i.denial_reason), i.authorized ? "t-up" : "t-bad")))
        : [el("span", { class: "muted", text: "no identities in revision" })])));
}

function viewRoutes() {
  const aliases = projectedAliases();
  if (!aliases.length) {
    return pending("No aliases in this revision", "The active revision defines no alias, so there is no route path to show.");
  }
  const entry = selectedAliasEntry();
  const body = entry.fusion
    ? fusionChain(entry)
    : el("div", null,
        el("div", { class: "path-head" },
          el("span", { text: "Order" }), el("span", { text: "Route path" }),
          el("span", { text: "Credential" }), el("span", { text: "Cost" }), el("span", { text: "State" })),
        ...entry.paths.map((p) => pathRow(entry, p)));
  return el("div", { class: "routes" },
    el("div", { class: "routes-main" }, aliasRail(aliases, entry), el("div", { class: "routes-body" }, body)),
    inspector(entry));
}

// ---- render ----

function render() {
  // every render replaces whole subtrees, which destroys whatever the keyboard
  // was on and drops focus to <body>. Selecting a route path must not cost a
  // keyboard operator their place, so the same control is focused again by id.
  const focused = document.activeElement;
  const restore = focused && focused.id && focused !== document.body ? focused.id : null;

  renderHeader();
  renderNav();
  renderBringup();
  renderView();
  renderCandidateBar();

  if (restore) {
    const again = document.getElementById(restore);
    if (again) again.focus();
  }
}

wireCandidateBar();
refresh();
