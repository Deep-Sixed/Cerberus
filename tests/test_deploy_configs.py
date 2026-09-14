"""The shipped deployable configs must validate, expose both slices, and hold free-only.

config/dev.yaml (native) and config/example.yaml (container default) are the
bring-up artifacts. These lock in that they load against the live schema, carry
the Dispatch and Free Router aliases, and that free mode can never be widened to
a paid candidate without failing the loader.
"""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from cerberus.registry import CerberusConfig, load_config_document

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
DEPLOY_CONFIGS = ("dev.yaml", "example.yaml")


@pytest.mark.parametrize("name", DEPLOY_CONFIGS)
def test_deploy_config_validates_and_carries_both_slices(name):
    doc = load_config_document(CONFIG_DIR / name, validate_credentials=False)
    modes = {alias: spec.mode for alias, spec in doc.config.aliases.items()}
    assert modes == {"cerberus/dispatch-dev": "dispatch", "cerberus/free-dev": "free"}
    dev = doc.config.identities["dev"]
    assert set(dev.allowed_modes) == {"dispatch", "free"}
    assert set(dev.allowed_aliases) == {"cerberus/dispatch-dev", "cerberus/free-dev"}


@pytest.mark.parametrize("name", DEPLOY_CONFIGS)
def test_free_alias_cannot_be_widened_to_a_paid_candidate(name):
    raw = yaml.safe_load((CONFIG_DIR / name).read_text())
    # the registry declares a paid model; adding it to the free alias must fail
    raw["aliases"]["cerberus/free-dev"]["candidates"].append(
        {"provider": "openrouter", "credential": "main", "model": "anthropic/claude-sonnet-4.5"}
    )
    with pytest.raises(ValidationError, match="free-mode but lists a paid candidate"):
        CerberusConfig.model_validate(raw)


def test_local_profile_is_valid_cerberus_schema():
    """The local example uses the current schema and a loopback-only upstream."""
    doc = load_config_document(CONFIG_DIR / "local.example.yaml", validate_credentials=False)
    assert doc.config.aliases["cerberus/local"].mode == "dispatch"
    assert str(doc.config.providers["local"].base_url) == "http://127.0.0.1:8080/v1"
    assert doc.config.telemetry.endpoint is None


def test_fusion_dev_config_is_valid():
    """The fusion bundle config validates and carries the worker binding + guard identity."""
    doc = load_config_document(CONFIG_DIR / "fusion-dev.yaml", validate_credentials=False)
    assert doc.config.fusion_worker.endpoint is not None
    # the worker's own identity excludes fusion (recursion guard)
    worker_identity = doc.config.identities["fusion-worker"]
    assert "fusion" not in worker_identity.allowed_modes
