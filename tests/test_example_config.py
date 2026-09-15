"""The shipped example config must always validate."""

from pathlib import Path

from cerberus.registry import load_config_document

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "cerberus.example.yaml"


def test_example_config_validates():
    doc = load_config_document(EXAMPLE, validate_credentials=False)
    assert doc.version == "cerberus-2026-07-16.1"
    assert set(doc.config.aliases) == {"cerberus/free", "cerberus/dispatch-standard", "cerberus/fusion-code"}
