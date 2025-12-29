"""Resource guards for low-resource environments."""

import asyncio
import gc
import os
import sys
from typing import Optional
import structlog

from ..core.errors import SafetyError


logger = structlog.get_logger()


class MemoryGuard:
    """
    Memory usage monitor and guard.

    Designed for low-resource environments (8GB RAM target).
    Triggers cleanup when memory exceeds threshold.
    """

    def __init__(
        self,
        max_memory_mb: int = 4096,
        gc_threshold_mb: int = 3072,
        check_interval_seconds: float = 30.0,
    ):
        self.max_memory_mb = max_memory_mb
        self.gc_threshold_mb = gc_threshold_mb
        self.check_interval = check_interval_seconds

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._cleanup_callbacks: list = []

    async def start(self) -> None:
        """Start memory monitoring."""
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info(
            "memory_guard_started",
            max_mb=self.max_memory_mb,
            gc_threshold_mb=self.gc_threshold_mb,
        )

    async def stop(self) -> None:
        """Stop memory monitoring."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def register_cleanup_callback(self, callback) -> None:
        """Register a callback to be called during memory pressure cleanup."""
        self._cleanup_callbacks.append(callback)

    def get_memory_usage_mb(self) -> float:
        """Get current process memory usage in MB."""
        try:
            # Try to use resource module (Unix)
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF)
            # maxrss is in KB on Linux, bytes on macOS
            if sys.platform == "darwin":
                return usage.ru_maxrss / (1024 * 1024)
            else:
                return usage.ru_maxrss / 1024
        except ImportError:
            # Fallback: try /proc on Linux
            try:
                with open("/proc/self/status") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            return int(line.split()[1]) / 1024
            except (FileNotFoundError, ValueError):
                pass

            # Last resort: use sys.getsizeof estimate (inaccurate)
            return 0

    def check_memory_available(self, required_mb: float = 100) -> bool:
        """Check if there's enough memory available."""
        current = self.get_memory_usage_mb()
        available = self.max_memory_mb - current
        return available >= required_mb

    async def _monitor_loop(self) -> None:
        """Background memory monitoring loop."""
        while self._running:
            try:
                await asyncio.sleep(self.check_interval)

                current_mb = self.get_memory_usage_mb()

                if current_mb >= self.max_memory_mb:
                    logger.error(
                        "memory_limit_exceeded",
                        current_mb=current_mb,
                        limit_mb=self.max_memory_mb,
                    )
                    raise SafetyError(
                        f"Memory limit exceeded: {current_mb:.0f}MB / {self.max_memory_mb}MB",
                        rule="memory_limit",
                    )

                elif current_mb >= self.gc_threshold_mb:
                    logger.warning(
                        "memory_pressure",
                        current_mb=current_mb,
                        threshold_mb=self.gc_threshold_mb,
                    )
                    await self._trigger_cleanup()

            except asyncio.CancelledError:
                break
            except SafetyError:
                raise
            except Exception:
                logger.exception("memory_monitor_error")

    async def _trigger_cleanup(self) -> None:
        """Trigger memory cleanup."""
        logger.info("triggering_memory_cleanup")

        # Force garbage collection
        gc.collect()

        # Call registered cleanup callbacks
        for callback in self._cleanup_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback()
                else:
                    callback()
            except Exception:
                logger.exception("cleanup_callback_error")

        # Check if cleanup helped
        after_mb = self.get_memory_usage_mb()
        logger.info("memory_cleanup_complete", memory_mb=after_mb)


class ResourceGuard:
    """
    Composite resource guard for low-resource environments.

    Monitors:
    - Memory usage
    - Task count
    - Error rate
    """

    def __init__(
        self,
        max_memory_mb: int = 4096,
        max_concurrent_tasks: int = 5,
        error_rate_threshold: float = 0.5,
        error_window_seconds: int = 60,
    ):
        self.memory_guard = MemoryGuard(max_memory_mb=max_memory_mb)
        self.max_concurrent_tasks = max_concurrent_tasks
        self.error_rate_threshold = error_rate_threshold
        self.error_window = error_window_seconds

        self._current_tasks = 0
        self._task_lock = asyncio.Lock()

        # Error tracking
        self._errors: list[float] = []
        self._successes: list[float] = []
        self._tracking_lock = asyncio.Lock()

    async def start(self) -> None:
        """Start resource monitoring."""
        await self.memory_guard.start()

    async def stop(self) -> None:
        """Stop resource monitoring."""
        await self.memory_guard.stop()

    async def acquire_task_slot(self) -> bool:
        """Try to acquire a task execution slot."""
        async with self._task_lock:
            if self._current_tasks >= self.max_concurrent_tasks:
                return False

            # Check memory
            if not self.memory_guard.check_memory_available(required_mb=50):
                logger.warning("task_slot_denied_memory")
                return False

            # Check error rate
            error_rate = await self._get_error_rate()
            if error_rate > self.error_rate_threshold:
                logger.warning("task_slot_denied_error_rate", rate=error_rate)
                return False

            self._current_tasks += 1
            return True

    async def release_task_slot(self, success: bool = True) -> None:
        """Release a task execution slot."""
        async with self._task_lock:
            self._current_tasks = max(0, self._current_tasks - 1)

        # Record result for error rate tracking
        await self._record_result(success)

    async def _record_result(self, success: bool) -> None:
        """Record task result for error rate calculation."""
        import time
        now = time.time()

        async with self._tracking_lock:
            if success:
                self._successes.append(now)
            else:
                self._errors.append(now)

            # Cleanup old entries
            cutoff = now - self.error_window
            self._errors = [t for t in self._errors if t > cutoff]
            self._successes = [t for t in self._successes if t > cutoff]

    async def _get_error_rate(self) -> float:
        """Get error rate in the current window."""
        async with self._tracking_lock:
            total = len(self._errors) + len(self._successes)
            if total == 0:
                return 0.0
            return len(self._errors) / total

    async def get_status(self) -> dict:
        """Get resource guard status."""
        error_rate = await self._get_error_rate()
        return {
            "memory_mb": self.memory_guard.get_memory_usage_mb(),
            "memory_limit_mb": self.memory_guard.max_memory_mb,
            "current_tasks": self._current_tasks,
            "max_tasks": self.max_concurrent_tasks,
            "error_rate": error_rate,
            "error_threshold": self.error_rate_threshold,
        }
