#!/bin/sh
# Cerberus CI: lint + tests. Every session ends green.
set -eu
cd "$(dirname "$0")/.."
uv run --frozen ruff check .
uv run --frozen python -m pytest -q
