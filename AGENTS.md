# Agent instructions

Verification for this repository follows the **Verification Ladder**:

  https://github.com/Deep-Sixed/verification-ladder

The invariant that verification is mandatory lives in the operator's global
agent instructions, not here, so this file does not restate it. What lives here
is the part that is specific to Cerberus: `verification.toml` declares the gates
this project requires, the commands that run them in a checkout, and the CI step
that establishes each gate a checkout cannot run.

If the Ladder is not installed for your agent, install it (see that repository's
`INSTALL.md`). Do not vendor a copy into this repository, do not improvise a
verification procedure in its place, and do not treat its absence as a pass.

## Non-negotiable

- Do not claim PASS, "tests pass", "the build works" or "complete" without
  evidence you hold for the current state. Absent evidence the verdict is
  BLOCKED, and the report says so.
- Do not skip, delete, `xfail` or narrow a test, and do not widen a lint ignore,
  to reach green.
- Secrets, operator hostnames, personal identity and private deployment paths
  never enter a blob or a commit message; see `scripts/scan-secrets.sh` and
  `scripts/check-public-history.py`, which enforce this over all ancestry.

## Toolchain

Python 3.14.5, uv 0.11.29, the committed `uv.lock`, Node 26.3.1.

```sh
uv sync --frozen --python 3.14.5
./scripts/ci.sh                    # lint + the full suite
```

See [docs/verification.md](docs/verification.md) for how the layers fit together.
