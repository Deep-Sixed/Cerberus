"""First-run bring-up — the console's bootstrap-vs-operator-managed distinction.

Cerberus always boots with an active revision and routes from it, so "a revision
is active" cannot tell an operator-managed gateway from one still running the
configuration it started with. ``operator_activated`` answers that, derived from
the audit trail activation already writes rather than from a new column or a
flag held in memory — so it survives restart.

The bring-up flow follows from it: an unchanged bootstrap configuration is
validated and activated by its own path. Staging is for actual edits, and
staging nothing is refused rather than minting a candidate whose version is
already bound to another checksum.
"""

import httpx
import pytest

from cerberus.app import create_app
from cerberus.registry import load_config_document
from tests.test_control import ok_upstream, raw_config, write_config

CSRF = {"x-cerberus-csrf": "1"}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")


def persisted_app(tmp_path, version="cerberus-2026-07-16.1", name="active.yaml"):
    """An app whose control plane is on disk, so state survives a 'restart'."""

    raw = raw_config(version)
    raw["state"] = {"path": str(tmp_path / "state.sqlite3")}
    doc = load_config_document(write_config(tmp_path, name, raw))
    return create_app(doc, http_transport=httpx.MockTransport(ok_upstream))


def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 40001)), base_url="http://test"
    )


async def status_of(app):
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            return (await client.get("/admin/status")).json()


@pytest.mark.asyncio
async def test_boot_alone_is_not_an_operator_activation(env, tmp_path):
    status = await status_of(persisted_app(tmp_path))
    assert status["operator_activated"] is False
    # the revision *is* active and routable — first run is about taking charge
    # of it, not about a gateway that cannot serve
    assert status["active"]["version"] == "cerberus-2026-07-16.1"


@pytest.mark.asyncio
async def test_activating_the_bootstrap_config_ends_first_run(env, tmp_path):
    app = persisted_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            boot_path = (await client.get("/admin/status")).json()["active"]["source_path"]

            valid = await client.post("/admin/validate", json={"path": boot_path}, headers=CSRF)
            assert valid.status_code == 200, valid.text
            assert valid.json()["valid"] is True

            activated = await client.post("/admin/activate", json={"path": boot_path}, headers=CSRF)
            assert activated.status_code == 200, activated.text

            after = (await client.get("/admin/status")).json()
            assert after["operator_activated"] is True


@pytest.mark.asyncio
async def test_operator_activation_survives_restart(env, tmp_path):
    app = persisted_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            boot_path = (await client.get("/admin/status")).json()["active"]["source_path"]
            await client.post("/admin/activate", json={"path": boot_path}, headers=CSRF)

    # a second process over the same state database — the console must not fall
    # back into first run just because the gateway restarted
    restarted = await status_of(persisted_app(tmp_path))
    assert restarted["operator_activated"] is True


@pytest.mark.asyncio
async def test_without_a_control_plane_activation_is_process_local(env, tmp_path):
    """state.path unset: nothing persists, so the only truthful answer is this
    process's own activation history."""

    doc = load_config_document(write_config(tmp_path, "active.yaml", raw_config("cerberus-2026-07-16.1")))
    app = create_app(doc, http_transport=httpx.MockTransport(ok_upstream))
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            assert (await client.get("/admin/status")).json()["operator_activated"] is False
            boot_path = (await client.get("/admin/status")).json()["active"]["source_path"]
            await client.post("/admin/activate", json={"path": boot_path}, headers=CSRF)
            assert (await client.get("/admin/status")).json()["operator_activated"] is True


@pytest.mark.asyncio
async def test_staging_nothing_is_refused_not_turned_into_a_candidate(env, tmp_path):
    """An empty edit used to stage a re-serialized copy of the active document
    under its own version, which /admin/validate must then refuse because that
    version is already bound to another checksum — a candidate that could never
    be activated. The endpoint says so instead."""

    app = persisted_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            response = await client.post("/admin/config/stage", json={"updates": {}}, headers=CSRF)
            assert response.status_code == 409, response.text
            body = response.json()
            assert body["staged"] is False
            assert body["reason"] == "no_changes"


@pytest.mark.asyncio
async def test_a_real_edit_still_stages_validates_and_activates(env, tmp_path):
    app = persisted_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            staged = await client.post(
                "/admin/config/stage",
                json={"updates": {"providers.alpha.quota_cooldown_seconds": 123}},
                headers=CSRF,
            )
            assert staged.status_code == 200, staged.text
            path = staged.json()["path"]
            # the edit bumps the version, so the candidate is a new revision
            assert staged.json()["version"] != "cerberus-2026-07-16.1"

            assert (await client.post("/admin/validate", json={"path": path}, headers=CSRF)).status_code == 200
            activated = await client.post("/admin/activate", json={"path": path}, headers=CSRF)
            assert activated.status_code == 200, activated.text

            after = (await client.get("/admin/status")).json()
            assert after["operator_activated"] is True
            assert after["active"]["version"] == staged.json()["version"]
