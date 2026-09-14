"""Best-effort, redacted routing telemetry output."""

import asyncio
import copy
from collections import deque
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
import time
from typing import Any, Literal

import httpx

from ..registry.schema import TelemetryConfig

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 4
AttemptOutcome = Literal[
    "missing_credentials",
    "transport_error",
    "retryable_status",
    "response",
    "invalid_response",
    "stream_interrupted",
]
RoutingOutcome = Literal[
    "success",
    "upstream_error",
    "routing_exhausted",
    "stream_interrupted",
    "unauthorized",
    "shadow",
    "fusion_unavailable",  # fusion backend unreachable/unconfigured — fusion aliases fail closed
]


@dataclass(slots=True)
class RoutingAttempt:
    provider: str
    pool: str
    model: str
    used_fallback: bool
    outcome: AttemptOutcome
    latency_ms: float
    http_status: int | None = None
    # the exact scope this attempt's failure applied to the cooldown store
    # (e.g. "model" or "credential" on a 429); None when nothing was applied
    cooldown_scope: str | None = None


@dataclass(frozen=True, slots=True)
class RoutingEvent:
    request_id: str
    alias: str
    provider: str | None
    mode: str | None
    model: str | None
    used_fallback: bool
    attempts: list[RoutingAttempt]
    http_status: int
    outcome: RoutingOutcome
    latency_ms: float
    token_usage: dict[str, int] | None
    timestamp: datetime
    streaming: bool
    identity: str | None = None
    config_version: str | None = None
    credential: str | None = None
    cost_tier: str | None = None
    candidates: list[str] | None = None
    exclusions: list[dict[str, Any]] | None = None
    # Effective reasoning budget injected by the route, so later cost and
    # quality evidence is attributable to the setting actually used. None
    # when the route injects nothing (the provider default applied).
    reasoning_effort: str | None = None
    # Fusion-mode only: what Cerberus asked the deliberation backend for and what
    # it vouched for in return (backend name, requested panel/analyst, returned
    # model, backend generation id, router metadata). None outside fusion mode.
    fusion: dict[str, Any] | None = None
    schema_version: int = SCHEMA_VERSION

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "alias": self.alias,
            "reasoning_effort": self.reasoning_effort,
            "fusion": self.fusion,
            "identity": self.identity,
            "config_version": self.config_version,
            "provider": self.provider,
            "credential": self.credential,
            "mode": self.mode,
            "model": self.model,
            "cost_tier": self.cost_tier,
            "candidates": self.candidates,
            "exclusions": self.exclusions,
            "used_fallback": self.used_fallback,
            "attempt_count": len(self.attempts),
            "attempts": [asdict(attempt) for attempt in self.attempts],
            "http_status": self.http_status,
            "outcome": self.outcome,
            "latency_ms": round(self.latency_ms, 3),
            "token_usage": self.token_usage,
            "timestamp": self.timestamp.astimezone(timezone.utc).isoformat(),
            "streaming": self.streaming,
        }


class TelemetryEmitter:
    """Send routing events without putting telemetry on the inference critical path."""

    _WARNING_INTERVAL_SECONDS = 60.0

    def __init__(
        self,
        config: TelemetryConfig,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        release_id: str,
        recent_capacity: int = 50,
    ) -> None:
        if not release_id:
            raise ValueError("release_id must be a nonempty release identifier")
        self._release_id = release_id
        self._recent: deque[dict[str, Any]] = deque(maxlen=recent_capacity)
        self._endpoint = str(config.endpoint) if config.endpoint is not None else None
        self._token_file = Path(config.bearer_token_file) if config.bearer_token_file is not None else None
        self._timeout_seconds = config.timeout_seconds
        self._queue_capacity = config.queue_capacity
        self._client = (
            httpx.AsyncClient(transport=transport, timeout=config.timeout_seconds)
            if self._endpoint is not None
            else None
        )
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._in_flight = False
        self._dropped_events = 0
        self._last_drop_warning_at: float | None = None
        self._last_failure_warning_at: float | None = None
        self._consecutive_failures = 0
        self._last_error: str | None = None
        self._last_status_code: int | None = None
        self._last_error_at: str | None = None
        self._last_success_at: str | None = None

    @property
    def dropped_events(self) -> int:
        """Number of events dropped because the bounded local queue was full."""

        return self._dropped_events

    @property
    def release_id(self) -> str:
        return self._release_id

    async def start(self) -> None:
        """Start the single bounded delivery worker inside the application lifespan."""

        if self._client is None or self._worker is not None:
            return
        self._queue = asyncio.Queue(maxsize=self._queue_capacity)
        self._worker = asyncio.create_task(self._drain(), name="cerberus-telemetry")

    def recent_events_snapshot(self) -> list[dict[str, Any]]:
        """Newest-first deep-copied payloads for the read-only admin UI.

        A copy, never the internal deque or its dicts: callers can mutate the
        returned value without corrupting recorded events.
        """

        return copy.deepcopy(list(reversed(self._recent)))

    def health_snapshot(self) -> dict[str, Any]:
        """Return delivery health without exposing endpoint, credential, or event data."""

        if self._client is None:
            status = "disabled"
        elif self._consecutive_failures:
            status = "degraded"
        elif self._last_success_at is not None:
            status = "healthy"
        else:
            status = "pending"
        return {
            "status": status,
            "last_error": self._last_error,
            "last_status_code": self._last_status_code,
            "consecutive_failures": self._consecutive_failures,
            "last_error_at": self._last_error_at,
            "last_success_at": self._last_success_at,
            "dropped_events": self._dropped_events,
        }

    def emit(self, event: RoutingEvent) -> None:
        # snapshot at emit time: later mutation of attempt/exclusion objects by
        # the request path must not rewrite an already-recorded event
        payload = copy.deepcopy({**event.as_payload(), "release_id": self._release_id})
        self._recent.append(payload)
        if self._client is None or self._queue is None:
            return
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            self._record_drop()

    async def _drain(self) -> None:
        assert self._queue is not None
        while True:
            payload = await self._queue.get()
            self._in_flight = True
            try:
                await self._post(payload)
            except Exception:  # pragma: no cover - defensive worker boundary
                self._record_delivery_failure("internal_error")
            finally:
                self._in_flight = False
                self._queue.task_done()

    async def _post(self, payload: dict[str, Any]) -> None:
        try:
            assert self._client is not None
            assert self._endpoint is not None
            assert self._token_file is not None
            token = self._token_file.read_text(encoding="utf-8").strip()
            if not token:
                self._record_delivery_failure("missing_bearer")
                return
            response = await self._client.post(
                self._endpoint,
                headers={"authorization": f"Bearer {token}"},
                json=payload,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            self._record_delivery_failure(
                f"http_{status_code}",
                status_code=status_code,
                persistent=400 <= status_code < 500,
            )
            return
        except httpx.RequestError:
            self._record_delivery_failure("transport_error")
            return
        except (OSError, UnicodeError):
            self._record_delivery_failure("token_file_error")
            return
        except httpx.HTTPError:
            self._record_delivery_failure("http_error")
            return
        self._record_delivery_success()

    def _record_drop(self, count: int = 1) -> None:
        self._dropped_events += count
        now = time.monotonic()
        if self._last_drop_warning_at is None or now - self._last_drop_warning_at >= self._WARNING_INTERVAL_SECONDS:
            self._last_drop_warning_at = now
            logger.warning("Routing telemetry queue full; dropped_events=%d", self._dropped_events)

    def _record_delivery_success(self) -> None:
        self._consecutive_failures = 0
        self._last_error = None
        self._last_status_code = None
        self._last_error_at = None
        self._last_success_at = datetime.now(timezone.utc).isoformat()
        self._last_failure_warning_at = None

    def _record_delivery_failure(
        self,
        error: str,
        *,
        status_code: int | None = None,
        persistent: bool = False,
    ) -> None:
        self._consecutive_failures += 1
        self._last_error = error
        self._last_status_code = status_code
        self._last_error_at = datetime.now(timezone.utc).isoformat()
        now = time.monotonic()
        if (
            self._last_failure_warning_at is None
            or now - self._last_failure_warning_at >= self._WARNING_INTERVAL_SECONDS
        ):
            self._last_failure_warning_at = now
            log = logger.error if persistent else logger.warning
            log(
                "Routing telemetry delivery failed; error=%s status_code=%s "
                "consecutive_failures=%d event data suppressed",
                error,
                status_code,
                self._consecutive_failures,
            )

    async def close(self) -> None:
        if self._queue is not None and self._worker is not None:
            try:
                await asyncio.wait_for(
                    self._queue.join(),
                    timeout=self._timeout_seconds + 0.5,
                )
            except TimeoutError:
                # exact discard count: everything still queued, plus the event
                # mid-delivery whose worker is about to be cancelled. Events
                # delivered before the timeout are never counted.
                self._record_drop(self._queue.qsize() + (1 if self._in_flight else 0))
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
            self._queue = None
            self._worker = None
        if self._client is not None:
            await self._client.aclose()
