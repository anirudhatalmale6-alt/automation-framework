"""Adaptive throttling for resource-constrained environments."""

import asyncio
import time
from typing import Optional
from dataclasses import dataclass
import structlog


logger = structlog.get_logger()


@dataclass
class ThrottleState:
    """Current throttle state."""
    level: int          # 0 = normal, 1-5 = increasingly throttled
    delay_ms: float     # Current delay between operations
    reason: str         # Why throttled
    since: float        # When this level started


class AdaptiveThrottle:
    """
    Adaptive throttling that adjusts based on system conditions.

    Automatically increases delays when:
    - Memory pressure detected
    - Error rate increases
    - Response times increase

    Gradually decreases throttle when conditions improve.
    """

    # Throttle levels and their delays
    LEVELS = {
        0: 0,       # Normal - no delay
        1: 100,     # Light - 100ms
        2: 500,     # Moderate - 500ms
        3: 1000,    # Heavy - 1s
        4: 5000,    # Severe - 5s
        5: 10000,   # Critical - 10s
    }

    def __init__(
        self,
        base_delay_ms: float = 0,
        max_level: int = 5,
        recovery_interval_seconds: float = 30.0,
    ):
        self.base_delay_ms = base_delay_ms
        self.max_level = max_level
        self.recovery_interval = recovery_interval_seconds

        self._state = ThrottleState(
            level=0,
            delay_ms=base_delay_ms,
            reason="normal",
            since=time.time(),
        )
        self._lock = asyncio.Lock()

        # Metrics for adaptive behavior
        self._recent_response_times: list[float] = []
        self._recent_errors: list[float] = []
        self._last_recovery_check = time.time()

    @property
    def current_level(self) -> int:
        """Get current throttle level."""
        return self._state.level

    @property
    def current_delay_ms(self) -> float:
        """Get current delay in milliseconds."""
        return self._state.delay_ms

    async def wait(self) -> None:
        """Wait according to current throttle level."""
        delay_ms = self._state.delay_ms
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

    async def increase_throttle(self, reason: str = "manual") -> int:
        """Increase throttle level by one."""
        async with self._lock:
            if self._state.level < self.max_level:
                new_level = self._state.level + 1
                self._state = ThrottleState(
                    level=new_level,
                    delay_ms=self.LEVELS.get(new_level, self.LEVELS[self.max_level]),
                    reason=reason,
                    since=time.time(),
                )
                logger.warning(
                    "throttle_increased",
                    level=new_level,
                    delay_ms=self._state.delay_ms,
                    reason=reason,
                )
            return self._state.level

    async def decrease_throttle(self) -> int:
        """Decrease throttle level by one."""
        async with self._lock:
            if self._state.level > 0:
                new_level = self._state.level - 1
                self._state = ThrottleState(
                    level=new_level,
                    delay_ms=self.LEVELS.get(new_level, 0),
                    reason="recovery",
                    since=time.time(),
                )
                logger.info(
                    "throttle_decreased",
                    level=new_level,
                    delay_ms=self._state.delay_ms,
                )
            return self._state.level

    async def reset_throttle(self) -> None:
        """Reset throttle to normal."""
        async with self._lock:
            self._state = ThrottleState(
                level=0,
                delay_ms=self.base_delay_ms,
                reason="reset",
                since=time.time(),
            )
            logger.info("throttle_reset")

    async def record_response_time(self, duration_ms: float) -> None:
        """Record a response time for adaptive throttling."""
        self._recent_response_times.append(duration_ms)

        # Keep only last 100 entries
        if len(self._recent_response_times) > 100:
            self._recent_response_times = self._recent_response_times[-100:]

        # Check if we should increase throttle
        await self._check_response_times()

    async def record_error(self) -> None:
        """Record an error for adaptive throttling."""
        self._recent_errors.append(time.time())

        # Cleanup old errors (last 5 minutes)
        cutoff = time.time() - 300
        self._recent_errors = [t for t in self._recent_errors if t > cutoff]

        # Increase throttle on error
        await self.increase_throttle(reason="error")

    async def record_success(self) -> None:
        """Record a success, potentially decreasing throttle."""
        now = time.time()

        # Only check recovery periodically
        if now - self._last_recovery_check >= self.recovery_interval:
            self._last_recovery_check = now
            await self._check_recovery()

    async def _check_response_times(self) -> None:
        """Check response times and adjust throttle."""
        if len(self._recent_response_times) < 10:
            return

        # Calculate average of recent response times
        recent = self._recent_response_times[-10:]
        avg_ms = sum(recent) / len(recent)

        # Thresholds for increasing throttle
        if avg_ms > 5000:  # > 5s average
            await self.increase_throttle(reason="slow_responses")
        elif avg_ms > 2000:  # > 2s average
            if self._state.level < 2:
                await self.increase_throttle(reason="moderate_slowdown")

    async def _check_recovery(self) -> None:
        """Check if conditions have improved for recovery."""
        if self._state.level == 0:
            return

        # Check error rate
        recent_errors = len([
            t for t in self._recent_errors
            if t > time.time() - 60  # Last minute
        ])

        if recent_errors > 3:
            # Still too many errors
            return

        # Check response times
        if self._recent_response_times:
            recent = self._recent_response_times[-10:]
            avg_ms = sum(recent) / len(recent)
            if avg_ms > 2000:
                # Still slow
                return

        # Conditions improved, decrease throttle
        await self.decrease_throttle()

    def get_state(self) -> ThrottleState:
        """Get current throttle state."""
        return self._state

    def get_stats(self) -> dict:
        """Get throttle statistics."""
        recent_response_times = self._recent_response_times[-10:] if self._recent_response_times else []
        return {
            "level": self._state.level,
            "delay_ms": self._state.delay_ms,
            "reason": self._state.reason,
            "since": self._state.since,
            "avg_response_ms": sum(recent_response_times) / len(recent_response_times) if recent_response_times else 0,
            "recent_errors": len([t for t in self._recent_errors if t > time.time() - 60]),
        }
