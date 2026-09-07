import os

import pytest

from thaqip_ingestion.circuit_breaker import (
    CircuitBreakerOpen,
    CircuitState,
    GlobalKillSwitchActive,
    RouteCircuitBreaker,
)


@pytest.mark.asyncio
async def test_circuit_breaker_flow():
    cb = RouteCircuitBreaker("test_route", failure_threshold=2, recovery_timeout=0.1)

    assert cb.state == CircuitState.CLOSED
    await cb.before_request()

    # Record 1 failure
    await cb.record_failure()
    assert cb.state == CircuitState.CLOSED

    # Record 2nd failure -> should trip to OPEN
    await cb.record_failure()
    assert cb.state == CircuitState.OPEN

    # Next call raises CircuitBreakerOpen
    with pytest.raises(CircuitBreakerOpen):
        await cb.before_request()

    # Wait for recovery timeout
    import asyncio
    await asyncio.sleep(0.15)

    # Should transition to HALF_OPEN
    await cb.before_request()
    assert cb.state == CircuitState.HALF_OPEN

    # On success -> back to CLOSED
    await cb.record_success()
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_global_kill_switch():
    cb = RouteCircuitBreaker("test_route")
    os.environ["THAQIP_KILL_SWITCH"] = "1"
    try:
        with pytest.raises(GlobalKillSwitchActive):
            await cb.before_request()
    finally:
        del os.environ["THAQIP_KILL_SWITCH"]
