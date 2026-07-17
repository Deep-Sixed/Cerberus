"""Best-effort, redacted routing telemetry output."""

import asyncio
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

SCHEMA_VERSION = 1
AttemptOutcome = Literal[
    "missing_credentials",
    "transport_error",
    "retryable_status",
    "response",
    "invalid_response",
    "stream_interrupted",
]
RoutingOutcome = Literal["success", "upstream_error", "routing_exhausted", "stream_interrupted", "unauthorized"]


@dataclass(slots=True)
class RoutingAttempt:
    provider: str
    pool: str
    model: str
    used_fallback: bool
    outcome: AttemptOutcome
    latency_ms: float
    http_status: int | None = None


@dataclass(frozen=True, slots=True)
class RoutingEvent:
    request_id: str
    request_type: str
    provider: str | None
    pool: str | None
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
    schema_version: int = SCHEMA_VERSION

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "request_type": self.request_type,
            "identity": self.identity,
            "provider": self.provider,
            "pool": self.pool,
            "model": self.model,
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
    ) -> None:
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
        self._dropped_events = 0
        self._last_drop_warning_at: float | None = None
        self._last_failure_warning_at: float | None = None

    @property
    def dropped_events(self) -> int:
        """Number of events dropped because the bounded local queue was full."""

        return self._dropped_events

    async def start(self) -> None:
        """Start the single bounded delivery worker inside the application lifespan."""

        if self._client is None or self._worker is not None:
            return
        self._queue = asyncio.Queue(maxsize=self._queue_capacity)
        self._worker = asyncio.create_task(self._drain(), name="cerberus-telemetry")

    def emit(self, event: RoutingEvent) -> None:
        if self._client is None or self._queue is None:
            return
        try:
            self._queue.put_nowait(event.as_payload())
        except asyncio.QueueFull:
            self._record_drop()

    async def _drain(self) -> None:
        assert self._queue is not None
        while True:
            payload = await self._queue.get()
            try:
                await self._post(payload)
            except Exception:  # pragma: no cover - defensive worker boundary
                self._warn_delivery_failure()
            finally:
                self._queue.task_done()

    async def _post(self, payload: dict[str, Any]) -> None:
        try:
            assert self._client is not None
            assert self._endpoint is not None
            assert self._token_file is not None
            token = self._token_file.read_text(encoding="utf-8").strip()
            if not token:
                return
            response = await self._client.post(
                self._endpoint,
                headers={"authorization": f"Bearer {token}"},
                json=payload,
            )
            response.raise_for_status()
        except OSError, UnicodeError, httpx.HTTPError:
            self._warn_delivery_failure()
            return

    def _record_drop(self) -> None:
        self._dropped_events += 1
        now = time.monotonic()
        if self._last_drop_warning_at is None or now - self._last_drop_warning_at >= self._WARNING_INTERVAL_SECONDS:
            self._last_drop_warning_at = now
            logger.warning("Routing telemetry queue full; dropped_events=%d", self._dropped_events)

    def _warn_delivery_failure(self) -> None:
        now = time.monotonic()
        if (
            self._last_failure_warning_at is None
            or now - self._last_failure_warning_at >= self._WARNING_INTERVAL_SECONDS
        ):
            self._last_failure_warning_at = now
            logger.warning("Routing telemetry delivery failed; event data suppressed")

    async def close(self) -> None:
        if self._queue is not None and self._worker is not None:
            try:
                await asyncio.wait_for(
                    self._queue.join(),
                    timeout=self._timeout_seconds + 0.5,
                )
            except TimeoutError:
                self._record_drop()
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
            self._queue = None
            self._worker = None
        if self._client is not None:
            await self._client.aclose()
