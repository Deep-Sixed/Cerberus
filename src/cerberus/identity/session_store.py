"""Shared backing store for admin SSO state — OIDC pending logins and sessions.

Both live in this store rather than in per-process dicts: with more than one
uvicorn worker a login begun on one worker callbacks on another, and a login
begun before a restart must still complete after it. The SQLite backend is the
same proportionate-persistence choice as the cooldown store (one file, no
Postgres, no vectors); the in-memory backend keeps tests and dry runs dependency-free.

Single-use pending state is enforced atomically with DELETE ... RETURNING so two
concurrent callbacks for the same state can never both succeed.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(slots=True)
class AdminSession:
    sub: str
    email: str
    name: str
    groups: list[str]
    expires: float


# nonce, verifier, expires — the PKCE/replay material stashed between login start and callback
Pending = tuple[str, str, float]


class SessionStore(Protocol):
    def put_pending(self, state: str, nonce: str, verifier: str, expires: float) -> None: ...
    def pop_pending(self, state: str, *, now: float | None = None) -> Pending | None: ...
    def put_session(self, sid: str, session: AdminSession) -> None: ...
    def get_session(self, sid: str, *, now: float | None = None) -> AdminSession | None: ...
    def delete_session(self, sid: str) -> None: ...
    def close(self) -> None: ...


class InMemorySessionStore:
    """Process-local backend — correct only for a single worker (tests, dry runs)."""

    def __init__(self) -> None:
        self._pending: dict[str, Pending] = {}
        self._sessions: dict[str, AdminSession] = {}

    def put_pending(self, state: str, nonce: str, verifier: str, expires: float) -> None:
        self._pending[state] = (nonce, verifier, expires)

    def pop_pending(self, state: str, *, now: float | None = None) -> Pending | None:
        current = time.time() if now is None else now
        self._pending = {s: v for s, v in self._pending.items() if v[2] > current}  # gc
        return self._pending.pop(state, None)

    def put_session(self, sid: str, session: AdminSession) -> None:
        self._sessions[sid] = session

    def get_session(self, sid: str, *, now: float | None = None) -> AdminSession | None:
        current = time.time() if now is None else now
        session = self._sessions.get(sid)
        if session is None:
            return None
        if current > session.expires:
            self._sessions.pop(sid, None)
            return None
        return session

    def delete_session(self, sid: str) -> None:
        self._sessions.pop(sid, None)

    def close(self) -> None:  # symmetry with the sqlite backend
        pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS oidc_pending (
    state TEXT PRIMARY KEY,
    nonce TEXT NOT NULL,
    verifier TEXT NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_sessions (
    sid TEXT PRIMARY KEY,
    sub TEXT NOT NULL,
    email TEXT NOT NULL,
    name TEXT NOT NULL,
    groups TEXT NOT NULL,
    expires REAL NOT NULL
);
"""


class SqliteSessionStore:
    """Shared across workers and across restarts — one SQLite file, WAL for
    multi-process reads/writes. Rows are lazily evicted on the read that finds
    them expired, matching the cooldown store's approach."""

    def __init__(self, path: str | Path) -> None:
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(db_path, check_same_thread=False)
        # WAL + a busy timeout so concurrent workers don't trip over each other's writes
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def put_pending(self, state: str, nonce: str, verifier: str, expires: float) -> None:
        self._connection.execute(
            "INSERT INTO oidc_pending (state, nonce, verifier, expires) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(state) DO UPDATE SET nonce=excluded.nonce,"
            " verifier=excluded.verifier, expires=excluded.expires",
            (state, nonce, verifier, expires),
        )
        self._connection.commit()

    def pop_pending(self, state: str, *, now: float | None = None) -> Pending | None:
        current = time.time() if now is None else now
        # atomic single-use: the DELETE both consumes the row and returns it, so two
        # concurrent callbacks for one state cannot both read it as still-present
        row = self._connection.execute(
            "DELETE FROM oidc_pending WHERE state=? RETURNING nonce, verifier, expires",
            (state,),
        ).fetchone()
        self._connection.execute("DELETE FROM oidc_pending WHERE expires <= ?", (current,))  # gc
        self._connection.commit()
        if row is None or row[2] <= current:
            return None
        return (row[0], row[1], row[2])

    def put_session(self, sid: str, session: AdminSession) -> None:
        self._connection.execute(
            "INSERT INTO admin_sessions (sid, sub, email, name, groups, expires)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(sid) DO UPDATE SET sub=excluded.sub, email=excluded.email,"
            " name=excluded.name, groups=excluded.groups, expires=excluded.expires",
            (sid, session.sub, session.email, session.name, json.dumps(session.groups), session.expires),
        )
        self._connection.commit()

    def get_session(self, sid: str, *, now: float | None = None) -> AdminSession | None:
        current = time.time() if now is None else now
        row = self._connection.execute(
            "SELECT sub, email, name, groups, expires FROM admin_sessions WHERE sid=?",
            (sid,),
        ).fetchone()
        if row is None:
            return None
        sub, email, name, groups, expires = row
        if current > expires:
            self._connection.execute("DELETE FROM admin_sessions WHERE sid=?", (sid,))
            self._connection.commit()
            return None
        return AdminSession(sub=sub, email=email, name=name, groups=json.loads(groups), expires=expires)

    def delete_session(self, sid: str) -> None:
        self._connection.execute("DELETE FROM admin_sessions WHERE sid=?", (sid,))
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()
