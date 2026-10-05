"""What a jev decision may read: the policy-scoped decision state.

The decision service is a third party even when the model it picks runs on a
local host, so everything it receives is built here and nowhere else, under the
alias's ``jev.input`` policy:

    last_user_message   the latest user turn, truncated
    full_conversation   every turn, keeping the most recent text when truncated
    metadata            request shape only; no prompt text at all

Every mode also states request shape (turn count, size, tools, response format,
requested output length) and describes each pool model by registry facts —
model id, strength, cost tier, context window, capabilities, description. A
model is offered under an opaque id; provider and credential names never leave.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cerberus.registry.schema import CerberusConfig, JevInput
from cerberus.router.engine import Target

_TRUNCATED = " …[truncated]"


@dataclass(frozen=True, slots=True)
class Option:
    id: str  # opaque, what the decision service sees and answers with
    provider: str
    model: str

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"


def options_for(targets: list[Target]) -> list[Option]:
    """One option per distinct provider/model, in configured order.

    Two credentials for one model are the same choice to a decision; which
    credential serves it stays the failover loop's business.
    """

    seen: dict[tuple[str, str], Option] = {}
    for target in targets:
        key = (target.provider_id, target.model)
        if key not in seen:
            seen[key] = Option(id=f"m{len(seen) + 1}", provider=target.provider_id, model=target.model)
    return list(seen.values())


def decision_state(
    body: dict[str, Any],
    options: list[Option],
    config: CerberusConfig,
    *,
    input_mode: JevInput,
    max_chars: int,
) -> str:
    messages = [m for m in body.get("messages") or [] if isinstance(m, dict)]
    sections = [_request_shape(body, messages)]
    if input_mode == "last_user_message":
        last = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        text = _truncate_head(_text(last.get("content")), max_chars) if last is not None else ""
        sections.append(f"Latest user message:\n<<<\n{text}\n>>>")
    elif input_mode == "full_conversation":
        transcript = "\n\n".join(f"{m.get('role', 'unknown')}: {_text(m.get('content'))}" for m in messages)
        sections.append(f"Conversation:\n<<<\n{_truncate_tail(transcript, max_chars)}\n>>>")
    sections.append("Candidate models:\n" + "\n".join(_describe(option, config) for option in options))
    return "\n\n".join(sections)


def _request_shape(body: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    size = sum(len(_text(m.get("content"))) for m in messages)
    tools = body.get("tools")
    response_format = body.get("response_format")
    max_out = body.get("max_completion_tokens", body.get("max_tokens"))
    lines = [
        "Request:",
        f"- turns: {len(messages)} (user turns: {sum(1 for m in messages if m.get('role') == 'user')})",
        f"- approximate prompt size: {size} characters",
        f"- tools offered: {len(tools) if isinstance(tools, list) else 0}",
        f"- response format: {response_format.get('type', 'unspecified') if isinstance(response_format, dict) else 'text'}",
    ]
    if isinstance(max_out, int) and not isinstance(max_out, bool):
        lines.append(f"- requested maximum output: {max_out} tokens")
    return "\n".join(lines)


def _describe(option: Option, config: CerberusConfig) -> str:
    entry = config.providers[option.provider].models[option.model]
    facts = [
        f"model {option.model}",
        f"strength {entry.strength}",
        f"cost {entry.cost_tier}",
        f"context window {entry.context_window if entry.context_window is not None else 'unknown'}",
        f"capabilities {', '.join(entry.capabilities) if entry.capabilities else 'unspecified'}",
    ]
    line = f"- {option.id}: " + "; ".join(facts)
    return line + (f". {entry.description}" if entry.description else "")


def _text(content: Any) -> str:
    """Message text; non-text parts are named, never inlined."""

    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            parts.append(part["text"])
        elif isinstance(part, dict):
            parts.append(f"[{part.get('type', 'part')}]")
    return "\n".join(parts)


def _truncate_head(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + _TRUNCATED


def _truncate_tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else "[earlier turns truncated] …" + text[-limit:]
