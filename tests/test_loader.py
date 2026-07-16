"""Session 1 acceptance tests — config document loading, checksum, credential fail-closed."""

import pytest
import yaml

from cerberus.registry import load_config_document
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
