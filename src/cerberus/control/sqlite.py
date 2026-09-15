"""SQLite control and operational state for Cerberus Dispatch.

Configuration is authored as a validated ``ConfigDocument`` and materialized
into revision-scoped tables.  The active document is loaded once and held by
``ConfigLifecycle``; request routing never builds policy with ad-hoc SQL.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cerberus.registry.loader import ConfigDocument, require_configured_credentials
from cerberus.registry.schema import CerberusConfig


_SCHEMA = """
CREATE TABLE IF NOT EXISTS control_schema (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL
);
INSERT OR IGNORE INTO control_schema(singleton, version) VALUES (1, 1);

CREATE TABLE IF NOT EXISTS config_revisions (
    revision TEXT PRIMARY KEY,
    checksum TEXT NOT NULL UNIQUE,
    source_path TEXT NOT NULL,
    document_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_aliases (
    revision TEXT NOT NULL REFERENCES config_revisions(revision) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    mode TEXT NOT NULL,
    allow_paid_fallback INTEGER NOT NULL,
    allow_experimental INTEGER NOT NULL,
    PRIMARY KEY (revision, alias)
);

CREATE TABLE IF NOT EXISTS service_catalog (
    revision TEXT NOT NULL REFERENCES config_revisions(revision) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    base_url TEXT NOT NULL,
    protocol TEXT NOT NULL,
    maturity TEXT NOT NULL,
    cost_tier TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    PRIMARY KEY (revision, provider, model)
);

CREATE TABLE IF NOT EXISTS alias_bindings (
    revision TEXT NOT NULL,
    alias TEXT NOT NULL,
    binding_type TEXT NOT NULL,
    PRIMARY KEY (revision, alias),
    FOREIGN KEY (revision, alias) REFERENCES service_aliases(revision, alias) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS route_paths (
    revision TEXT NOT NULL,
    alias TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    provider TEXT NOT NULL,
    credential_ref TEXT NOT NULL,
    model TEXT NOT NULL,
    cost_tier TEXT NOT NULL,
    PRIMARY KEY (revision, alias, ordinal),
    FOREIGN KEY (revision, alias) REFERENCES service_aliases(revision, alias) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS route_members (
    revision TEXT NOT NULL,
    alias TEXT NOT NULL,
    role TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    provider TEXT NOT NULL,
    credential_ref TEXT NOT NULL,
    model TEXT NOT NULL,
    PRIMARY KEY (revision, alias, role, ordinal),
    FOREIGN KEY (revision, alias) REFERENCES service_aliases(revision, alias) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS identity_alias_policy (
    revision TEXT NOT NULL REFERENCES config_revisions(revision) ON DELETE CASCADE,
    identity TEXT NOT NULL,
    alias TEXT NOT NULL,
    mode TEXT NOT NULL,
    credential_ref TEXT,
    jwt_client_id TEXT,
    PRIMARY KEY (revision, identity, alias)
);

CREATE TABLE IF NOT EXISTS provider_credentials_ref (
    revision TEXT NOT NULL REFERENCES config_revisions(revision) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    credential_ref TEXT NOT NULL,
    secret_env_ref TEXT NOT NULL,
    PRIMARY KEY (revision, provider, credential_ref)
);

CREATE TABLE IF NOT EXISTS control_activation (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    active_revision TEXT NOT NULL REFERENCES config_revisions(revision),
    checksum TEXT NOT NULL,
    activated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_health (
    provider TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('unknown', 'healthy', 'degraded', 'down')),
    checked_at TEXT NOT NULL,
    latency_ms REAL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS routing_events (
    request_id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    identity TEXT,
    alias TEXT NOT NULL,
    revision TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_records (
    request_id TEXT PRIMARY KEY REFERENCES routing_events(request_id) ON DELETE CASCADE,
    provider TEXT,
    model TEXT,
    cost_tier TEXT,
    usage_json TEXT,
    monetary_cost REAL
);

CREATE TABLE IF NOT EXISTS audit_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    action TEXT NOT NULL,
    revision TEXT,
    checksum TEXT,
    detail_json TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteControlPlane:
    """Revision store plus bounded operational facts for one Cerberus node."""

    def __init__(self, path: str | Path) -> None:
        db_path = Path(path).resolve()
        self._path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(db_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(_SCHEMA)
        schema_version = self._connection.execute(
            "SELECT version FROM control_schema WHERE singleton=1"
        ).fetchone()[0]
        if schema_version != 1:
            self._connection.close()
            raise RuntimeError(
                f"unsupported Cerberus control schema version {schema_version}; expected 1"
            )
        self._connection.commit()
        self._health = self._load_health()

    def _load_health(self) -> dict[str, str]:
        rows = self._connection.execute("SELECT provider, status FROM provider_health").fetchall()
        return {str(row["provider"]): str(row["status"]) for row in rows}

    def bootstrap(self, initial: ConfigDocument) -> ConfigDocument:
        """Seed an empty database, or restore its atomically active revision."""

        with self._lock:
            row = self._connection.execute(
                "SELECT active_revision FROM control_activation WHERE singleton=1"
            ).fetchone()
            if row is None:
                self.register(initial)
                self.activate(initial, action="bootstrap")
                return initial
            restored = self.load(str(row["active_revision"]))
            require_configured_credentials(restored.config)
            return restored

    def revision_checksums(self) -> dict[str, str]:
        with self._lock:
            rows = self._connection.execute("SELECT revision, checksum FROM config_revisions").fetchall()
        return {str(row["revision"]): str(row["checksum"]) for row in rows}

    def register(self, document: ConfigDocument) -> None:
        configured_path = document.config.state.path
        if configured_path is None or Path(configured_path).resolve() != self._path:
            raise ValueError(
                "state.path is a boot-time control-plane locator and cannot change "
                "between persisted revisions"
            )
        payload = document.config.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT checksum FROM config_revisions WHERE revision=?", (document.version,)
            ).fetchone()
            if existing is not None:
                if existing["checksum"] != document.checksum:
                    raise ValueError(
                        f"configuration version {document.version!r} is already bound to checksum "
                        f"{existing['checksum']}; candidate checksum is {document.checksum}"
                    )
                return
            self._connection.execute(
                "INSERT INTO config_revisions VALUES (?, ?, ?, ?, ?)",
                (document.version, document.checksum, document.source_path, encoded, _now()),
            )
            self._materialize(document)
            self._audit("revision_registered", document, {})

    def _materialize(self, document: ConfigDocument) -> None:
        config = document.config
        revision = document.version
        for provider_name, provider in config.providers.items():
            for model_name, model in provider.models.items():
                self._connection.execute(
                    "INSERT INTO service_catalog VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        revision,
                        provider_name,
                        model_name,
                        str(provider.base_url),
                        provider.protocol,
                        provider.maturity,
                        model.cost_tier,
                        json.dumps(model.capabilities, sort_keys=True),
                    ),
                )
            for credential_name, credential in provider.credentials.items():
                self._connection.execute(
                    "INSERT INTO provider_credentials_ref VALUES (?, ?, ?, ?)",
                    (revision, provider_name, credential_name, credential.api_key_env),
                )
        for alias_name, alias in config.aliases.items():
            self._connection.execute(
                "INSERT INTO service_aliases VALUES (?, ?, ?, ?, ?)",
                (revision, alias_name, alias.mode, alias.allow_paid_fallback, alias.allow_experimental),
            )
            self._connection.execute(
                "INSERT INTO alias_bindings VALUES (?, ?, ?)",
                (revision, alias_name, "fusion" if alias.mode == "fusion" else "dedicated"),
            )
            for ordinal, candidate in enumerate(alias.candidates):
                model = config.resolve_model(candidate, context=f"alias {alias_name!r} candidate")
                self._connection.execute(
                    "INSERT INTO route_paths VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (revision, alias_name, ordinal, candidate.provider, candidate.credential, candidate.model, model.cost_tier),
                )
                if alias.mode == "fusion":
                    self._connection.execute(
                        "INSERT INTO route_members VALUES (?, ?, 'panel', ?, ?, ?, ?)",
                        (revision, alias_name, ordinal, candidate.provider, candidate.credential, candidate.model),
                    )
            if alias.fusion is not None:
                for role, candidate in (("analyst", alias.fusion.judge), ("outer", alias.fusion.outer_model)):
                    self._connection.execute(
                        "INSERT INTO route_members VALUES (?, ?, ?, 0, ?, ?, ?)",
                        (revision, alias_name, role, candidate.provider, candidate.credential, candidate.model),
                    )
        for identity_name, identity in config.identities.items():
            for alias_name in identity.allowed_aliases:
                self._connection.execute(
                    "INSERT INTO identity_alias_policy VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        revision,
                        identity_name,
                        alias_name,
                        config.aliases[alias_name].mode,
                        identity.credential_env,
                        identity.jwt_client_id,
                    ),
                )

    def activate(self, document: ConfigDocument, *, action: str = "activate") -> None:
        self.register(document)
        when = _now()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO control_activation(singleton, active_revision, checksum, activated_at) VALUES (1, ?, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET active_revision=excluded.active_revision, "
                "checksum=excluded.checksum, activated_at=excluded.activated_at",
                (document.version, document.checksum, when),
            )
            self._audit(action, document, {"activated_at": when})

    def load(self, revision: str) -> ConfigDocument:
        with self._lock:
            row = self._connection.execute(
                "SELECT checksum, source_path, document_json FROM config_revisions WHERE revision=?",
                (revision,),
            ).fetchone()
        if row is None:
            raise LookupError(f"unknown configuration revision {revision!r}")
        return ConfigDocument(
            config=CerberusConfig.model_validate(json.loads(row["document_json"])),
            version=revision,
            checksum=str(row["checksum"]),
            source_path=str(row["source_path"]),
        )

    def status(self) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT active_revision, checksum, activated_at FROM control_activation WHERE singleton=1"
            ).fetchone()
        return {"storage": "sqlite", **(dict(row) if row is not None else {})}

    def provider_available(self, provider: str) -> bool:
        """Health may exclude a configured provider; it can never add one."""

        return self._health.get(provider, "unknown") != "down"

    def set_provider_health(
        self,
        provider: str,
        status: str,
        *,
        latency_ms: float | None = None,
        detail: str | None = None,
    ) -> None:
        if status not in {"unknown", "healthy", "degraded", "down"}:
            raise ValueError(f"invalid provider health status {status!r}")
        checked_at = _now()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO provider_health VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(provider) DO UPDATE SET status=excluded.status, checked_at=excluded.checked_at, "
                "latency_ms=excluded.latency_ms, detail=excluded.detail",
                (provider, status, checked_at, latency_ms, detail),
            )
            self._health = {**self._health, provider: status}

    def record_routing_event(self, payload: dict[str, Any]) -> None:
        """Persist a redacted event and its accounting projection atomically."""

        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        usage = payload.get("token_usage")
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO routing_events VALUES (?, ?, ?, ?, ?, ?)",
                (
                    payload["request_id"], payload["timestamp"], payload.get("identity"),
                    payload["alias"], payload.get("config_version"), encoded,
                ),
            )
            self._connection.execute(
                "INSERT OR REPLACE INTO usage_records VALUES (?, ?, ?, ?, ?, ?)",
                (
                    payload["request_id"], payload.get("provider"), payload.get("model"),
                    payload.get("cost_tier"), json.dumps(usage, sort_keys=True) if usage is not None else None,
                    payload.get("reported_cost"),
                ),
            )

    def _audit(self, action: str, document: ConfigDocument, detail: dict[str, Any]) -> None:
        self._connection.execute(
            "INSERT INTO audit_records(occurred_at, action, revision, checksum, detail_json) VALUES (?, ?, ?, ?, ?)",
            (_now(), action, document.version, document.checksum, json.dumps(detail, sort_keys=True)),
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()
