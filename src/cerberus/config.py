"""Portable Cerberus configuration loaded from YAML and environment names."""

from pathlib import Path
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator
import yaml

DEFAULT_CONFIG_PATH = Path("/etc/cerberus/config.yaml")
MAX_ROUTING_LABEL_LENGTH = 100


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = Field(default=4101, ge=1, le=65535)
    api_token_env: str | None = None


class TelemetryConfig(BaseModel):
    """Optional redacted routing-event sink."""

    model_config = ConfigDict(extra="forbid")

    endpoint: HttpUrl | None = None
    bearer_token_file: Path | None = None
    timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    queue_capacity: int = Field(default=256, ge=1, le=4096)

    @model_validator(mode="after")
    def validate_sink(self) -> "TelemetryConfig":
        if (self.endpoint is None) != (self.bearer_token_file is None):
            raise ValueError("telemetry endpoint and bearer_token_file must be configured together")
        return self


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: Literal["openai_chat"] = "openai_chat"
    base_url: HttpUrl
    api_key_env: str | None = None
    model: str = Field(min_length=1)
    cooldown_seconds: int = Field(default=60, ge=1)


class PoolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["first", "random", "round_robin"] = "first"
    providers: list[str] = Field(min_length=1)


class RoutingRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match: dict[str, str | bool]
    pool: str
    fallback_pool: str | None = None


class RouterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server: ServerConfig = Field(default_factory=ServerConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    providers: dict[str, ProviderConfig] = Field(min_length=1)
    pools: dict[str, PoolConfig] = Field(min_length=1)
    routing_rules: list[RoutingRule] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_references(self) -> "RouterConfig":
        if self.server.host not in {"127.0.0.1", "::1", "localhost"} and not self.server.api_token_env:
            raise ValueError("non-loopback server.host requires server.api_token_env")
        for pool_name, pool in self.pools.items():
            unknown = sorted(set(pool.providers) - set(self.providers))
            if unknown:
                raise ValueError(f"pool {pool_name!r} references unknown providers: {', '.join(unknown)}")
        for rule in self.routing_rules:
            request_type = rule.match.get("request_type")
            if request_type is not None and (
                not isinstance(request_type, str) or not request_type or len(request_type) > MAX_ROUTING_LABEL_LENGTH
            ):
                raise ValueError(
                    f"routing rule request_type must be a non-empty string up to {MAX_ROUTING_LABEL_LENGTH} characters"
                )
            if rule.pool not in self.pools:
                raise ValueError(f"routing rule references unknown pool: {rule.pool}")
            if rule.fallback_pool is not None and rule.fallback_pool not in self.pools:
                raise ValueError(f"routing rule references unknown fallback pool: {rule.fallback_pool}")
        if not any(rule.match.get("default") is True for rule in self.routing_rules):
            raise ValueError("routing_rules must include one default rule")
        return self

    def require_configured_credentials(self) -> None:
        required_names = [provider.api_key_env for provider in self.providers.values() if provider.api_key_env]
        if self.server.api_token_env:
            required_names.append(self.server.api_token_env)
        missing = sorted(name for name in required_names if not os.environ.get(name, "").strip())
        if missing:
            raise RuntimeError(f"Missing provider credential environment variables: {', '.join(missing)}")
        if self.telemetry.bearer_token_file is not None:
            token_file = self.telemetry.bearer_token_file
            if not token_file.is_file() or not os.access(token_file, os.R_OK):
                raise RuntimeError(f"Telemetry bearer token file is not readable: {token_file}")


def load_config(path: str | Path | None = None, *, validate_credentials: bool = True) -> RouterConfig:
    config_path = Path(path or os.environ.get("CERBERUS_CONFIG", DEFAULT_CONFIG_PATH))
    with config_path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    config = RouterConfig.model_validate(raw)
    if validate_credentials:
        config.require_configured_credentials()
    return config
