"""Session 1 acceptance tests — config document loading, checksum, credential fail-closed."""

import pytest
import yaml

from cerberus.registry import load_config_document
from cerberus.registry.loader import required_credential_envs
from tests.test_schema import make


def write_config(tmp_path, raw) -> str:
    path = tmp_path / "cerberus.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return str(path)


def test_document_carries_version_and_checksum(tmp_path):
    path = write_config(tmp_path, make())
    doc = load_config_document(path, validate_credentials=False)
    assert doc.version == "cerberus-2026-07-16.1"
    assert doc.checksum.startswith("sha256:") and len(doc.checksum) == len("sha256:") + 64
    assert doc.config.aliases["cerberus/free"].mode == "free"


def test_checksum_is_content_addressed(tmp_path):
    path_a = write_config(tmp_path, make())
    doc_a = load_config_document(path_a, validate_credentials=False)
    raw_b = make()
    raw_b["metadata"]["version"] = "cerberus-2026-07-16.2"
    path_b = tmp_path / "b.yaml"
    path_b.write_text(yaml.safe_dump(raw_b), encoding="utf-8")
    doc_b = load_config_document(str(path_b), validate_credentials=False)
    assert doc_a.checksum != doc_b.checksum
    # identical bytes -> identical checksum
    doc_a2 = load_config_document(path_a, validate_credentials=False)
    assert doc_a.checksum == doc_a2.checksum


def test_missing_credential_env_fails_closed(tmp_path, monkeypatch):
    for name in ("OPENROUTER_API_KEY", "GEMINI_KEY_1", "GEMINI_KEY_2", "CB_KEY_RECON"):
        monkeypatch.delenv(name, raising=False)
    path = write_config(tmp_path, make())
    with pytest.raises(RuntimeError, match="GEMINI_KEY_1"):
        load_config_document(path)


def test_present_credentials_pass(tmp_path, monkeypatch):
    for name in ("OPENROUTER_API_KEY", "GEMINI_KEY_1", "GEMINI_KEY_2", "CB_KEY_RECON"):
        monkeypatch.setenv(name, "test-value")
    path = write_config(tmp_path, make())
    doc = load_config_document(path)
    assert doc.config.identities["recon"].credential_env == "CB_KEY_RECON"


ADMIN_SSO_BLOCK = {
    "authorize_url": "https://idp.example/authorize",
    "token_url": "https://idp.example/token",
    "jwks_url": "https://idp.example/jwks",
    "issuer": "https://idp.example",
    "client_id_env": "SSO_CLIENT_ID",
    "client_secret_env": "SSO_CLIENT_SECRET",
    "redirect_uri": "https://cerberus.example/admin/callback",
    "admin_groups": ["admins"],
    "session_secret_env": "SSO_SESSION_SECRET",
}
SSO_ENVS = ("SSO_SESSION_SECRET", "SSO_CLIENT_ID", "SSO_CLIENT_SECRET")


def _sso_config(monkeypatch):
    for name in ("OPENROUTER_API_KEY", "GEMINI_KEY_1", "GEMINI_KEY_2", "CB_KEY_RECON"):
        monkeypatch.setenv(name, "test-value")
    raw = make()
    raw["admin_sso"] = dict(ADMIN_SSO_BLOCK)
    return raw


@pytest.mark.parametrize("missing", SSO_ENVS)
def test_missing_admin_sso_secret_fails_closed(tmp_path, monkeypatch, missing):
    """An unset session_secret_env would sign admin cookies with an empty HMAC key;
    the client id/secret are equally required to complete an OIDC exchange. All
    three fail closed at boot like every other credential reference."""

    for name in SSO_ENVS:
        monkeypatch.setenv(name, "sso-value")
    monkeypatch.delenv(missing, raising=False)
    path = write_config(tmp_path, _sso_config(monkeypatch))
    with pytest.raises(RuntimeError, match=missing):
        load_config_document(path)


def test_admin_sso_secrets_are_required_credentials(tmp_path, monkeypatch):
    for name in SSO_ENVS:
        monkeypatch.setenv(name, "sso-value")
    path = write_config(tmp_path, _sso_config(monkeypatch))
    doc = load_config_document(path)
    assert set(SSO_ENVS) <= set(required_credential_envs(doc.config))


def test_admin_sso_secrets_unrequired_when_sso_absent(tmp_path, monkeypatch):
    for name in SSO_ENVS:
        monkeypatch.delenv(name, raising=False)
    for name in ("OPENROUTER_API_KEY", "GEMINI_KEY_1", "GEMINI_KEY_2", "CB_KEY_RECON"):
        monkeypatch.setenv(name, "test-value")
    path = write_config(tmp_path, make())
    doc = load_config_document(path)  # no admin_sso block -> unchanged behavior
    assert not set(SSO_ENVS) & set(required_credential_envs(doc.config))
