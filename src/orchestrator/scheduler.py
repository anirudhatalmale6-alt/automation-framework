"""Task scheduling with priority queues and rate limiting."""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from enum import Enum
import heapq

from core.config import ThrottleConfig, FrameworkConfig
from core.state import StateManager, TaskStatus
from core.errors import ThrottleError


@dataclass(order=True)
class ScheduledTask:
    """A task in the priority queue."""
    priority: int  # Negative for max-heap behavior (higher priority = lower value)
    created_at: float
    task_id: str = field(compare=False)
    workflow_id: Optional[str] = field(compare=False, default=None)
    rule_id: Optional[str] = field(compare=False, default=None)
    action: str = field(compare=False, default="")
    payload: dict = field(compare=False, default_factory=dict)
    max_attempts: int = field(compare=False, default=3)


class RateLimiter:
    """Token bucket rate limiter."""

    def __init__(self, rate: float, capacity: float):
        """
        Args:
            rate: Tokens per second to add
            capacity: Maximum tokens in bucket
        """
        self.rate = rate
        self.capacity = capacity
        self._tokens = capacity
        self._last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: int = 1, timeout: Optional[float] = None) -> bool:
        """
        Acquire tokens from the bucket.

        Returns True if tokens were acquired, False if timeout.
        """
        start_time = time.monotonic()

        async with self._lock:
            while True:
                self._refill()

                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True

                # Calculate wait time
                wait_time = (tokens - self._tokens) / self.rate

                if timeout is not None:
                    elapsed = time.monotonic() - start_time
                    if elapsed + wait_time > timeout:
                        return False

                # Release lock while waiting
                self._lock.release()
                try:
                    await asyncio.sleep(min(wait_time, 0.1))
                finally:
                    await self._lock.acquire()

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_update
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last_update = now

    @property
    def available_tokens(self) -> float:
        """Get current available tokens (approximate)."""
        return self._tokens


class TaskScheduler:
    """
    Manages task scheduling with priority queues, rate limiting, and throttling.

    Features:
    - Priority-based task ordering
    - Rate limiting (tasks per minute, browser actions per minute)
    - Concurrent task limits
    - Cooldown enforcement from failure tracking
    """

    def __init__(
        self,
        config: FrameworkConfig,
        state_manager: StateManager,
    ):
        self.config = config
        self.state = state_manager
        self.throttle = config.throttle

        # Priority queue (min-heap with negative priority for max-heap behavior)
        self._queue: list[ScheduledTask] = []
        self._queue_lock = asyncio.Lock()

        # Rate limiters
        self._task_limiter = RateLimiter(
            rate=self.throttle.max_tasks_per_minute / 60.0,
            capacity=self.throttle.max_tasks_per_minute,
        )
        self._browser_limiter = RateLimiter(
            rate=self.throttle.max_browser_actions_per_minute / 60.0,
            capacity=self.throttle.max_browser_actions_per_minute,
        )

        # Concurrency tracking
        self._running_count = 0
        self._running_lock = asyncio.Lock()

        # Shutdown flag
        self._shutdown = False

        # Task lookup for deduplication
        self._pending_task_ids: set[str] = set()

    async def schedule(
        self,
        action: str,
        payload: dict[str, Any],
        priority: int = 0,
        workflow_id: Optional[str] = None,
        rule_id: Optional[str] = None,
        task_id: Optional[str] = None,
        max_attempts: int = 3,
    ) -> str:
        """
        Schedule a task for execution.

        Args:
            action: Action type to execute
            payload: Task payload/parameters
            priority: Higher values = higher priority (default 0)
            workflow_id: Associated workflow ID
            rule_id: Associated rule ID
            task_id: Custom task ID (generated if not provided)
            max_attempts: Maximum retry attempts

        Returns:
            Task ID
        """
        if self._shutdown:
            raise RuntimeError("Scheduler is shutting down")

        task_id = task_id or f"task_{uuid.uuid4().hex[:12]}"

        # Check for duplicate
        async with self._queue_lock:
            if task_id in self._pending_task_ids:
                return task_id

            task = ScheduledTask(
                priority=-priority,  # Negative for max-heap
                created_at=time.time(),
                task_id=task_id,
                workflow_id=workflow_id,
                rule_id=rule_id,
                action=action,
                payload=payload,
                max_attempts=max_attempts,
            )

            heapq.heappush(self._queue, task)
            self._pending_task_ids.add(task_id)

        # Create task record in state
        await self.state.create_task(
            task_id=task_id,
            workflow_id=workflow_id,
            rule_id=rule_id,
            priority=priority,
            input_data={"action": action, "payload": payload},
            max_attempts=max_attempts,
        )

        return task_id

    async def get_next_task(self, timeout: float = 5.0) -> Optional[ScheduledTask]:
        """
        Get the next task to execute.

        Respects rate limits and concurrency constraints.
        Returns None if no task is available or timeout reached.
        """
        if self._shutdown:
            return None

        start_time = time.monotonic()

        while time.monotonic() - start_time < timeout:
            # Check concurrency limit
            async with self._running_lock:
                if self._running_count >= self.throttle.max_concurrent_tasks:
                    await asyncio.sleep(0.1)
                    continue

            # Try to acquire rate limit token
            if not await self._task_limiter.acquire(timeout=0.1):
                continue

            # Get task from queue
            async with self._queue_lock:
                if not self._queue:
                    # Return token since we didn't use it
                    self._task_limiter._tokens += 1
                    await asyncio.sleep(0.1)
                    continue

                task = heapq.heappop(self._queue)
                self._pending_task_ids.discard(task.task_id)

            # Check if task is in cooldown
            # Generate a fingerprint key for this task type
            cooldown_key = f"{task.action}:{task.rule_id or 'none'}"
            if await self.state.is_in_cooldown(cooldown_key):
                # Re-queue with delay
                async with self._queue_lock:
                    heapq.heappush(self._queue, task)
                    self._pending_task_ids.add(task.task_id)
                await asyncio.sleep(0.5)
                continue

            # Check if quarantined
            if await self.state.is_quarantined(cooldown_key):
                # Don't re-queue quarantined tasks
                await self.state.update_task_status(
                    task.task_id,
                    TaskStatus.QUARANTINED
                )
                continue

            # Mark as running
            async with self._running_lock:
                self._running_count += 1

            await self.state.update_task_status(task.task_id, TaskStatus.RUNNING)
            return task

        return None

    async def complete_task(self, task_id: str, success: bool, output: Optional[dict] = None, error: Optional[dict] = None) -> None:
        """Mark a task as completed or failed."""
        async with self._running_lock:
            self._running_count = max(0, self._running_count - 1)

        status = TaskStatus.COMPLETED if success else TaskStatus.FAILED
        await self.state.update_task_status(task_id, status, output=output, error=error)

    async def acquire_browser_action(self, timeout: float = 30.0) -> bool:
        """Acquire a browser action rate limit token."""
        return await self._browser_limiter.acquire(timeout=timeout)

    async def release_browser_action(self) -> None:
        """Release a browser action token (if action didn't execute)."""
        self._browser_limiter._tokens = min(
            self._browser_limiter.capacity,
            self._browser_limiter._tokens + 1
        )

    def queue_size(self) -> int:
        """Get current queue size."""
        return len(self._queue)

    def running_count(self) -> int:
        """Get number of currently running tasks."""
        return self._running_count

    async def shutdown(self, timeout: float = 30.0) -> None:
        """
        Graceful shutdown.

        Waits for running tasks to complete up to timeout.
        """
        self._shutdown = True

        start_time = time.monotonic()
        while self._running_count > 0:
            if time.monotonic() - start_time > timeout:
                break
            await asyncio.sleep(0.5)

    async def clear_queue(self) -> int:
        """Clear all pending tasks from queue. Returns count of cleared tasks."""
        async with self._queue_lock:
            count = len(self._queue)
            self._queue.clear()
            self._pending_task_ids.clear()
            return count

    async def get_queue_stats(self) -> dict[str, Any]:
        """Get queue statistics."""
        return {
            "queue_size": self.queue_size(),
            "running_count": self.running_count(),
            "max_concurrent": self.throttle.max_concurrent_tasks,
            "task_rate_available": self._task_limiter.available_tokens,
            "browser_rate_available": self._browser_limiter.available_tokens,
        }
