"""Gatekeeping for admin-supplied candidate configurations.

Two questions, both answered before anything reaches the caller: where a
candidate configuration may live, and what may be said about why it failed.

``/admin/validate``, ``/admin/activate`` and ``/admin/shadow`` all accept a
filesystem path from the request body and hand it to ``load_config_document``.
Unconstrained, that is a file-read primitive for an admin-authenticated caller:
the loader's own exception text echoes file content back in the response — a
YAML scanner error quotes the offending source line, and a pydantic error
carries ``input_value``, which for a non-mapping document is the whole parsed
file. Both halves are needed. Confinement stops an arbitrary file being opened;
classification stops the contents of a permitted one being narrated verbatim.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from pathlib import Path

import yaml
from pydantic import ValidationError

logger = logging.getLogger(__name__)

# Stable machine-readable reasons. The console shows `message`; automation can
# branch on `reason` without parsing prose.
REASON_OUTSIDE_ALLOWED_PATH = "outside_allowed_path"
REASON_NOT_READABLE = "not_readable"
REASON_INVALID_YAML = "invalid_yaml"
REASON_SCHEMA_INVALID = "schema_invalid"
# Cerberus's own policy refusals: a version already bound to another checksum, a
# revision trying to move state.path, credentials named by the config but absent
# from the environment. Distinct from schema_invalid because the document parsed
# and validated fine — Cerberus declined it, and the operator needs to know that.
REASON_CONFIG_REJECTED = "config_rejected"


class CandidateRejected(Exception):
    """A candidate refused with a stable reason and a disclosure-safe message."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def allowed_roots(
    *, staging_dir: str | Path, boot_config_path: str | None, extra_roots: Iterable[str] = ()
) -> tuple[Path, ...]:
    """Resolved directories a candidate configuration may be read from.

    The staging directory (where /admin/config/stage writes) is always allowed,
    as is the directory holding the configuration this process booted from —
    git owns authoring, so an operator-authored revision lands beside the active
    one. Extra roots are explicit configuration, never discovered from the
    filesystem, and empty by default.

    An in-memory boot document has no directory to trust; its sentinel
    source_path would otherwise resolve to the process working directory.
    """

    roots: list[Path] = [Path(staging_dir).resolve()]
    if boot_config_path and not boot_config_path.startswith("<"):
        roots.append(Path(boot_config_path).resolve().parent)
    roots.extend(Path(root).resolve() for root in extra_roots)
    return tuple(dict.fromkeys(roots))


def resolve_candidate(raw_path: str, roots: Sequence[Path]) -> Path:
    """Canonicalize a caller-supplied path and require containment in a root.

    ``Path.resolve()`` collapses ``..`` and follows symlinks, so containment is
    decided on the real target rather than on the spelling of the request: a
    symlink under an allowed root that points outside it is rejected, and no
    string-level ``..`` filtering is involved. The check runs before the file is
    opened, so a path outside every root is never read at all.
    """

    candidate = Path(raw_path).resolve()
    for root in roots:
        if candidate == root or root in candidate.parents:
            return candidate
    logger.warning(
        "Rejected candidate configuration outside every allowed root; "
        "roots=%s (candidate path suppressed)",
        [str(root) for root in roots],
    )
    raise CandidateRejected(
        REASON_OUTSIDE_ALLOWED_PATH,
        "Candidate configuration must live in the staging directory, beside the "
        "active configuration, or under a configured additional root.",
    )


def _yaml_message(exc: yaml.YAMLError) -> str:
    """Describe a parse failure without quoting the source.

    ``str(exc)`` embeds a snippet of the offending line, which is the disclosure.
    ``problem`` is parser-generated prose and the marks are coordinates, so both
    are safe and are what an operator actually needs to find the mistake.
    """

    problem = getattr(exc, "problem", None)
    mark = getattr(exc, "problem_mark", None)
    detail = problem if isinstance(problem, str) and problem else "could not be parsed as YAML"
    if mark is not None and getattr(mark, "line", None) is not None:
        return f"Invalid YAML at line {mark.line + 1}, column {mark.column + 1}: {detail}."
    return f"Invalid YAML: {detail}."


def _schema_message(exc: ValidationError) -> str:
    """Summarize schema failures from location and message only.

    Never ``error['input']``: that is the rejected value straight out of the
    file, and for a document that is not a mapping it is the entire file.
    """

    parts: list[str] = []
    for error in exc.errors()[:5]:
        location = ".".join(str(item) for item in error.get("loc", ())) or "(document root)"
        parts.append(f"{location}: {error.get('msg', 'is invalid')}")
    total = len(exc.errors())
    if total > len(parts):
        parts.append(f"... and {total - len(parts)} more")
    return "Configuration does not match the Cerberus schema — " + "; ".join(parts)


def describe_failure(exc: Exception) -> tuple[str, str]:
    """Map a loader/lifecycle failure to (reason, disclosure-safe message).

    Full detail goes to the server log; only the classified pair is returned to
    the caller.
    """

    if isinstance(exc, CandidateRejected):
        return exc.reason, exc.message
    logger.warning("Candidate configuration rejected: %s", exc, exc_info=True)
    if isinstance(exc, ValidationError):
        return REASON_SCHEMA_INVALID, _schema_message(exc)
    if isinstance(exc, yaml.YAMLError):
        return REASON_INVALID_YAML, _yaml_message(exc)
    if isinstance(exc, OSError):
        # errno text can name the path; the caller supplied it, but there is no
        # reason to confirm what does or does not exist on the host.
        return REASON_NOT_READABLE, "Candidate configuration could not be read."
    # ValueError / RuntimeError from the loader and control plane. These messages
    # are written by Cerberus, describe its own policy, and quote configuration
    # identifiers rather than file content — exactly the actionable ones.
    return REASON_CONFIG_REJECTED, str(exc)
