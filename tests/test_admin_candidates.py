"""Candidate-configuration confinement and disclosure-safe validation errors.

/admin/validate, /admin/activate and /admin/shadow each take a filesystem path
from the request body. Unconfined, and with the loader's own exception text
returned verbatim, that is a file-read primitive for an admin-authenticated
caller: a YAML scanner error quotes the offending source line and a pydantic
error carries input_value, which for a non-mapping document is the whole file.
"""

from __future__ import annotations

import httpx
import pytest

from cerberus.app import create_app
from cerberus.registry import load_config_document
from tests.test_control import CSRF, ok_upstream, raw_config, write_config

# every endpoint that accepts a candidate path, with the key it reports under
CANDIDATE_ENDPOINTS = (("/admin/validate", "valid"), ("/admin/activate", "activated"), ("/admin/shadow", "armed"))

SECRET = "sk-live-MUST-NEVER-APPEAR"


@pytest.fixture(autouse=True)
def _provider_key(monkeypatch):
    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")


def build_app(tmp_path, **server):
    """An app booted from tmp_path/active.yaml, so tmp_path is the boot root."""

    raw = raw_config("cerberus-2026-07-16.1")
    if server:
        raw["server"] = {"host": "127.0.0.1", "port": 4000, **server}
    doc = load_config_document(write_config(tmp_path, "active.yaml", raw))
    return create_app(doc, http_transport=httpx.MockTransport(ok_upstream))


async def post(app, url, body):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(url, json=body, headers=CSRF)


# -- confinement ----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("url,key", CANDIDATE_ENDPOINTS)
async def test_path_outside_every_root_is_refused_unread(tmp_path, url, key):
    """The refusal must be identical whatever the file holds — proof it was never
    opened. Previously this echoed the file's own bytes back in the error."""

    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    secret_file = outside / "private.yaml"
    secret_file.write_text(f"TOKEN: {SECRET}\n\tbadindent\n", encoding="utf-8")

    app = build_app(tmp_path)
    response = await post(app, url, {"path": str(secret_file)})

    assert response.status_code == 422
    body = response.json()
    assert body[key] is False
    assert body["reason"] == "outside_allowed_path"
    assert SECRET not in response.text
    assert str(secret_file) not in response.text  # not even the path is confirmed


@pytest.mark.asyncio
@pytest.mark.parametrize("url,key", CANDIDATE_ENDPOINTS)
async def test_dotdot_escape_is_refused(tmp_path, url, key):
    """Containment is decided on the canonical path, so `..` cannot walk out even
    though the string starts inside an allowed root."""

    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "escape.yaml").write_text(f"TOKEN: {SECRET}\n", encoding="utf-8")

    app = build_app(tmp_path)
    traversal = str(tmp_path / ".." / "outside" / "escape.yaml")
    response = await post(app, url, {"path": traversal})

    assert response.status_code == 422
    assert response.json()["reason"] == "outside_allowed_path"
    assert SECRET not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("url,key", CANDIDATE_ENDPOINTS)
async def test_symlink_escape_is_refused(tmp_path, url, key):
    """A symlink sitting inside an allowed root but pointing out of it is judged
    on its target. String-level `..` filtering would not catch this."""

    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    target = outside / "target.yaml"
    target.write_text(f"TOKEN: {SECRET}\n", encoding="utf-8")
    link = tmp_path / "innocent.yaml"
    link.symlink_to(target)

    app = build_app(tmp_path)
    response = await post(app, url, {"path": str(link)})

    assert response.status_code == 422
    assert response.json()["reason"] == "outside_allowed_path"
    assert SECRET not in response.text


@pytest.mark.asyncio
async def test_config_beside_the_active_one_is_accepted(tmp_path):
    """Git owns authoring: a revision placed beside the active config still works."""

    candidate = write_config(tmp_path, "v2.yaml", raw_config("cerberus-2026-07-16.2"))
    app = build_app(tmp_path)
    response = await post(app, "/admin/validate", {"path": candidate})

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["version"] == "cerberus-2026-07-16.2"


@pytest.mark.asyncio
async def test_staged_candidate_round_trips_through_validate_and_activate(tmp_path):
    """The console's own stage -> validate -> activate loop must keep working:
    the staging directory is always an allowed root."""

    app = build_app(tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            staged = await client.post(
                "/admin/config/stage",
                json={"updates": {"providers.alpha.quota_cooldown_seconds": 1234}},
                headers=CSRF,
            )
            assert staged.status_code == 200
            path = staged.json()["path"]
            validated = await client.post("/admin/validate", json={"path": path}, headers=CSRF)
            activated = await client.post("/admin/activate", json={"path": path}, headers=CSRF)

    assert validated.status_code == 200 and validated.json()["valid"] is True
    assert activated.status_code == 200 and activated.json()["activated"] is True


@pytest.mark.asyncio
async def test_explicitly_configured_extra_root_is_allowed(tmp_path):
    """admin_config_roots widens the boundary only where an operator says so."""

    elsewhere = tmp_path.parent / "authored"
    elsewhere.mkdir(exist_ok=True)
    candidate = write_config(elsewhere, "v2.yaml", raw_config("cerberus-2026-07-16.2"))

    refused = await post(build_app(tmp_path), "/admin/validate", {"path": candidate})
    assert refused.status_code == 422 and refused.json()["reason"] == "outside_allowed_path"

    allowed_app = build_app(tmp_path, admin_config_roots=[str(elsewhere)])
    accepted = await post(allowed_app, "/admin/validate", {"path": candidate})
    assert accepted.status_code == 200 and accepted.json()["valid"] is True


@pytest.mark.asyncio
async def test_shadow_clear_needs_no_path(tmp_path):
    """path=None reads nothing, so containment must not stand in its way."""

    response = await post(build_app(tmp_path), "/admin/shadow", {"path": None})
    assert response.status_code == 200
    assert response.json() == {"shadow_version": None}


# -- disclosure-safe errors ------------------------------------------------


@pytest.mark.asyncio
async def test_yaml_error_reports_position_without_quoting_the_source(tmp_path):
    """A YAML scanner error's str() embeds the offending line. Only the parser's
    own description and the coordinates may be returned."""

    broken = tmp_path / "broken.yaml"
    broken.write_text(f"metadata:\n  version: cerberus-2026-07-16.2\nleaked: {SECRET}: extra\n", encoding="utf-8")

    response = await post(build_app(tmp_path), "/admin/validate", {"path": str(broken)})

    assert response.status_code == 422
    body = response.json()
    assert body["reason"] == "invalid_yaml"
    assert SECRET not in response.text
    assert "line 3" in body["error"]  # position is safe and is what an operator needs


@pytest.mark.asyncio
async def test_non_mapping_document_never_echoes_its_contents(tmp_path):
    """pydantic's input_value for a scalar document is the entire file."""

    scalar = tmp_path / "scalar.yaml"
    scalar.write_text(f"AWS_SECRET_ACCESS_KEY={SECRET}\n", encoding="utf-8")

    response = await post(build_app(tmp_path), "/admin/validate", {"path": str(scalar)})

    assert response.status_code == 422
    body = response.json()
    assert body["reason"] == "schema_invalid"
    assert SECRET not in response.text
    assert "input_value" not in response.text


@pytest.mark.asyncio
async def test_schema_errors_keep_cerberus_authored_detail(tmp_path):
    """The fix suppresses echoed file content, not diagnostics: a cross-reference
    failure is generated from Cerberus's own schema and stays in the response."""

    raw = raw_config("cerberus-2026-07-16.2")
    raw["aliases"]["cerberus/free"]["candidates"][0]["model"] = "ghost"
    candidate = write_config(tmp_path, "ghost.yaml", raw)

    response = await post(build_app(tmp_path), "/admin/validate", {"path": candidate})

    assert response.status_code == 422
    body = response.json()
    assert body["reason"] == "schema_invalid"
    assert "ghost" in body["error"]


@pytest.mark.asyncio
async def test_unreadable_candidate_inside_a_root_is_classified(tmp_path):
    """A missing file under an allowed root is a clean reason, not an OSError string."""

    response = await post(build_app(tmp_path), "/admin/validate", {"path": str(tmp_path / "absent.yaml")})

    assert response.status_code == 422
    body = response.json()
    assert body["reason"] == "not_readable"
    assert "absent.yaml" not in body["error"]


@pytest.mark.asyncio
async def test_version_checksum_collision_keeps_its_cerberus_message(tmp_path):
    """Cerberus's own policy refusals are distinct from schema failures and their
    messages are safe to return — they quote config identifiers, never content."""

    app = build_app(tmp_path)
    first = write_config(tmp_path, "a.yaml", raw_config("cerberus-2026-07-16.2"))
    collision = write_config(tmp_path, "b.yaml", raw_config("cerberus-2026-07-16.2", model="alpha-two"))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/admin/validate", json={"path": first}, headers=CSRF)
            clash = await client.post("/admin/validate", json={"path": collision}, headers=CSRF)

    assert clash.status_code == 422
    assert clash.json()["reason"] == "config_rejected"
    assert "already bound to checksum" in clash.json()["error"]
