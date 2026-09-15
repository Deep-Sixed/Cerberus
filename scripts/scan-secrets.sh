#!/bin/sh
set -eu
# Supply a trusted local Gitleaks 8.30.1 executable on PATH.
gitleaks git --redact --ignore-gitleaks-allow --log-opts='--all HEAD' .
# Scan exactly the tracked publication tree, excluding dependency environments.
cerberus_scan_dir=$(mktemp -d)
trap 'rm -rf -- "$cerberus_scan_dir"' EXIT HUP INT TERM
git archive HEAD | tar -x -C "$cerberus_scan_dir"
gitleaks dir --redact --ignore-gitleaks-allow --config .gitleaks.toml "$cerberus_scan_dir"
python scripts/check-public-history.py
