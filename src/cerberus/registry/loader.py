"""Load a Cerberus config document: parse, validate, checksum, fail closed on missing secrets."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from cerberus.registry.schema import CerberusConfig

DEFAULT_CONFIG_PATH = Path("/etc/cerberus/config.yaml")
CONFIG_ENV = "CERBERUS_CONFIG"


@dataclass(frozen=True, slots=True)
class ConfigDocument:
    """An immutable, content-addressed configuration snapshot."""

    config: CerberusConfig
    version: str
    checksum: str
    source_path: str


def required_credential_envs(config: CerberusConfig) -> list[str]:
    names: set[str] = set()
    for provider in config.providers.values():
        for credential in provider.credentials.values():
            names.add(credential.api_key_env)
    for identity in config.identities.values():
        if identity.credential_env is not None:
            names.add(identity.credential_env)
    if config.server.api_token_env:
        names.add(config.server.api_token_env)
    if config.server.admin_token_env:
        names.add(config.server.admin_token_env)
    return sorted(names)


def require_configured_credentials(config: CerberusConfig) -> None:
    missing = [name for name in required_credential_envs(config) if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError(f"Missing credential environment variables: {', '.join(missing)}")
    server = config.server
    if server.admin_token_env and server.api_token_env:
        # real credential separation: two names resolving to one value would let
        # the inference token pass admin checks. Compare values here; the error
        # names only the environment variables, never their contents.
        if os.environ.get(server.admin_token_env, "") == os.environ.get(server.api_token_env, ""):
            raise RuntimeError(
                f"{server.admin_token_env} and {server.api_token_env} must hold distinct credentials"
            )
    token_file = config.telemetry.bearer_token_file
    if token_file is not None:
        path = Path(token_file)
        if not path.is_file() or not os.access(path, os.R_OK):
            raise RuntimeError(f"Telemetry bearer token file is not readable: {token_file}")


def load_config_document(
    path: str | Path | None = None,
    *,
    validate_credentials: bool = True,
) -> ConfigDocument:
    config_path = Path(path or os.environ.get(CONFIG_ENV, DEFAULT_CONFIG_PATH))
    raw_bytes = config_path.read_bytes()
    parsed = yaml.safe_load(raw_bytes)
    config = CerberusConfig.model_validate(parsed)
    if validate_credentials:
        require_configured_credentials(config)
    checksum = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
    return ConfigDocument(
        config=config,
        version=config.metadata.version,
        checksum=checksum,
        source_path=str(config_path),
    )
