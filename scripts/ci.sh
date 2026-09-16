#!/bin/sh
# Cerberus CI: lint + tests. Every session ends green.
set -eu
cd "$(dirname "$0")/.."
uv run --frozen ruff check .
# Record the interpreter the tests actually run on. pyproject pins
# python-preference = "only-managed" so CI and a local checkout resolve the same
# build; printing it keeps any future divergence visible in the log rather than
# surfacing as a test that passes in one place and fails in the other.
uv run --frozen python -c 'import sqlite3, sys; print(f"toolchain: python {sys.version.split()[0]} ({sys.executable}), sqlite {sqlite3.sqlite_version}")'
uv run --frozen python -m pytest -q
