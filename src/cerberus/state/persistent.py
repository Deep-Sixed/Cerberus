"""SQLite-backed cooldown store — same interface as the in-memory store, survives restart.

Single-host persistence (see docs/persistence.md): one file, one table, absolute
wall-clock retry_at. Postgres is deliberately not required.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from cerberus.state.cooldowns import Cooldown, CooldownScope

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cooldowns (
    provider TEXT NOT NULL,
    credential TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL,
    reason TEXT NOT NULL,
    retry_at REAL NOT NULL,
    PRIMARY KEY (provider, credential, model)
)
"""


class SqliteCooldownStore:
    def __init__(self, path: str | Path) -> None:
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._lock:
            self._connection = sqlite3.connect(db_path, check_same_thread=False)
            self._connection.execute(_SCHEMA)
            self._connection.commit()

    @staticmethod
    def _key(scope: CooldownScope, credential: str | None, model: str | None) -> tuple[str, str]:
        if scope == "provider":
            return ("", "")
        if scope == "credential":
            return (credential or "", "")
        return (credential or "", model or "")

    def apply(
        self,
        *,
        scope: CooldownScope,
        provider: str,
        credential: str | None,
        model: str | None,
        reason: str,
        duration_seconds: float,
        now: float | None = None,
    ) -> Cooldown:
        started = time.time() if now is None else now
        retry_at = started + max(1.0, duration_seconds)
        credential_key, model_key = self._key(scope, credential, model)
        with self._lock:
            cursor = self._connection.execute(
                "INSERT INTO cooldowns ("
                "    provider,"
                "    credential,"
                "    model,"
                "    scope,"
                "    reason,"
                "    retry_at"
                ")"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(provider, credential, model)"
                " DO UPDATE SET"
                "    scope = CASE"
                "        WHEN excluded.retry_at > cooldowns.retry_at"
                "        THEN excluded.scope"
                "        ELSE cooldowns.scope"
                "    END,"
                "    reason = CASE"
                "        WHEN excluded.retry_at > cooldowns.retry_at"
                "        THEN excluded.reason"
                "        ELSE cooldowns.reason"
                "    END,"
                "    retry_at = MAX(cooldowns.retry_at, excluded.retry_at)"
                " RETURNING scope, reason, retry_at",
                (provider, credential_key, model_key, scope, reason, retry_at),
            )
            winning_scope, winning_reason, winning_retry_at = cursor.fetchone()
            self._connection.commit()
        return Cooldown(
            scope=winning_scope,
            provider=provider,
            credential=credential_key or None,
            model=model_key or None,
            reason=winning_reason,
            retry_at=winning_retry_at,
        )

    def active_for(
        self, provider: str, credential: str, model: str, *, now: float | None = None
    ) -> Cooldown | None:
        current = time.time() if now is None else now
        with self._lock:
            for credential_key, model_key in ((credential, model), (credential, ""), ("", "")):
                row = self._connection.execute(
                    "SELECT scope, reason, retry_at FROM cooldowns WHERE provider=? AND credential=? AND model=?",
                    (provider, credential_key, model_key),
                ).fetchone()
                if row is None:
                    continue
                scope, reason, retry_at = row
                if retry_at > current:
                    return Cooldown(
                        scope=scope, provider=provider,
                        credential=credential_key or None, model=model_key or None,
                        reason=reason, retry_at=retry_at,
                    )
                self._connection.execute(
                    "DELETE FROM cooldowns WHERE provider=? AND credential=? AND model=? AND retry_at <= ?",
                    (provider, credential_key, model_key, current),
                )
                self._connection.commit()
            return None

    def snapshot(self, *, now: float | None = None) -> list[dict]:
        current = time.time() if now is None else now
        with self._lock:
            rows = self._connection.execute(
                "SELECT provider, credential, model, scope, reason, retry_at FROM cooldowns WHERE retry_at > ?",
                (current,),
            ).fetchall()
        active = [
            {
                "scope": scope,
                "provider": provider,
                "credential": credential or None,
                "model": model or None,
                "reason": reason,
                "seconds_remaining": round(retry_at - current, 2),
            }
            for provider, credential, model, scope, reason, retry_at in rows
        ]
        return sorted(active, key=lambda entry: (entry["provider"], entry["scope"]))

    def close(self) -> None:
        with self._lock:
            self._connection.close()
