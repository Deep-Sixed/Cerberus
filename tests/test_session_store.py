"""SQLite session store — the shared backing that lets SSO survive restarts and
span workers. Two store instances over one file stand in for two uvicorn workers."""

import time

import pytest

from cerberus.identity.session_store import (
    AdminSession,
    InMemorySessionStore,
    SqliteSessionStore,
)


def a_session(expires):
    return AdminSession(sub="u1", email="op@x", name="Op", groups=["authentik Admins"], expires=expires)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    s = InMemorySessionStore() if request.param == "memory" else SqliteSessionStore(tmp_path / "s.sqlite3")
    yield s
    s.close()


def test_session_roundtrip_and_expiry(store):
    store.put_session("sid1", a_session(time.time() + 100))
    got = store.get_session("sid1")
    assert got is not None and got.groups == ["authentik Admins"]
    # expired sessions read as absent and are evicted
    store.put_session("sid2", a_session(time.time() - 1))
    assert store.get_session("sid2") is None
    store.delete_session("sid1")
    assert store.get_session("sid1") is None


def test_pending_is_single_use_and_expires(store):
    store.put_pending("state-a", "nonce", "verifier", time.time() + 100)
    first = store.pop_pending("state-a")
    assert first == ("nonce", "verifier", first[2])
    # a second pop of the same state returns nothing — replay is impossible
    assert store.pop_pending("state-a") is None
    # already-expired pending never resolves
    store.put_pending("state-b", "n", "v", time.time() - 1)
    assert store.pop_pending("state-b") is None


def test_sqlite_store_is_shared_across_instances(tmp_path):
    """A login/session created by one worker must be visible to another. Two
    SqliteSessionStore instances over the same file model two workers."""

    path = tmp_path / "shared.sqlite3"
    worker_a = SqliteSessionStore(path)
    worker_b = SqliteSessionStore(path)
    try:
        # session created on A is found on B
        worker_a.put_session("sid", a_session(time.time() + 100))
        assert worker_b.get_session("sid") is not None
        # login begun on A completes on B
        worker_a.put_pending("state", "nonce", "verifier", time.time() + 100)
        popped = worker_b.pop_pending("state")
        assert popped is not None and popped[:2] == ("nonce", "verifier")
        # and having been consumed on B, it is gone on A too (single-use is global)
        assert worker_a.pop_pending("state") is None
    finally:
        worker_a.close()
        worker_b.close()


def test_sqlite_session_survives_reopen(tmp_path):
    """Restart durability: a new store over the same file still sees the session."""

    path = tmp_path / "persist.sqlite3"
    first = SqliteSessionStore(path)
    first.put_session("sid", a_session(time.time() + 100))
    first.close()
    reopened = SqliteSessionStore(path)
    try:
        assert reopened.get_session("sid") is not None
    finally:
        reopened.close()
