"""The verification policy must describe this repository, not a plausible one.

`verification.toml` tells the Verification Ladder what Cerberus requires and
which workflow step establishes each gate a checkout cannot run. Every claim in
it is checkable against the repository it describes, and an unchecked one fails
quietly: a renamed CI step turns its gate MISSING, which reads as "nobody ran
it" rather than as a broken map. These are the checks that keep the policy
honest without the Ladder being installed.
"""

import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
POLICY = tomllib.loads((ROOT / "verification.toml").read_text())
WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
# yaml parses the bare key `on:` as the boolean True; the job we validate is the
# only one, so find it by shape rather than by that key.
JOB = next(iter(WORKFLOW["jobs"].values()))


def reported_step_names():
    """Name each workflow step the way the Actions API reports it.

    An explicitly named step reports its name; an unnamed `run:` step reports
    "Run <command>". A multi-line unnamed run has no single-line form, so it
    cannot be mapped to a gate and must be given a name instead.
    """
    names = set()
    for step in JOB["steps"]:
        if "name" in step:
            names.add(step["name"])
        elif "run" in step and "\n" not in step["run"].strip():
            names.add(f"Run {step['run'].strip()}")
        elif "uses" in step:
            names.add(f"Run {step['uses']}")
    return names


@pytest.mark.parametrize("gate", sorted(POLICY["ci_steps"]))
def test_every_mapped_gate_names_a_real_workflow_step(gate):
    step = POLICY["ci_steps"][gate]
    assert step in reported_step_names(), (
        f"{gate} maps to {step!r}, which no step in ci.yml reports. A gate whose step "
        f"was renamed reads MISSING, not FAIL, so nothing else would catch this."
    )


@pytest.mark.parametrize("gate", sorted(POLICY["required_gates"]))
def test_every_required_gate_can_be_established_by_someone(gate):
    local = POLICY["gates"]["local"]
    assert gate in local or gate in POLICY["ci_steps"], (
        f"{gate} is required but no authority can establish it: it is neither in "
        f"[gates.local] nor mapped to a CI step, so it can only ever be MISSING."
    )


def test_local_gates_run_files_that_exist():
    """A gate command naming a path that has moved fails as a broken gate rather
    than as a failing check, which is a slower and more confusing signal."""
    for gate, command in POLICY["gates"]["local"].items():
        for token in command.split():
            if "/" in token and not token.startswith("-"):
                assert (ROOT / token).exists(), f"{gate} runs {token!r}, which does not exist"


def test_ci_only_gates_are_the_ones_a_checkout_cannot_run():
    """The split is a claim about this environment, so state it explicitly: these
    need Docker or a checksum-pinned Gitleaks and are BLOCKED until CI runs them."""
    local = set(POLICY["gates"]["local"])
    ci_only = set(POLICY["required_gates"]) - local
    assert ci_only == {"build", "compose", "container-build", "container-smoke", "gitleaks", "history"}


def test_judgment_rungs_are_never_also_gates():
    """A rung an agent attests to must not be satisfiable by a command, or the
    distinction between execution and attestation stops meaning anything."""
    judgments = set(POLICY["judgment_rungs"])
    assert not judgments & set(POLICY["required_gates"])
    assert not judgments & set(POLICY["gates"]["local"])
    assert not judgments & set(POLICY["ci_steps"])


def test_policy_keys_carry_no_dots():
    """A bare TOML key containing a dot is a table path: `tests-3.11 = "x"` parses
    as table `tests-3` holding `11`, and the gate silently disappears."""
    for table in ("ci_steps", "gates"):
        for name in POLICY[table]:
            assert "." not in name
    for gate in POLICY["required_gates"] + POLICY["judgment_rungs"]:
        assert "." not in gate


def test_pull_request_runs_are_not_accepted_as_head_evidence():
    """A pull_request run checks out a merge of head into base; binding its
    results to the branch tip would be true only while the two happen to match."""
    assert POLICY["ci_head_events"] == ["push"]
