/* Cerberus read-only dashboard.
   Rendering rules: all API data enters the DOM through textContent (never
   innerHTML), and the only network calls are same-origin GETs to the
   endpoints listed in ENDPOINTS. */
"use strict";

const ENDPOINTS = {
  health: "/health",
  status: "/admin/status",
  config: "/admin/config/active",
  events: "/admin/events",
};

async function getJSON(url) {
  const response = await fetch(url); // GET only; no method/body options anywhere in this file
  if (!response.ok) throw new Error(url + " " + response.status);
  return response.json();
}

const text = (value) => String(value ?? "—");

function el(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}

function cell(content) {
  return el("td", null, text(content));
}

function codeCell(parts, separator) {
  const td = document.createElement("td");
  parts.forEach((part, index) => {
    if (index > 0 && separator) td.append(separator === "br" ? document.createElement("br") : separator);
    td.append(el("code", null, text(part)));
  });
  return td;
}

function fillTable(id, rows, emptyText, emptyClass) {
  const body = document.querySelector("#" + id + " tbody");
  body.replaceChildren();
  if (!rows.length) {
    const td = el("td", emptyClass || null, emptyText);
    td.colSpan = document.querySelectorAll("#" + id + " thead th").length;
    const tr = document.createElement("tr");
    tr.append(td);
    body.append(tr);
    return;
  }
  rows.forEach((cells) => {
    const tr = document.createElement("tr");
    cells.forEach((c) => tr.append(c));
    body.append(tr);
  });
}

function renderMeta(health, status) {
  const meta = document.getElementById("meta");
  meta.replaceChildren(
    el("span", "pill ok", text(health.status)),
    " active: ",
    el("code", null, text(health.config_version)),
    " (",
    el("code", null, text(health.config_checksum).slice(0, 18) + "…"),
    ") · ",
  );
  if (status.shadow) {
    meta.append("shadow: ", el("code", null, text(status.shadow.version)));
  } else {
    meta.append("no shadow armed");
  }
  meta.append(" · rollback depth " + text(status.rollback_depth));
}

function renderError(err) {
  document.getElementById("meta").replaceChildren(el("span", "pill warn", "unreachable"), " " + text(err.message));
}

async function refresh() {
  try {
    const [health, status, config, events] = await Promise.all([
      getJSON(ENDPOINTS.health),
      getJSON(ENDPOINTS.status),
      getJSON(ENDPOINTS.config),
      getJSON(ENDPOINTS.events),
    ]);
    renderMeta(health, status);

    fillTable(
      "providers",
      Object.entries(config.providers).flatMap(([provider, pv]) =>
        Object.keys(pv.credentials).flatMap((credential) =>
          Object.entries(pv.models).map(([model, mv]) => [
            cell(provider),
            cell(credential),
            codeCell([model]),
            cell(mv.cost_tier),
          ]),
        ),
      ),
      "none",
    );

    fillTable(
      "cooldowns",
      health.cooldowns.map((c) => [
        cell(c.scope),
        codeCell([text(c.provider) + "/" + text(c.credential) + "/" + text(c.model)]),
        cell(c.reason),
        cell(Math.round(c.seconds_remaining) + "s"),
      ]),
      "none — all targets live",
      "ok",
    );

    fillTable(
      "aliases",
      Object.entries(config.aliases).map(([alias, av]) => [
        codeCell([alias]),
        cell(av.mode),
        codeCell(av.candidates.map((c) => text(c.provider) + "/" + text(c.credential) + "/" + text(c.model)), "br"),
      ]),
      "none",
    );

    fillTable(
      "identities",
      Object.entries(config.identities || {}).map(([identity, iv]) => [
        cell(identity),
        cell((iv.allowed_modes || []).join(", ")),
        codeCell(iv.allowed_aliases || [], "br"),
      ]),
      "none (server-token mode)",
    );

    fillTable(
      "events",
      events.events.map((e) => [
        el("td", e.outcome === "success" ? "ok" : "warn", text(e.outcome)),
        cell(e.identity),
        codeCell([e.alias]),
        codeCell([e.provider ? text(e.provider) + "/" + text(e.credential) + "/" + text(e.model) : "—"]),
        cell((e.exclusions || []).length),
        cell(e.latency_ms != null ? Math.round(e.latency_ms) + "ms" : "—"),
        codeCell([e.config_version]),
      ]),
      "no events yet",
    );
  } catch (err) {
    renderError(err);
  }
}

refresh();
setInterval(refresh, 5000);
