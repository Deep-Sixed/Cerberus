"""What a caller may put in a chat-completions request that Cerberus forwards.

A leaf module with no Cerberus imports: both the dispatch egress and the fusion
backend read it, and those two packages import each other's neighbours.
"""

from __future__ import annotations

from typing import Any

# Standard OpenAI chat-completions fields a caller may set. Anything else is
# dropped before the upstream call: provider extensions such as OpenRouter's
# `models` fallback list, `provider`, `route` and `plugins` can select or add
# models outside the alias's registry and cost policy, so forwarding them
# verbatim would let a caller on a free-only alias spend paid quota.
CALLER_FIELDS = frozenset(
    {
        "messages",
        "model",
        "stream",
        "stream_options",
        "temperature",
        "top_p",
        "n",
        "stop",
        "max_tokens",
        "max_completion_tokens",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "seed",
        "user",
        "response_format",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "functions",
        "function_call",
        "reasoning_effort",
        "chat_template_kwargs",
    }
)


def caller_body(body: dict[str, Any]) -> dict[str, Any]:
    """The caller's request restricted to CALLER_FIELDS."""

    return {key: value for key, value in body.items() if key in CALLER_FIELDS}


def non_function_tools(body: dict[str, Any]) -> list[str]:
    """Tool types other than ``function`` named by ``tools`` or ``tool_choice``.

    Server-side tools (``openrouter:fusion``, web search and the like) run on
    the provider under Cerberus's credential and can bill models the alias
    never listed, so they are refused rather than forwarded.
    """

    found: list[str] = []
    tools = body.get("tools")
    entries = tools if isinstance(tools, list) else ([] if tools is None else [tools])
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        entries = [*entries, choice]
    for entry in entries:
        kind = entry.get("type") if isinstance(entry, dict) else None
        if kind != "function":
            found.append(str(kind))
    return found
