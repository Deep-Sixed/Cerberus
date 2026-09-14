"""Session 9 acceptance tests — dashboard read surface + narrow, allow-listed
config editing (SPEC test 19 analog, extended).

The read surface's boundary is proven behaviorally: its routes accept only
GET/HEAD, the page carries no native form-submission path (buttons are
type="button", never inside a <form>, never type="submit"), and the page
script is audited for mutating fetch configuration — not by grepping the HTML
for banned words. The console's one write path (stage/validate/activate, an
allow-listed subset of operational fields) is intentional and CSRF-guarded;
it is verified directly rather than asserted absent.
"""

import re

import httpx
import pytest

from cerberus.app import create_app
from cerberus.registry import load_config_document
from tests.test_control import ok_upstream, raw_config, write_config

CSRF = {"x-cerberus-csrf": "1"}

# every URL the console is allowed to touch (all GET), and its own assets
DASHBOARD_DATA_URLS = {
    "/health", "/admin/status", "/admin/config/active", "/admin/config/schema",
    "/admin/events", "/admin/providers",
}
DASHBOARD_ASSET_URLS = {"/admin/ui", "/admin/ui/app.css", "/admin/ui/app.js"}

LOOPBACK = ("127.0.0.1", 40001)
LOOPBACK_V6 = ("::1", 40001)
REMOTE = ("203.0.113.9", 40001)


def make_app(monkeypatch, tmp_path, *, token: str | None = None, admin_token: str | None = None):
    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")
    raw = raw_config("cerberus-2026-07-16.1")
    if token is not None or admin_token is not None:
        server: dict = {"host": "0.0.0.0", "port": 4000}
        if token is not None:
            monkeypatch.setenv("CERBERUS_API_TOKEN", token)
            server["api_token_env"] = "CERBERUS_API_TOKEN"
        if admin_token is not None:
            monkeypatch.setenv("CERBERUS_ADMIN_TOKEN", admin_token)
            server["admin_token_env"] = "CERBERUS_ADMIN_TOKEN"
        raw["server"] = server
    doc = load_config_document(write_config(tmp_path, "v1.yaml", raw))
    return create_app(doc, http_transport=httpx.MockTransport(ok_upstream))


def client_for(app, client_addr):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=client_addr), base_url="http://test"
    )


async def fetch(app, url, client_addr=LOOPBACK, headers=None, method="GET"):
    async with app.router.lifespan_context(app):
        async with client_for(app, client_addr) as client:
            return await client.request(method, url, headers=headers)


# -- read-only surface --------------------------------------------------------


def test_dashboard_routes_accept_only_get_and_head(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    surface = DASHBOARD_DATA_URLS | DASHBOARD_ASSET_URLS
    seen = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if path in surface:
            seen.add(path)
            assert set(route.methods) <= {"GET", "HEAD"}, f"{path} allows {route.methods}"
    assert seen == surface, f"missing dashboard routes: {surface - seen}"


@pytest.mark.asyncio
async def test_mutating_methods_are_rejected_on_dashboard_routes(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    async with app.router.lifespan_context(app):
        async with client_for(app, LOOPBACK) as client:
            for url in sorted(DASHBOARD_DATA_URLS | DASHBOARD_ASSET_URLS):
                for method in ("POST", "PUT", "PATCH", "DELETE"):
                    response = await client.request(method, url)
                    assert response.status_code == 405, f"{method} {url} -> {response.status_code}"


@pytest.mark.asyncio
async def test_page_has_no_forms_or_submit_controls_or_inline_code(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    page = await fetch(app, "/admin/ui")
    assert page.status_code == 200
    html = page.text.lower()
    assert "<form" not in html
    assert "formaction" not in html
    assert "<input" not in html  # every field input is JS-created at runtime, never static
    assert 'type="submit"' not in html
    # every static <button> must be explicitly type="button" — inert without JS,
    # and with no <form> on the page (asserted above) there's nothing to submit
    # to even if the type attribute were ever omitted
    buttons = re.findall(r"<button\b[^>]*>", html)
    assert buttons, "expected the config editor's static action-bar buttons"
    assert all('type="button"' in b for b in buttons), buttons
    # CSP compatibility: no inline script bodies, no inline event handlers
    assert re.search(r"<script(?![^>]*\bsrc=)", html) is None
    assert re.search(r"\son\w+\s*=", html) is None


# the console's one write path: an allow-listed subset of operational config
# fields, staged then run through the existing, already-reviewed validate/
# activate machinery. rollback and shadow are deliberately NOT wired to any
# UI control in this pass — no browser control should reach them yet.
WIRED_MUTATION_URLS = {"/admin/config/stage", "/admin/validate", "/admin/activate"}
UNWIRED_MUTATION_URLS = {"/admin/rollback", "/admin/shadow"}


@pytest.mark.asyncio
async def test_page_script_performs_only_approved_same_origin_requests(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    script = (await fetch(app, "/admin/ui/app.js")).text
    # every write (provider probe, stage, validate, activate) funnels through
    # one shared POST helper, carrying the CSRF header — not four call sites
    assert script.count('method: "POST"') == 1
    assert "X-Cerberus-CSRF" in script
    assert re.search(r"\b(PUT|PATCH|DELETE)\b", script) is None
    assert "XMLHttpRequest" not in script and "sendBeacon" not in script and "WebSocket" not in script
    # every fetch() is funnelled through a helper taking a `url` variable — never an
    # inline/constructed target (getJSON reads; postJSON is the CSRF'd write path)
    first_args = {m.split(",")[0].strip() for m in re.findall(r"fetch\(([^)]*)", script)}
    assert first_args == {"url"}, f"fetch() must only take the helper's url: {first_args}"
    # "/" is a join separator; "/test" is the probe suffix appended to /admin/providers
    literal_urls = set(re.findall(r'"(/[^"]+)"', script)) - {"/", "/test"}
    assert literal_urls <= DASHBOARD_DATA_URLS | WIRED_MUTATION_URLS
    assert literal_urls.isdisjoint(UNWIRED_MUTATION_URLS), literal_urls & UNWIRED_MUTATION_URLS
    # the SVG XML namespace URI is a required createElementNS() identifier, never
    # dereferenced as a network target — excluded, everything else must be absent
    absolute_urls = set(re.findall(r'https?://[^\s"\']+', script)) - {"http://www.w3.org/2000/svg"}
    assert absolute_urls == set(), "no absolute/cross-origin URLs"
    # rollback/shadow stay entirely absent from the shipped assets — not a
    # capability the browser can reach even indirectly
    page = (await fetch(app, "/admin/ui")).text
    for unwired in UNWIRED_MUTATION_URLS:
        assert unwired not in script and unwired not in page


@pytest.mark.asyncio
async def test_dashboard_responses_carry_defensive_headers(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    for url in sorted(DASHBOARD_ASSET_URLS | {"/admin/events"}):
        response = await fetch(app, url)
        assert response.status_code == 200, url
        assert response.headers["cache-control"] == "no-store", url
        assert response.headers["x-content-type-options"] == "nosniff", url
        csp = response.headers["content-security-policy"]
        assert "default-src 'none'" in csp and "form-action 'none'" in csp and "frame-ancestors 'none'" in csp
    page = await fetch(app, "/admin/ui")
    assert page.headers["content-type"].startswith("text/html")
    assert "Cerberus" in page.text


# -- authorization ------------------------------------------------------------

ADMIN_URLS = sorted(DASHBOARD_ASSET_URLS | {"/admin/events", "/admin/status", "/admin/config/active"})


@pytest.mark.asyncio
async def test_loopback_is_allowed_without_token_ipv4_and_ipv6(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    async with app.router.lifespan_context(app):
        for addr in (LOOPBACK, LOOPBACK_V6):
            async with client_for(app, addr) as client:
                for url in ADMIN_URLS:
                    assert (await client.get(url)).status_code == 200, f"{addr} {url}"


@pytest.mark.asyncio
async def test_anonymous_remote_requests_are_denied_without_token(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    async with app.router.lifespan_context(app):
        async with client_for(app, REMOTE) as client:
            for url in ADMIN_URLS:
                assert (await client.get(url)).status_code == 403, url


@pytest.mark.asyncio
async def test_forwarding_headers_cannot_impersonate_loopback(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    spoofs = [
        {"x-forwarded-for": "127.0.0.1"},
        {"x-real-ip": "127.0.0.1"},
        {"forwarded": "for=127.0.0.1"},
        {"x-forwarded-for": "::1", "x-forwarded-host": "localhost"},
    ]
    async with app.router.lifespan_context(app):
        async with client_for(app, REMOTE) as client:
            for headers in spoofs:
                response = await client.get("/admin/ui", headers=headers)
                assert response.status_code == 403, headers


MUTATION_URLS = ("/admin/config/stage", "/admin/validate", "/admin/activate", "/admin/rollback", "/admin/shadow")
INFER = {"model": "cerberus/free", "messages": []}


@pytest.mark.asyncio
async def test_inference_token_authorizes_inference_only(monkeypatch, tmp_path):
    """The inference api token must never grant admin access of any kind."""

    app = make_app(monkeypatch, tmp_path, token="inference-token")
    bearer = {"authorization": "Bearer inference-token"}
    async with app.router.lifespan_context(app):
        async with client_for(app, REMOTE) as remote:
            # inference works with the inference token
            assert (await remote.post("/v1/chat/completions", json=INFER, headers=bearer)).status_code == 200
            assert (await remote.post("/v1/chat/completions", json=INFER)).status_code == 401
            # ...but the same token gets no admin surface at all
            for url in ADMIN_URLS:
                assert (await remote.get(url, headers=bearer)).status_code in (401, 403), url
            for url in MUTATION_URLS:
                assert (await remote.post(url, json={}, headers=bearer)).status_code == 403, url


@pytest.mark.asyncio
async def test_loopback_reads_dashboard_even_in_token_mode(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path, token="inference-token", admin_token="admin-secret")
    async with app.router.lifespan_context(app):
        # a local browser cannot attach a bearer token: loopback reads the
        # read-only surface directly even with credentials configured
        for addr in (LOOPBACK, LOOPBACK_V6):
            async with client_for(app, addr) as local:
                for url in ADMIN_URLS:
                    assert (await local.get(url)).status_code == 200, f"{addr} {url}"


@pytest.mark.asyncio
async def test_remote_read_only_requires_the_distinct_admin_token(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path, token="inference-token", admin_token="admin-secret")
    async with app.router.lifespan_context(app):
        async with client_for(app, REMOTE) as remote:
            assert (await remote.get("/admin/ui")).status_code == 401
            assert (
                await remote.get("/admin/ui", headers={"authorization": "Bearer admin-secret"})
            ).status_code == 200
            # the inference token and wrong tokens are rejected
            assert (
                await remote.get("/admin/events", headers={"authorization": "Bearer inference-token"})
            ).status_code == 401
            assert (
                await remote.get("/admin/events", headers={"authorization": "Bearer wrong"})
            ).status_code == 401
            # forwarding headers cannot impersonate loopback in token mode
            assert (
                await remote.get("/admin/ui", headers={"x-forwarded-for": "127.0.0.1"})
            ).status_code == 401


@pytest.mark.asyncio
async def test_mutations_are_loopback_only_for_every_credential(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path, token="inference-token", admin_token="admin-secret")
    async with app.router.lifespan_context(app):
        async with client_for(app, REMOTE) as remote:
            for headers in (
                {},
                {"authorization": "Bearer inference-token"},
                {"authorization": "Bearer admin-secret"},
                {"x-forwarded-for": "127.0.0.1"},
            ):
                for url in MUTATION_URLS:
                    assert (await remote.post(url, json={}, headers=headers)).status_code == 403, (url, headers)
        # a true loopback peer reaches the mutation endpoints (auth + CSRF pass;
        # empty bodies then fail validation, not authorization)
        async with client_for(app, LOOPBACK) as local:
            assert (await local.post("/admin/validate", json={}, headers=CSRF)).status_code == 400
            assert (await local.post("/admin/rollback", headers=CSRF)).status_code == 409


@pytest.mark.asyncio
async def test_admin_status_reports_release_and_fusion_distinct_from_checksum(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    async with app.router.lifespan_context(app):
        async with client_for(app, LOOPBACK) as local:
            status = (await local.get("/admin/status")).json()
            health = (await local.get("/health")).json()
            script = (await local.get("/admin/ui/app.js")).text

    assert status["release_id"], "release_id must be exposed and nonempty"
    assert status["release_id"] != health["config_checksum"], "release is not the config checksum"
    # no fusion alias in this config → truthful not_configured, aliases listed
    assert status["fusion"]["state"] == "not_configured", "fusion state must be truthful, never omitted"
    assert status["fusion"]["aliases"] == []
    # the dashboard renders both as their own labeled values
    assert "release_id" in script and "fusion" in script


@pytest.mark.asyncio
async def test_no_credential_is_embedded_in_dashboard_assets(monkeypatch, tmp_path):
    monkeypatch.setenv("CERBERUS_API_TOKEN", "admin-token-secret-value")
    app = make_app(monkeypatch, tmp_path, token="admin-token-secret-value")
    async with app.router.lifespan_context(app):
        async with client_for(app, LOOPBACK) as local:
            for url in sorted(DASHBOARD_ASSET_URLS):
                body = (await local.get(url)).text
                assert "admin-token-secret-value" not in body, url
                assert "authorization" not in body.lower(), url
                assert "localstorage" not in body.lower() and "document.cookie" not in body.lower(), url


# -- events endpoint ----------------------------------------------------------


@pytest.mark.asyncio
async def test_events_endpoint_returns_recent_events_newest_first(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    async with app.router.lifespan_context(app):
        async with client_for(app, LOOPBACK) as client:
            await client.post("/v1/chat/completions", json={"model": "cerberus/free", "messages": []})
            await client.post("/v1/chat/completions", json={"model": "cerberus/free", "messages": []})
            events = await client.get("/admin/events")

    payload = events.json()["events"]
    assert len(payload) == 2
    assert payload[0]["alias"] == "cerberus/free"
    assert payload[0]["outcome"] == "success"
    assert payload[0]["release_id"], "every event must carry a nonempty release_id"
    assert payload[0]["config_version"] == "cerberus-2026-07-16.1"


@pytest.mark.asyncio
async def test_admin_providers_lists_status_without_secret_values(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    async with app.router.lifespan_context(app):
        async with client_for(app, LOOPBACK) as local:
            resp = await local.get("/admin/providers")
    assert resp.status_code == 200
    providers = resp.json()["providers"]
    alpha = next(p for p in providers if p["name"] == "alpha")
    assert alpha["configured"] is True  # ALPHA_KEY is set by make_app
    assert alpha["credential_envs"] == ["ALPHA_KEY"]  # names only
    assert "alpha-secret" not in resp.text  # never leak values
    assert any(m["cost_tier"] == "free" for m in alpha["models"])


@pytest.mark.asyncio
async def test_admin_providers_requires_admin_like_the_dashboard(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    async with app.router.lifespan_context(app):
        async with client_for(app, REMOTE) as remote:
            resp = await remote.get("/admin/providers")
    assert resp.status_code == 403  # loopback-only, no token configured here


@pytest.mark.asyncio
async def test_provider_probe_is_post_and_requires_csrf_header(monkeypatch, tmp_path):
    """The probe spends provider quota: it must not be reachable by a bare GET,
    and a POST without the custom CSRF header is refused."""
    app = make_app(monkeypatch, tmp_path)
    async with app.router.lifespan_context(app):
        async with client_for(app, LOOPBACK) as local:
            assert (await local.get("/admin/providers/alpha/test")).status_code == 405
            assert (await local.post("/admin/providers/alpha/test")).status_code == 403
            ok = await local.post("/admin/providers/alpha/test", headers={"x-cerberus-csrf": "1"})
            assert ok.status_code == 200
            assert ok.json()["probe"] == "models"  # default probe kind


@pytest.mark.asyncio
async def test_every_mutation_endpoint_requires_csrf_even_from_loopback(monkeypatch, tmp_path):
    """A loopback-authenticated request is not enough on its own for a mutation
    (R4's lesson, now applied to all five, not just the provider probe): every
    /admin/* POST that changes state must also carry the CSRF header, since a
    cross-site form/img riding an ambient SSO session cookie cannot set one."""
    app = make_app(monkeypatch, tmp_path)
    async with app.router.lifespan_context(app):
        async with client_for(app, LOOPBACK) as local:
            for url in MUTATION_URLS:
                resp = await local.post(url, json={})
                assert resp.status_code == 403, (url, resp.status_code)
                assert "csrf" in resp.json()["error"]["message"].lower(), (url, resp.json())
