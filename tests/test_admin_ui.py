"""Session 9 acceptance tests — read-only dashboard (SPEC test 19 analog).

The read-only property is proven behaviorally: the dashboard's routes accept
only GET/HEAD, the page carries no forms or submit controls, and the page
script is audited for mutating fetch configuration — not by grepping the HTML
for banned words.
"""

import re

import httpx
import pytest

from cerberus.app import create_app
from cerberus.registry import load_config_document
from tests.test_control import ok_upstream, raw_config, write_config

# every URL the dashboard is allowed to touch, and its own assets
DASHBOARD_DATA_URLS = {"/health", "/admin/status", "/admin/config/active", "/admin/events"}
DASHBOARD_ASSET_URLS = {"/admin/ui", "/admin/ui/app.css", "/admin/ui/app.js"}

LOOPBACK = ("127.0.0.1", 40001)
LOOPBACK_V6 = ("::1", 40001)
REMOTE = ("203.0.113.9", 40001)


def make_app(monkeypatch, tmp_path, *, token: str | None = None):
    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")
    raw = raw_config("cerberus-2026-07-16.1")
    if token is not None:
        monkeypatch.setenv("CERBERUS_API_TOKEN", token)
        raw["server"] = {"host": "0.0.0.0", "port": 4000, "api_token_env": "CERBERUS_API_TOKEN"}
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
    assert 'type="submit"' not in html and "<button" not in html and "<input" not in html
    # CSP compatibility: no inline script bodies, no inline event handlers
    assert re.search(r"<script(?![^>]*\bsrc=)", html) is None
    assert re.search(r"\son\w+\s*=", html) is None


@pytest.mark.asyncio
async def test_page_script_performs_only_approved_same_origin_gets(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path)
    script = (await fetch(app, "/admin/ui/app.js")).text
    # no fetch/XHR configuration for mutating methods anywhere in the script
    assert re.search(r"\bmethod\s*[:=]", script) is None
    assert re.search(r"\b(POST|PUT|PATCH|DELETE)\b", script) is None
    assert "XMLHttpRequest" not in script and "sendBeacon" not in script and "WebSocket" not in script
    # every fetched URL is a same-origin literal on the approved list
    fetched = set(re.findall(r'fetch\(([^)]*)\)', script))
    assert fetched == {"url"}, "fetch() must only be called through getJSON(url)"
    literal_urls = set(re.findall(r'"(/[^"]+)"', script)) - {"/"}  # bare "/" is the a/b/c join separator
    assert literal_urls <= DASHBOARD_DATA_URLS
    assert set(re.findall(r'https?://[^\s"\']+', script)) == set(), "no absolute/cross-origin URLs"
    # and every mutation endpoint of the control plane is absent from the page assets
    page = (await fetch(app, "/admin/ui")).text
    for mutation in ("/admin/validate", "/admin/activate", "/admin/rollback", "/admin/shadow"):
        assert mutation not in script and mutation not in page


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


@pytest.mark.asyncio
async def test_token_mode_requires_the_admin_token_from_any_origin(monkeypatch, tmp_path):
    app = make_app(monkeypatch, tmp_path, token="admin-token")
    async with app.router.lifespan_context(app):
        # token is the admin credential: valid from remote, required even on loopback
        async with client_for(app, REMOTE) as remote:
            assert (await remote.get("/admin/ui")).status_code == 401
            assert (
                await remote.get("/admin/ui", headers={"authorization": "Bearer admin-token"})
            ).status_code == 200
            assert (
                await remote.get("/admin/events", headers={"authorization": "Bearer wrong-token"})
            ).status_code == 401
        async with client_for(app, LOOPBACK) as local:
            assert (await local.get("/admin/events")).status_code == 401
            assert (
                await local.get("/admin/events", headers={"authorization": "Bearer admin-token"})
            ).status_code == 200


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
