"""Editable-field schema and staging for the browser admin console.

Deliberately narrow: only operational tuning knobs (provider cooldown windows,
health-probe mode) are editable from a browser. Secrets, identities,
authorization, server/SSO bindings, and alias/routing structure are excluded
by construction — EDITABLE_FIELDS is an allow-list, not a denylist, so a new
config field is read-only here by default until explicitly added.

Secrets are shown (locked) but never accepted as an update: Cerberus runs in
a container with no path to the KeePassXC vault (no vault mount, no
secret-tool/D-Bus access, no keepassxc-cli in the image, and the host secret
files are bind-mounted read-only) — there is no host-vault bridge for a
container process to write through. Rotating a key stays a vault-workflow
operation outside this endpoint, not something this form can silently fake.
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

from cerberus.registry.schema import CerberusConfig

_VERSION_RE = re.compile(r"^(cerberus-\d{4}-\d{2}-\d{2})\.(\d+)$")

# (dotted-path-template, type, label-template, description) — {name} is the
# provider name, filled in per provider at schema-build time.
_PROVIDER_FIELD_SPECS: tuple[tuple[str, str, str, str], ...] = (
    (
        "providers.{name}.quota_cooldown_seconds",
        "number",
        "{name}: quota cooldown (seconds)",
        "How long a 429 quota exhaustion cools this provider down before it's tried again.",
    ),
    (
        "providers.{name}.transport_cooldown_seconds",
        "number",
        "{name}: transport cooldown (seconds)",
        "How long a connection/timeout failure cools this provider down.",
    ),
    (
        "providers.{name}.health_probe",
        "select",
        "{name}: health probe method",
        "models GETs /models; chat sends a 1-token completion, for providers that reject GET /models.",
    ),
)

_SECTION = {"id": "providers", "label": "Providers", "description": "Cooldown tuning and health-probe mode."}


def _get_path(obj: dict[str, Any], dotted: str) -> Any:
    node: Any = obj
    for part in dotted.split("."):
        node = node[part]
    return node


def _set_path(obj: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = obj
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value


def editable_keys(config: CerberusConfig) -> set[str]:
    """The allow-listed dotted paths that /admin/config/stage will accept."""

    keys: set[str] = set()
    for name in config.providers:
        for template, *_ in _PROVIDER_FIELD_SPECS:
            keys.add(template.format(name=name))
    return keys


def build_schema(config: CerberusConfig) -> dict[str, Any]:
    """The field/section manifest the console renders — current values, editable
    fields writable, everything else present but locked (visible, not editable)."""

    dumped = config.model_dump(mode="json")
    fields: list[dict[str, Any]] = []
    for name in sorted(config.providers):
        for template, ftype, label_t, description in _PROVIDER_FIELD_SPECS:
            key = template.format(name=name)
            value = _get_path(dumped, key)
            field: dict[str, Any] = {
                "key": key,
                "section": "providers",
                "type": ftype,
                "label": label_t.format(name=name),
                "description": description,
                "value": value,
                "locked": False,
            }
            if ftype == "select":
                field["options"] = ["models", "chat"]
            fields.append(field)
        # credentials are shown, never editable here — no vault bridge from the container
        for cred_name, cred in config.providers[name].credentials.items():
            fields.append({
                "key": f"providers.{name}.credentials.{cred_name}.api_key_env",
                "section": "providers",
                "type": "text",
                "label": f"{name}: {cred_name} key (env name)",
                "description": "Rotate the value via the vault workflow, not this form.",
                "value": cred.api_key_env,
                "locked": True,
            })
    return {"sections": [_SECTION], "fields": fields, "version": config.metadata.version}


def _next_version(current: str) -> str:
    m = _VERSION_RE.match(current)
    if m is None:
        raise ValueError(f"unrecognized version format: {current!r}")
    return f"{m.group(1)}.{int(m.group(2)) + 1}"


def apply_updates(config: CerberusConfig, updates: dict[str, Any]) -> CerberusConfig:
    """Apply an allow-listed set of edits on top of the active config and
    return a new, re-validated CerberusConfig with its version auto-bumped —
    never the same version string bound to two different byte-for-byte
    documents (the exact drift this session found and flagged earlier)."""

    allowed = editable_keys(config)
    rejected = set(updates) - allowed
    if rejected:
        raise ValueError(f"not editable from this console: {sorted(rejected)}")

    dumped = config.model_dump(mode="json")
    key: str | None = None
    try:
        for key, value in updates.items():
            current_type = type(_get_path(dumped, key))
            if current_type is bool:
                coerced: Any = bool(value)
            elif current_type in (int, float):
                coerced = current_type(value)
            else:
                coerced = value
            _set_path(dumped, key, coerced)
    except (KeyError, TypeError) as exc:
        # allow-listed keys are built from real provider names (editable_keys),
        # so this only fires if a provider name itself contains "." — an
        # unconstrained-but-possible config shape, not attacker input; fail
        # clean rather than let the traversal crash the request
        raise ValueError(f"cannot resolve editable field {key!r}: {exc}") from exc
    if updates:
        dumped["metadata"]["version"] = _next_version(config.metadata.version)
    return CerberusConfig.model_validate(dumped)


_STAGING_TTL_SECONDS = 900  # bounds disk growth from repeated Validate clicks
                             # that never Apply — a fresh call's own file is
                             # never this old, so no race with an in-flight
                             # validate/activate on it


def _sweep_stale(directory: Path) -> None:
    cutoff = time.time() - _STAGING_TTL_SECONDS
    for candidate in directory.glob("*.yaml"):
        try:
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink()
        except OSError:
            pass  # already removed by a concurrent sweep — fine


def stage(config: CerberusConfig, staging_dir: str | Path) -> str:
    """Write a candidate config to a fresh file under the writable staging
    directory and return its path, for the caller to hand to the existing
    /admin/validate + /admin/activate (unchanged, already-reviewed) endpoints."""

    directory = Path(staging_dir)
    directory.mkdir(parents=True, exist_ok=True)
    _sweep_stale(directory)
    path = directory / f"{uuid.uuid4().hex}.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
    return str(path)
