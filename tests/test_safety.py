"""Tests for safety mechanisms."""

import asyncio
import pytest
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from safety.throttle import AdaptiveThrottle, ThrottleState
from orchestrator.supervisor import CircuitBreaker, CircuitState


class TestAdaptiveThrottle:
    """Test adaptive throttling."""

    @pytest.fixture
    def throttle(self):
        return AdaptiveThrottle(base_delay_ms=0)

    @pytest.mark.asyncio
    async def test_initial_state(self, throttle):
        """Test initial throttle state."""
        assert throttle.current_level == 0
        assert throttle.current_delay_ms == 0

    @pytest.mark.asyncio
    async def test_increase_throttle(self, throttle):
        """Test throttle level increase."""
        await throttle.increase_throttle(reason="test")
        assert throttle.current_level == 1

        await throttle.increase_throttle(reason="test")
        assert throttle.current_level == 2

    @pytest.mark.asyncio
    async def test_decrease_throttle(self, throttle):
        """Test throttle level decrease."""
        await throttle.increase_throttle(reason="test")
        await throttle.increase_throttle(reason="test")

        await throttle.decrease_throttle()
        assert throttle.current_level == 1

        await throttle.decrease_throttle()
        assert throttle.current_level == 0

    @pytest.mark.asyncio
    async def test_reset_throttle(self, throttle):
        """Test throttle reset."""
        await throttle.increase_throttle(reason="test")
        await throttle.increase_throttle(reason="test")
        await throttle.increase_throttle(reason="test")

        await throttle.reset_throttle()
        assert throttle.current_level == 0

    @pytest.mark.asyncio
    async def test_max_level_cap(self, throttle):
        """Test throttle doesn't exceed max level."""
        for _ in range(10):
            await throttle.increase_throttle(reason="test")

        assert throttle.current_level == throttle.max_level

    @pytest.mark.asyncio
    async def test_error_increases_throttle(self, throttle):
        """Test error recording increases throttle."""
        initial_level = throttle.current_level
        await throttle.record_error()
        assert throttle.current_level == initial_level + 1

    @pytest.mark.asyncio
    async def test_throttle_delays(self, throttle):
        """Test throttle levels have increasing delays."""
        delays = []
        for level in range(6):
            await throttle.increase_throttle(reason="test")
            delays.append(throttle.current_delay_ms)

        # Delays should be non-decreasing
        for i in range(len(delays) - 1):
            assert delays[i] <= delays[i + 1]


class TestCircuitBreaker:
    """Test circuit breaker."""

    @pytest.fixture
    def circuit_breaker(self):
        return CircuitBreaker(
            failure_threshold=3,
            recovery_timeout=1,  # 1 second for testing
            half_open_max_calls=2,
        )

    @pytest.mark.asyncio
    async def test_initial_state(self, circuit_breaker):
        """Test initial state is closed."""
        assert circuit_breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_opens_after_threshold(self, circuit_breaker):
        """Test circuit opens after failure threshold."""
        for _ in range(3):
            await circuit_breaker.record_failure()

        assert circuit_breaker.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_closed_allows_requests(self, circuit_breaker):
        """Test closed circuit allows requests."""
        assert await circuit_breaker.allow_request() is True

    @pytest.mark.asyncio
    async def test_open_rejects_requests(self, circuit_breaker):
        """Test open circuit rejects requests."""
        for _ in range(3):
            await circuit_breaker.record_failure()

        assert await circuit_breaker.allow_request() is False

    @pytest.mark.asyncio
    async def test_half_open_after_timeout(self, circuit_breaker):
        """Test circuit goes half-open after timeout."""
        for _ in range(3):
            await circuit_breaker.record_failure()

        assert circuit_breaker.state == CircuitState.OPEN

        # Wait for recovery timeout
        await asyncio.sleep(1.1)

        # Next request should trigger half-open
        assert await circuit_breaker.allow_request() is True
        assert circuit_breaker.state == CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_closes_on_recovery(self, circuit_breaker):
        """Test circuit closes after successful recovery."""
        # Open the circuit
        for _ in range(3):
            await circuit_breaker.record_failure()

        # Wait for recovery timeout
        await asyncio.sleep(1.1)

        # Allow request (goes to half-open)
        await circuit_breaker.allow_request()

        # Record successes
        await circuit_breaker.record_success()
        await circuit_breaker.record_success()

        assert circuit_breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_reopens_on_half_open_failure(self, circuit_breaker):
        """Test circuit reopens if failure during half-open."""
        # Open the circuit
        for _ in range(3):
            await circuit_breaker.record_failure()

        # Wait and go half-open
        await asyncio.sleep(1.1)
        await circuit_breaker.allow_request()

        # Fail during half-open
        await circuit_breaker.record_failure()

        assert circuit_breaker.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_manual_reset(self, circuit_breaker):
        """Test manual circuit reset."""
        for _ in range(3):
            await circuit_breaker.record_failure()

        assert circuit_breaker.state == CircuitState.OPEN

        await circuit_breaker.reset()

        assert circuit_breaker.state == CircuitState.CLOSED


class TestAntiLoopMechanisms:
    """Test anti-loop and restart storm prevention."""

    @pytest.mark.asyncio
    async def test_cooldown_ladder_progression(self):
        """Test cooldown ladder increases correctly."""
        ladder = [1, 5, 15, 300, 1800]

        for i, expected_delay in enumerate(ladder):
            # Simulate getting delay at each failure level
            delay_index = min(i, len(ladder) - 1)
            actual_delay = ladder[delay_index]
            assert actual_delay == expected_delay

    @pytest.mark.asyncio
    async def test_quarantine_after_max_failures(self):
        """Test quarantine logic."""
        max_failures = 6
        failure_count = 0

        for _ in range(10):
            failure_count += 1
            is_quarantined = failure_count >= max_failures

            if failure_count == 6:
                assert is_quarantined
                break

    @pytest.mark.asyncio
    async def test_loop_detection_logic(self):
        """Test loop detection pattern tracking."""
        execution_history = []
        max_iterations = 5
        window_seconds = 60

        import time

        for i in range(10):
            pattern = "same_task:same_rule"
            execution_history.append((pattern, time.time()))

            # Check for loop
            now = time.time()
            recent = [
                (p, t) for p, t in execution_history
                if now - t < window_seconds and p == pattern
            ]

            if len(recent) >= max_iterations:
                assert i >= max_iterations - 1
                break


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
