"""Decision-service seam plus the OpenRouter Decisions API implementation.

A jev alias asks bounded questions in one call — which pool model should serve
this request and, when its policy says so, how much reasoning effort the request
needs — and gets back, per question, one of the options it offered or nothing
usable. Nothing outside this module knows the wire shape of a decision service.

Contract for OpenRouter's Decisions API, from its published reference: ``POST
/api/alpha/decisions`` with a bearer key; the body carries ``model``, ``state``
and ``questions``, a record keyed by question id. A ``choice`` question is
``{type, instructions, criteria}``, where ``criteria`` maps each option to its
guidance or ``null``; the live validator rejects a list of questions and
accepts this form. The response carries ``answers`` keyed by question id — a
choice answer is ``{type, choice, confidence?, probabilities?}`` — and ``usage``
with ``input_tokens``, ``output_tokens`` and ``cost``. The answer is still read
tolerantly until a live call confirms it. A misread can only ever produce "no
usable answer", because each choice must equal an option Cerberus sent; the
request then runs in configured order and configured effort, which policy
already allows.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

DecisionFailure = Literal["timeout", "unreachable", "http_error", "invalid_response"]

# the questions a jev alias asks, by id
ROUTE_QUESTION_ID = "route"
ROUTE_QUESTION = (
    "Which candidate model is the least costly one that is still strong enough "
    "to handle this request well?"
)
EFFORT_QUESTION_ID = "effort"
EFFORT_QUESTION = "How much reasoning effort should the chosen model spend on this request?"
# keys an answer's chosen option may sit under, most specific first
_CHOICE_KEYS = ("choice", "result", "value", "answer", "selected")
_USAGE_KEYS = ("input_tokens", "output_tokens", "cost")


@dataclass(frozen=True, slots=True)
class Question:
    id: str
    question: str
    options: list[str]  # opaque model ids or effort levels; never a provider name


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    endpoint: str
    api_key: str
    model: str
    state: str  # everything the decision service may read, already scoped by policy
    questions: list[Question]
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class Answer:
    choice: str | None  # exactly as answered, or None when absent; not yet vetted
    confidence: float | None


@dataclass(frozen=True, slots=True)
class Decision:
    answers: dict[str, Answer]  # one per question asked, keyed by question id
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
        # an option needs no guidance of its own: the state already describes each one
        return {
            "model": request.model,
            "state": request.state,
            "questions": {
                q.id: {"type": "choice", "instructions": q.question, "criteria": dict.fromkeys(q.options)}
                for q in request.questions
            },
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

        answers = payload.get("answers")
        return Decision(
            answers={q.id: Answer(*_answer(answers, q.id)) for q in request.questions},
            usage=_usage(payload.get("usage")),
            http_status=response.status_code,
            latency_ms=elapsed(),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _answer(answers: Any, question_id: str) -> tuple[str | None, float | None]:
    """The chosen option and its probability for one question, if present."""

    if isinstance(answers, list):
        answer = next((a for a in answers if isinstance(a, dict) and a.get("id") == question_id), None)
    elif isinstance(answers, dict):
        answer = answers.get(question_id)
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
