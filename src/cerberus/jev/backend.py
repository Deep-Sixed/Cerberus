"""Decision-service seam plus the OpenRouter Decisions API implementation.

A jev alias asks one bounded question — which pool model should serve this
request — and gets back one of the option ids it offered, or nothing usable.
Nothing outside this module knows the wire shape of a decision service.

Confirmed contract for OpenRouter's Decisions API: ``POST /api/alpha/decisions``
with a bearer key; the body carries ``model``, ``state`` and ``questions``;
Jev answers ``choice``, ``noul`` and ``score`` questions; the response carries
``answers`` keyed by question id, with typed results and probabilities, and
``usage``. The field names inside one question and one answer are not yet
confirmed, so the request uses the plainest form and the answer is read
tolerantly. A misread can only ever produce "no usable answer", because the
choice must equal an option id Cerberus sent; the request then runs in
configured order, which every pool member already satisfies.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

DecisionFailure = Literal["timeout", "unreachable", "http_error", "invalid_response"]

# the one question a jev alias asks, by id
ROUTE_QUESTION_ID = "route"
ROUTE_QUESTION = (
    "Which candidate model is the least costly one that is still strong enough "
    "to handle this request well?"
)
# keys an answer's chosen option may sit under, most specific first
_CHOICE_KEYS = ("choice", "result", "value", "answer", "selected")
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "cost")


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    endpoint: str
    api_key: str
    model: str
    state: str  # everything the decision service may read, already scoped by policy
    options: list[str]  # opaque option ids; never a provider name
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class Decision:
    choice: str | None  # an option id exactly as answered, or None when absent
    confidence: float | None
    usage: dict[str, float] | None
    http_status: int
    latency_ms: float


class DecisionError(Exception):
    def __init__(self, reason: DecisionFailure, *, latency_ms: float, http_status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.latency_ms = latency_ms
        self.http_status = http_status


class DecisionBackend(Protocol):
    name: str

    async def decide(self, request: DecisionRequest) -> Decision: ...

    async def aclose(self) -> None: ...


class OpenRouterJevDecider:
    """Ask Jev through OpenRouter's Decisions API. Never chat completions."""

    name = "openrouter"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(transport=transport)

    @staticmethod
    def build_body(request: DecisionRequest) -> dict[str, Any]:
        return {
            "model": request.model,
            "state": request.state,
            "questions": [
                {
                    "id": ROUTE_QUESTION_ID,
                    "type": "choice",
                    "question": ROUTE_QUESTION,
                    "options": list(request.options),
                }
            ],
        }

    async def decide(self, request: DecisionRequest) -> Decision:
        started = time.perf_counter()

        def elapsed() -> float:
            return (time.perf_counter() - started) * 1000

        try:
            async with asyncio.timeout(request.timeout_seconds):
                response = await self._client.post(
                    request.endpoint,
                    json=self.build_body(request),
                    headers={"authorization": f"Bearer {request.api_key}", "content-type": "application/json"},
                    timeout=request.timeout_seconds,
                )
        except TimeoutError:
            raise DecisionError("timeout", latency_ms=elapsed()) from None
        except httpx.TimeoutException:
            raise DecisionError("timeout", latency_ms=elapsed()) from None
        except httpx.HTTPError:
            raise DecisionError("unreachable", latency_ms=elapsed()) from None

        if response.status_code >= 400:
            raise DecisionError("http_error", latency_ms=elapsed(), http_status=response.status_code)
        try:
            payload = response.json()
        except ValueError:
            raise DecisionError("invalid_response", latency_ms=elapsed(), http_status=response.status_code) from None
        if not isinstance(payload, dict):
            raise DecisionError("invalid_response", latency_ms=elapsed(), http_status=response.status_code)

        choice, confidence = _route_answer(payload.get("answers"))
        return Decision(
            choice=choice,
            confidence=confidence,
            usage=_usage(payload.get("usage")),
            http_status=response.status_code,
            latency_ms=elapsed(),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _route_answer(answers: Any) -> tuple[str | None, float | None]:
    """The chosen option and its probability from the route answer, if present."""

    if isinstance(answers, list):
        answer = next((a for a in answers if isinstance(a, dict) and a.get("id") == ROUTE_QUESTION_ID), None)
    elif isinstance(answers, dict):
        answer = answers.get(ROUTE_QUESTION_ID)
    else:
        return None, None
    if isinstance(answer, str):
        return answer, None
    if not isinstance(answer, dict):
        return None, None

    probabilities = answer.get("probabilities")
    probabilities = (
        {k: float(v) for k, v in probabilities.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        if isinstance(probabilities, dict)
        else {}
    )
    choice = next((answer[key] for key in _CHOICE_KEYS if isinstance(answer.get(key), str)), None)
    if choice is None and probabilities:
        choice = max(probabilities, key=probabilities.__getitem__)
    confidence = probabilities.get(choice) if choice is not None else None
    if confidence is None:
        stated = answer.get("confidence", answer.get("probability"))
        if isinstance(stated, (int, float)) and not isinstance(stated, bool):
            confidence = float(stated)
    return choice, confidence


def _usage(usage: Any) -> dict[str, float] | None:
    if not isinstance(usage, dict):
        return None
    kept = {
        key: value
        for key in _USAGE_KEYS
        if isinstance((value := usage.get(key)), (int, float)) and not isinstance(value, bool) and value >= 0
    }
    return kept or None
