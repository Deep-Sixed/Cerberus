#!/bin/sh
set -eu
# Supply a trusted local Gitleaks 8.30.1 executable on PATH.
gitleaks git --redact --ignore-gitleaks-allow --log-opts=HEAD .
gitleaks dir --redact --ignore-gitleaks-allow .
python scripts/check-public-history.py
