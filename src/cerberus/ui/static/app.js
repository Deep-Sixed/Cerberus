/* Cerberus console — the frozen console shell (PR #12).

   All data enters the DOM via textContent/createElement (never innerHTML).
   Reads are plain same-origin GETs through getJSON(url). The only writes are
   the provider probe and the candidate loop (stage/validate/activate) — both
   funnel through the single CSRF'd postJSON(url, body) below, an allow-listed
   subset of operational fields (never secrets, never identity, access-control,
   or server bindings — the backend enforces that boundary).

   Console state is derived, never stored as a lifecycle flag: `operator_activated`
   from /admin/status answers "has an operator taken charge of a revision", and
   the /admin/events ring answers "has anything been routed since boot". Cerberus
   always boots with an active revision and routes from it, so the first-run
   experience is about establishing the first operator-managed revision — not
   about a gateway that cannot serve. */
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

let STATE = { health: {}, status: {}, config: {}, schema: {}, events: [], providers: [] };
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
  routes: ["Routes", "Why a request lands where it lands: alias, identity policy, eligible path, provider.", "GET /admin/config/active · /admin/events"],
  fusion: ["Fusion", "Cerberus holds the policy; the backend holds the deliberation.", "GET /admin/status"],
  providers: ["Providers", "Credential presence, cooldown windows, live probe.", "GET /admin/providers"],
  health: ["Health", "Liveness, telemetry delivery and cooldown state behind the admin boundary.", "GET /admin/health"],
  audit: ["Audit", "Revision registrations and activations.", "no endpoint"],
};

// Two independent facts, never one lifecycle flag: has an operator activated a
// revision, and does the in-memory ring hold a decision from this boot.
const operatorActivated = () => STATE.status.operator_activated === true;
const hasDecisions = () => STATE.events.length > 0;

function aliasEntries() {
  return Object.entries(STATE.config.aliases || {});
}

function configuredProviders() {
  const map = {};
  for (const p of STATE.providers) map[p.name] = p.configured;
  return map;
}

// An alias is routable when at least one of its candidates names a provider
// whose credentials are actually present in the environment.
function routableCount() {
  const configured = configuredProviders();
  const aliases = aliasEntries();
  const routable = aliases.filter(([, a]) => (a.candidates || []).some((c) => configured[c.provider])).length;
  return { routable, total: aliases.length };
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
    const [health, status, config, schema, events, providers] = await Promise.all([
      getJSON(ENDPOINTS.health), getJSON(ENDPOINTS.status), getJSON(ENDPOINTS.config),
      getJSON(ENDPOINTS.schema), getJSON(ENDPOINTS.events), getJSON(ENDPOINTS.providers),
    ]);
    STATE = {
      health, status, config, schema,
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
  if (id === "routes") return { label: String(candidatePathCount()), cls: "navcount" };
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
        attrs: { type: "button", "aria-current": !locked && VIEW === id ? "page" : "false" },
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
  const configured = configuredProviders();
  const rows = aliasEntries().map(([name, a]) => {
    const routable = (a.candidates || []).some((c) => configured[c.provider]);
    return [
      codeText(name),
      el("span", { class: "pill", text: text(a.mode) }),
      String((a.candidates || []).length),
      a.allow_paid_fallback ? "paid fallback" : "free only",
      el("span", { class: "pill " + (routable ? "up" : "bad"), text: routable ? "routable" : "missing_credentials" }),
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
    el("div", null, el("h1", { text: title }), el("p", { text: hasDecisions() || VIEW !== "routes" ? sub : "No routing decisions retained since boot — what follows is configuration, not traffic." })),
    el("code", { text: source }),
  );

  const builders = {
    aliases: viewAliases,
    fusion: viewFusion,
    providers: viewProviders,
    health: viewHealth,
    routes: () => pending("Route topology arrives in PR #13",
      "The route path, its failover ladder and the route inspector are the next work package. This shell reserves their place; nothing is rendered from guesswork."),
    audit: () => pending("No read-only audit endpoint",
      "Cerberus records revision registrations and activations, but 0.2.0 exposes no endpoint to read them. The screen stays empty until that endpoint exists rather than inventing a history.", true),
  };
  body.replaceChildren(builders[VIEW]());
  if (VIEW === "providers") renderProvidersActions();
}

// ---- render ----

function render() {
  renderHeader();
  renderNav();
  renderBringup();
  renderView();
  renderCandidateBar();
}

wireCandidateBar();
refresh();
