"""Circuit breaker & anti-bot resilience layer (ticket B5).

Provides per-route circuit breakers, a global kill-switch, and state inspection
to prevent cascading failures or hammering protected endpoints when bot challenges
or sustained rate limits are triggered.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from enum import Enum

log = logging.getLogger("thaqip.resilience")


class CircuitState(str, Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing / tripped: reject requests immediately
    HALF_OPEN = "half_open"  # Probing for recovery


class CircuitBreakerOpen(Exception):
    """Raised when an operation is attempted while its circuit breaker is open."""

    def __init__(self, route: str, reset_in: float) -> None:
        super().__init__(f"Circuit breaker for route '{route}' is OPEN. Retry in {reset_in:.1f}s")
        self.route = route
        self.reset_in = reset_in


class GlobalKillSwitchActive(Exception):
    """Raised when ingestion or scraping is stopped via the global kill switch."""


class RouteCircuitBreaker:
    """Per-route failure counter and timeout circuit breaker."""

    def __init__(
        self,
        route: str,
        *,
        failure_threshold: int = 3,
        recovery_timeout: float = 300.0,
    ) -> None:
        self.route = route
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.opened_at: datetime | None = None
        self._lock = asyncio.Lock()

    def is_kill_switch_active(self) -> bool:
        return os.environ.get("THAQIP_KILL_SWITCH", "0").lower() in ("1", "true", "yes", "on")

    async def before_request(self) -> None:
        """Check whether execution is allowed before firing a request."""
        if self.is_kill_switch_active():
            log.critical("global kill switch THAQIP_KILL_SWITCH is active — halting execution on %s", self.route)
            raise GlobalKillSwitchActive("THAQIP_KILL_SWITCH is set")

        async with self._lock:
            if self.state == CircuitState.OPEN:
                elapsed = (datetime.now(UTC) - self.opened_at).total_seconds() if self.opened_at else 0
                if elapsed >= self.recovery_timeout:
                    log.info("circuit breaker for %s transitioning from OPEN to HALF_OPEN", self.route)
                    self.state = CircuitState.HALF_OPEN
                else:
                    raise CircuitBreakerOpen(self.route, self.recovery_timeout - elapsed)

    async def record_success(self) -> None:
        async with self._lock:
            if self.state != CircuitState.CLOSED:
                log.info("circuit breaker for %s recovered and is now CLOSED", self.route)
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            self.opened_at = None

    async def record_failure(self, exc: Exception | None = None) -> None:
        async with self._lock:
            self.failure_count += 1
            log.warning("route %s recorded failure %d/%d (%s)", self.route, self.failure_count, self.failure_threshold, exc)
            if self.failure_count >= self.failure_threshold or self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                self.opened_at = datetime.now(UTC)
                log.error(
                    "circuit breaker for %s tripped to OPEN! Cool-off period: %.0fs",
                    self.route,
                    self.recovery_timeout,
                )
