"""Live probe: does OpenRouter's Decisions API accept what a jev alias sends?

The jev decision adapter (src/cerberus/jev/backend.py) was written against a
partly confirmed contract: the endpoint, the model and the model/state/questions
envelope are confirmed; the fields inside one question and one answer are not.
This sends one real decision through that adapter's own code path — the same
request body, headers, deadline and answer reader production uses — and reports
the raw response beside what Cerberus made of it.

    OPENROUTER_API_KEY=... uv run --frozen python scripts/probe-jev-decisions.py

The decision state is synthetic: three made-up pool models and a fixed prompt,
or one you pass with --prompt. Nothing from a configuration or a real request is
sent, and the key is never printed. One decision call is billed.

Exit status: 0 when the request is accepted and every question asked comes back
as one of the options offered; 1 when not; 2 when the probe cannot run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import sys
from dataclasses import dataclass, field
from typing import Any

import httpx

from cerberus.jev.backend import (
    EFFORT_QUESTION,
    EFFORT_QUESTION_ID,
    ROUTE_QUESTION,
    ROUTE_QUESTION_ID,
    DecisionError,
    DecisionRequest,
    OpenRouterJevDecider,
    Question,
)
from cerberus.jev.state import Option, decision_state
from cerberus.registry.schema import CerberusConfig, JevPolicy

DEFAULTS = JevPolicy.model_validate({"decider": {"provider": "probe", "credential": "probe"}})
DEFAULT_PROMPT = "Rename the variable `tmp` to `total` in this three-line Python function."
EFFORTS = ["low", "medium", "high"]

# A synthetic registry: the decision is described from it exactly as a real
# pool would be, but nothing in it belongs to a deployment.
_PROBE_CONFIG = CerberusConfig.model_validate(
    {
        "metadata": {"version": "cerberus-2026-10-05.1"},
        "providers": {
            "probe": {
                "base_url": "https://probe.invalid/v1",
                "credentials": {"probe": {"api_key_env": "PROBE_UNUSED"}},
                "models": {
                    "probe/small-local": {
                        "cost_tier": "free", "strength": "standard", "context_window": 32768,
                        "capabilities": ["chat", "coding"], "description": "Small local model; fast, free.",
                    },
                    "probe/mid": {
                        "cost_tier": "free", "strength": "strong", "context_window": 131072,
                        "capabilities": ["chat", "coding"],
                    },
                    "probe/frontier": {
                        "cost_tier": "paid", "strength": "frontier", "context_window": 200000,
                        "capabilities": ["chat", "coding", "tools"], "description": "Strongest; most expensive.",
                    },
                },
            }
        },
        "aliases": {
            "cerberus/probe": {
                "mode": "dispatch",
                "candidates": [{"provider": "probe", "credential": "probe", "model": "probe/small-local"}],
            }
        },
    }
)
_OPTIONS = [
    Option(id=f"m{i}", provider="probe", model=model)
    for i, model in enumerate(_PROBE_CONFIG.providers["probe"].models, start=1)
]


class _Recording(httpx.AsyncBaseTransport):
    """Passes the call through unchanged and keeps what crossed the wire."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.status: int | None = None
        self.body: bytes | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        content = await response.aread()
        self.status, self.body = response.status_code, content
        return httpx.Response(response.status_code, headers=response.headers, content=content, request=request)

    async def aclose(self) -> None:
        await self._inner.aclose()


@dataclass
class Report:
    request_body: dict[str, Any]
    status: int | None = None
    raw: Any = None  # the parsed response, or its text when not JSON
    latency_ms: float | None = None
    failure: str | None = None  # why the adapter raised, if it did
    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    usage: dict[str, float] | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None and all(a["offered"] for a in self.answers.values())


async def run_probe(
    *,
    api_key: str,
    endpoint: str = str(DEFAULTS.endpoint),
    model: str = DEFAULTS.model,
    prompt: str = DEFAULT_PROMPT,
    ask_effort: bool = True,
    timeout_seconds: float = 20.0,
    transport: httpx.AsyncBaseTransport,
) -> Report:
    questions = [Question(ROUTE_QUESTION_ID, ROUTE_QUESTION, [o.id for o in _OPTIONS])]
    if ask_effort:
        questions.append(Question(EFFORT_QUESTION_ID, EFFORT_QUESTION, list(EFFORTS)))
    request = DecisionRequest(
        endpoint=endpoint,
        api_key=api_key,
        model=model,
        state=decision_state(
            {"messages": [{"role": "user", "content": prompt}]},
            _OPTIONS,
            _PROBE_CONFIG,
            input_mode="last_user_message",
            max_chars=DEFAULTS.max_input_chars,
        ),
        questions=questions,
        timeout_seconds=timeout_seconds,
    )
    report = Report(request_body=OpenRouterJevDecider.build_body(request))
    recording = _Recording(transport)
    decider = OpenRouterJevDecider(recording)
    try:
        decision = await decider.decide(request)
    except DecisionError as exc:
        report.failure, report.latency_ms = exc.reason, exc.latency_ms
    else:
        report.latency_ms, report.usage = decision.latency_ms, decision.usage
        for question in questions:
            answer = decision.answers[question.id]
            report.answers[question.id] = {
                "choice": answer.choice,
                "confidence": answer.confidence,
                "offered": answer.choice in question.options,
            }
    finally:
        await decider.aclose()
    report.status = recording.status
    if recording.body is not None:
        try:
            report.raw = json.loads(recording.body)
        except ValueError:
            report.raw = recording.body.decode("utf-8", errors="replace")[:2000]
    return report


def render(report: Report, *, endpoint: str, model: str) -> str:
    lines = [f"POST {endpoint}", f"model: {model}", "", "request body (synthetic; no key):"]
    lines.append(json.dumps(report.request_body, indent=2))
    lines += ["", f"response: HTTP {report.status if report.status is not None else '—'}"
              + (f" in {report.latency_ms:.0f} ms" if report.latency_ms is not None else "")]
    if report.raw is not None:
        lines.append(json.dumps(report.raw, indent=2) if not isinstance(report.raw, str) else report.raw)
    lines += ["", "Cerberus reads:"]
    if report.failure is not None:
        lines.append(f"  adapter failed: {report.failure}")
    for question_id, answer in report.answers.items():
        mark = "ok" if answer["offered"] else "NOT AN OFFERED OPTION"
        lines.append(
            f"  {question_id}: choice={answer['choice']!r} confidence={answer['confidence']} ({mark})"
        )
    if report.usage is not None:
        lines.append(f"  usage: {report.usage}")
    lines += ["", "verdict: " + verdict(report)]
    return "\n".join(lines)


def verdict(report: Report) -> str:
    if report.ok:
        return "OK. The request is accepted and every answer reads as an offered option."
    if report.failure == "http_error":
        return (
            f"REQUEST REJECTED (HTTP {report.status}). Compare the error body with the request body; "
            "the question shape is built in OpenRouterJevDecider.build_body."
        )
    if report.failure in ("timeout", "unreachable"):
        return f"NO ANSWER ({report.failure}). Check the endpoint, network and key, then retry."
    if report.failure == "invalid_response":
        return "UNREADABLE RESPONSE. The body above is not a JSON object."
    unread = [qid for qid, a in report.answers.items() if a["choice"] is None]
    if unread:
        return (
            f"ANSWER NOT READ for {', '.join(unread)}. The request was accepted; find each answer in "
            "the response above and adjust _answer in src/cerberus/jev/backend.py to read it."
        )
    return "ANSWERED OUTSIDE THE OPTIONS. An answer was read but is not one Cerberus offered."


def _transport() -> httpx.AsyncBaseTransport:
    # what an AsyncClient would have taken from the environment by itself
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    cafile = os.environ.get("SSL_CERT_FILE")
    return httpx.AsyncHTTPTransport(proxy=proxy, verify=ssl.create_default_context(cafile=cafile) if cafile else True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--key-env", default="OPENROUTER_API_KEY", help="environment variable holding the key")
    parser.add_argument("--endpoint", default=str(DEFAULTS.endpoint))
    parser.add_argument("--model", default=DEFAULTS.model)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="the synthetic user message to decide on")
    parser.add_argument("--no-effort", action="store_true", help="ask only which model, not the effort")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)

    api_key = os.environ.get(args.key_env, "").strip()
    if not api_key:
        print(f"probe: set {args.key_env} to an OpenRouter API key", file=sys.stderr)
        return 2
    report = asyncio.run(
        run_probe(
            api_key=api_key,
            endpoint=args.endpoint,
            model=args.model,
            prompt=args.prompt,
            ask_effort=not args.no_effort,
            timeout_seconds=args.timeout,
            transport=_transport(),
        )
    )
    print(render(report, endpoint=args.endpoint, model=args.model))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
