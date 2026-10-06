"""The live Decisions API probe, run offline against a mocked endpoint.

The probe exists to tell an operator whether a real Decisions API accepts what a
jev alias sends and whether Cerberus reads the answers back. These prove it
reports each outcome truthfully, sends nothing from a real configuration, and
never prints the key.
"""

import gzip
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "probe-jev-decisions.py"
_spec = importlib.util.spec_from_file_location("probe_jev_decisions", _PATH)
probe = importlib.util.module_from_spec(_spec)
# dataclasses resolve their module through sys.modules, so register before running
sys.modules[_spec.name] = probe
_spec.loader.exec_module(probe)

KEY = "sk-or-probe-secret"


def transport(status=200, payload=None, content=None, capture=None):
    def respond(request):
        if capture is not None:
            capture.append(request)
        if content is not None:
            return httpx.Response(status, content=content)
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(respond)


def answered(route="m1", effort="low"):
    return {
        "answers": {
            "route": {"type": "choice", "choice": route, "confidence": 0.7, "probabilities": {route: 0.84}},
            "effort": {"type": "choice", "choice": effort, "confidence": 0.5, "probabilities": {effort: 0.66}},
        },
        "usage": {"input_tokens": 210, "output_tokens": 2, "cost": 0.0000088},
    }


async def run(t, **kwargs):
    return await probe.run_probe(api_key=KEY, transport=t, **kwargs)


@pytest.mark.asyncio
async def test_the_probe_sends_the_production_request_shape():
    sent = []
    report = await run(transport(payload=answered(), capture=sent))

    assert len(sent) == 1
    request = sent[0]
    assert str(request.url) == "https://openrouter.ai/api/alpha/decisions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body == report.request_body
    assert body["model"] == "typesafe/jev-1.13"
    assert list(body["questions"]) == ["route", "effort"]
    assert body["questions"]["route"]["criteria"] == {"m1": None, "m2": None, "m3": None}
    assert body["questions"]["effort"]["criteria"] == {"low": None, "medium": None, "high": None}
    # synthetic state only: the probe's own made-up pool and prompt
    assert "probe/small-local" in body["state"] and probe.DEFAULT_PROMPT in body["state"]


@pytest.mark.asyncio
async def test_an_accepted_request_with_readable_answers_passes():
    report = await run(transport(payload=answered("m3", "high")))
    assert report.ok
    assert report.answers["route"] == {"choice": "m3", "confidence": 0.84, "offered": True}
    assert report.answers["effort"] == {"choice": "high", "confidence": 0.66, "offered": True}
    assert report.usage == {"input_tokens": 210, "output_tokens": 2, "cost": 0.0000088}
    assert probe.verdict(report).startswith("OK")


@pytest.mark.asyncio
async def test_no_effort_asks_only_the_route():
    sent = []
    report = await run(transport(payload=answered(), capture=sent), ask_effort=False)
    assert list(json.loads(sent[0].content)["questions"]) == ["route"]
    assert list(report.answers) == ["route"] and report.ok


@pytest.mark.parametrize(
    ("t", "start", "status"),
    [
        (transport(400, {"error": {"message": "questions: expected record, received array"}}), "REQUEST REJECTED (HTTP 400)", 400),
        (transport(403, {"error": {"message": "Key limit exceeded (total limit)."}}), "KEY REFUSED (HTTP 403)", 403),
        (transport(200, content=b"<html>"), "UNREADABLE RESPONSE", 200),
        (transport(200, {"answers": {"route": {"label": "m1"}}}), "ANSWER NOT READ for route, effort", 200),
        (transport(200, answered("anthropic/claude", "extreme")), "ANSWERED OUTSIDE THE OPTIONS", 200),
    ],
    ids=["rejected", "key-refused", "not-json", "unread", "outside"],
)
@pytest.mark.asyncio
async def test_each_failure_is_reported_as_what_it_is(t, start, status):
    report = await run(t)
    assert not report.ok
    assert report.status == status
    assert probe.verdict(report).startswith(start)


@pytest.mark.asyncio
async def test_a_compressed_response_is_reported_as_what_it_is():
    error = gzip.compress(json.dumps({"error": {"message": "Key limit exceeded (total limit)."}}).encode())
    report = await run(httpx.MockTransport(
        lambda _request: httpx.Response(403, content=error, headers={"content-encoding": "gzip"})
    ))
    assert (report.status, report.failure) == (403, "http_error")
    assert report.raw == {"error": {"message": "Key limit exceeded (total limit)."}}
    assert probe.verdict(report).startswith("KEY REFUSED (HTTP 403)")


@pytest.mark.asyncio
async def test_the_rendered_report_shows_the_raw_response_and_never_the_key():
    report = await run(transport(400, {"error": {"message": "bad question"}}))
    text = probe.render(report, endpoint="https://openrouter.ai/api/alpha/decisions", model="typesafe/jev-1.13")
    assert "bad question" in text and "HTTP 400" in text
    assert KEY not in text


def test_without_a_key_the_probe_refuses_to_run(monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert probe.main([]) == 2
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err
