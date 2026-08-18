"""OpenAI-compatible upstream call helpers and streaming usage extraction.

The streaming collector and usage parsing are transplanted verbatim from the
donor (MetaRouter v3 app.py, commit 14cb770) — behavior-preserving by design;
see the S2 review gate in PLAN.md.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from cerberus.router.engine import Target

RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


def retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def token_usage(body: dict[str, Any]) -> dict[str, int] | None:
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    safe_usage = {
        key: value
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if isinstance((value := usage.get(key)), int) and not isinstance(value, bool) and value >= 0
    }
    return safe_usage or None


class StreamingUsageCollector:
    """Extract usage-only SSE fields without retaining response content."""

    _MAX_LINE_BYTES = 65_536

    def __init__(self) -> None:
        self._buffer = b""
        self.usage: dict[str, int] | None = None

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._read_line(line)
        if len(self._buffer) > self._MAX_LINE_BYTES:
            self._buffer = b""

    def finish(self) -> None:
        if self._buffer:
            self._read_line(self._buffer)
            self._buffer = b""

    def _read_line(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"data:"):
            return
        payload = line.removeprefix(b"data:").strip()
        if not payload or payload == b"[DONE]":
            return
        try:
            body = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if isinstance(body, dict) and (usage := token_usage(body)) is not None:
            self.usage = usage


def build_upstream_request(
    client: httpx.AsyncClient, target: Target, body: dict[str, Any], api_key: str
) -> httpx.Request:
    upstream_body = {**body, "model": target.model}
    # Optional per-candidate reasoning budget. Absent -> nothing is added and the
    # body is exactly what it was before this feature existed. Present -> injected
    # only when the caller did not state one, unless the route explicitly claims
    # precedence, so a caller's stated budget is never silently replaced.
    if target.reasoning_effort is not None:
        if "reasoning_effort" not in upstream_body or target.reasoning_effort_override:
            upstream_body["reasoning_effort"] = target.reasoning_effort
    headers = {"content-type": "application/json", "authorization": f"Bearer {api_key}"}
    return client.build_request("POST", target.base_url + "/chat/completions", headers=headers, json=upstream_body)
