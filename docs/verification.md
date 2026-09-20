# Verification

Cerberus is a consumer of the [Verification
Ladder](https://github.com/Deep-Sixed/verification-ladder), not its owner. The
procedure an agent follows — nine rungs from baseline through a fresh-reviewer
pass, with defined invalidation, re-entry, escalation and termination — lives in
that repository and is installed once per machine. This repository declares only
what verification must establish here.

```text
LEVEL 1  behavioral   the operator's global agent instructions
                      "verification is mandatory, per task, before completion"
LEVEL 2  procedural   the Verification Ladder skill, installed with the agent
                      "here is the exact procedure"
LEVEL 3  mechanical   verification.toml, ./scripts/ci.sh, .github/workflows/ci.yml
                      "the evidence exists, or it does not"
```

They fail differently, which is why they are separate. Instructions to a model
can be ignored; a procedure can be followed against the wrong target; only the
mechanical layer states facts about runs that happened.

## What this repository declares

`verification.toml`:

- **`required_gates`** — what must PASS by execution before a change here is
  complete.
- **`judgment_rungs`** — the rungs no machine can execute, which an agent
  attests to and which satisfy no required gate.
- **`[gates.local]`** — the commands a contributor runs: ruff, the suite, and
  the admin console's syntax check.
- **`[ci_steps]`** — the workflow step that establishes each gate a checkout
  cannot run: the wheel build, the container and its smoke test, the compose
  bundle, Gitleaks and the private-history check. Those need Docker and a
  checksum-pinned Gitleaks, so locally they are BLOCKED until CI runs them.

A gate BLOCKED here is not waived. It stays BLOCKED until an authority that can
run it does, and the Ladder's `import-ci` brings that result back bound to the
commit the run actually checked out.

## Why the split

The Ladder was developed against this repository and every property in it was
found by running it here — but none of it is about Cerberus. Keeping the
procedure in one place means Cerberus and any other project verify the same way
and cannot drift into two ladders that agree on the word "complete" and nothing
else.

What is genuinely Cerberus's stays here: that a change must not break the
container or the compose bundle, that no blob or commit message may carry
operator identity or private deployment material, that the toolchain is pinned
to one interpreter build. See [architecture](architecture.md),
[persistence](persistence.md) and [security](../SECURITY.md).
