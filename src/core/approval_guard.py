"""
Approval Fatigue Guard.

Prevents approval fatigue by:
- Rate-limiting approval requests
- Burst throttling
- Auto-deferring non-critical approvals
- Batching similar requests
"""

import asyncio
import time
from typing import Any, Callable, Optional, Awaitable, Dict, List
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from collections import deque
import structlog

logger = structlog.get_logger()


class ApprovalPriority(Enum):
    """Priority levels for approvals."""
    CRITICAL = "critical"    # OTP, CAPTCHA, payment confirmation
    HIGH = "high"            # Important decisions
    NORMAL = "normal"        # Standard approvals
    LOW = "low"              # Can be deferred/batched


@dataclass
class ApprovalRequest:
    """An approval request."""
    id: str
    action_type: str
    description: str
    priority: ApprovalPriority
    context: Dict[str, Any]
    created_at: datetime = field(default_factory=datetime.now)
    deferred_until: Optional[datetime] = None
    batch_key: Optional[str] = None  # For grouping similar requests


@dataclass
class ApprovalGuardConfig:
    """Configuration for approval fatigue guard."""
    enabled: bool = True

    # Rate limits
    max_approvals_per_hour: int = 20
    max_approvals_per_minute: int = 5
    burst_limit: int = 3  # Max rapid approvals before throttle
    burst_window_seconds: int = 60

    # Deferral
    defer_low_priority: bool = True
    defer_duration_minutes: int = 30
    batch_similar_requests: bool = True
    batch_window_minutes: int = 5

    # Critical actions that always get through
    critical_action_types: List[str] = field(default_factory=lambda: [
        "otp_required",
        "captcha_required",
        "payment_confirmation",
        "security_alert",
    ])


class ApprovalFatigueGuard:
    """
    Guards against approval fatigue.

    Features:
    - Rate limiting (per hour, per minute)
    - Burst detection and throttling
    - Priority-based handling
    - Deferred/batched non-critical requests
    - Always allows critical actions
    """

    def __init__(self, config: Optional[ApprovalGuardConfig] = None):
        self.config = config or ApprovalGuardConfig()

        # Tracking
        self._approval_times: deque = deque(maxlen=1000)
        self._burst_times: deque = deque(maxlen=100)
        self._deferred_requests: Dict[str, ApprovalRequest] = {}
        self._batched_requests: Dict[str, List[ApprovalRequest]] = {}

        # Stats
        self._total_requests = 0
        self._total_allowed = 0
        self._total_deferred = 0
        self._total_throttled = 0

        # Callback for sending deferred approvals
        self._on_deferred_ready: Optional[Callable[[List[ApprovalRequest]], Awaitable[None]]] = None

        # Background task
        self._check_task: Optional[asyncio.Task] = None
        self._running = False

    def set_deferred_callback(
        self,
        callback: Callable[[List[ApprovalRequest]], Awaitable[None]],
    ) -> None:
        """Set callback for when deferred requests are ready."""
        self._on_deferred_ready = callback

    async def start(self) -> None:
        """Start the fatigue guard background tasks."""
        if not self.config.enabled:
            logger.info("approval_guard_disabled")
            return

        self._running = True
        self._check_task = asyncio.create_task(self._check_deferred_loop())
        logger.info(
            "approval_guard_started",
            max_per_hour=self.config.max_approvals_per_hour,
            max_per_minute=self.config.max_approvals_per_minute,
            burst_limit=self.config.burst_limit,
        )

    async def stop(self) -> None:
        """Stop the fatigue guard."""
        self._running = False
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
        logger.info("approval_guard_stopped")

    async def should_allow(self, request: ApprovalRequest) -> tuple[bool, str]:
        """
        Check if an approval request should be allowed now.

        Returns:
            (allowed, reason) - Whether to allow and why
        """
        self._total_requests += 1

        # Critical actions always pass
        if request.action_type in self.config.critical_action_types:
            self._record_approval()
            self._total_allowed += 1
            logger.info(
                "approval_allowed_critical",
                request_id=request.id,
                action_type=request.action_type,
            )
            return True, "critical_action"

        if request.priority == ApprovalPriority.CRITICAL:
            self._record_approval()
            self._total_allowed += 1
            return True, "critical_priority"

        # Check rate limits
        if self._is_rate_limited():
            self._total_throttled += 1
            logger.warning(
                "approval_rate_limited",
                request_id=request.id,
                hourly_count=self._get_hourly_count(),
                minutely_count=self._get_minutely_count(),
            )

            # Defer non-critical requests
            if request.priority in (ApprovalPriority.LOW, ApprovalPriority.NORMAL):
                await self._defer_request(request)
                return False, "rate_limited_deferred"

            return False, "rate_limited"

        # Check burst
        if self._is_burst():
            self._total_throttled += 1
            logger.warning(
                "approval_burst_detected",
                request_id=request.id,
                burst_count=len(self._burst_times),
            )

            # Defer low priority
            if request.priority == ApprovalPriority.LOW:
                await self._defer_request(request)
                return False, "burst_deferred"

            # High priority still goes through with warning
            if request.priority == ApprovalPriority.HIGH:
                self._record_approval()
                self._total_allowed += 1
                return True, "burst_allowed_high_priority"

            return False, "burst_throttled"

        # Check if should batch
        if (
            self.config.batch_similar_requests
            and request.priority == ApprovalPriority.LOW
            and request.batch_key
        ):
            await self._batch_request(request)
            return False, "batched"

        # Allow the request
        self._record_approval()
        self._total_allowed += 1
        logger.info(
            "approval_allowed",
            request_id=request.id,
            priority=request.priority.value,
        )
        return True, "allowed"

    def get_stats(self) -> Dict[str, Any]:
        """Get approval statistics."""
        return {
            "total_requests": self._total_requests,
            "total_allowed": self._total_allowed,
            "total_deferred": self._total_deferred,
            "total_throttled": self._total_throttled,
            "hourly_count": self._get_hourly_count(),
            "minutely_count": self._get_minutely_count(),
            "pending_deferred": len(self._deferred_requests),
            "pending_batched": sum(len(v) for v in self._batched_requests.values()),
        }

    def _record_approval(self) -> None:
        """Record an approval for rate tracking."""
        now = time.time()
        self._approval_times.append(now)
        self._burst_times.append(now)

    def _get_hourly_count(self) -> int:
        """Get approval count in last hour."""
        cutoff = time.time() - 3600
        return sum(1 for t in self._approval_times if t > cutoff)

    def _get_minutely_count(self) -> int:
        """Get approval count in last minute."""
        cutoff = time.time() - 60
        return sum(1 for t in self._approval_times if t > cutoff)

    def _is_rate_limited(self) -> bool:
        """Check if rate limit is exceeded."""
        return (
            self._get_hourly_count() >= self.config.max_approvals_per_hour
            or self._get_minutely_count() >= self.config.max_approvals_per_minute
        )

    def _is_burst(self) -> bool:
        """Check if in burst mode."""
        cutoff = time.time() - self.config.burst_window_seconds
        burst_count = sum(1 for t in self._burst_times if t > cutoff)
        return burst_count >= self.config.burst_limit

    async def _defer_request(self, request: ApprovalRequest) -> None:
        """Defer a request for later."""
        request.deferred_until = datetime.now() + timedelta(
            minutes=self.config.defer_duration_minutes
        )
        self._deferred_requests[request.id] = request
        self._total_deferred += 1
        logger.info(
            "approval_deferred",
            request_id=request.id,
            until=request.deferred_until.isoformat(),
        )

    async def _batch_request(self, request: ApprovalRequest) -> None:
        """Add request to a batch."""
        batch_key = request.batch_key
        if batch_key not in self._batched_requests:
            self._batched_requests[batch_key] = []
        self._batched_requests[batch_key].append(request)
        self._total_deferred += 1
        logger.info(
            "approval_batched",
            request_id=request.id,
            batch_key=batch_key,
            batch_size=len(self._batched_requests[batch_key]),
        )

    async def _check_deferred_loop(self) -> None:
        """Background loop to check and release deferred requests."""
        while self._running:
            try:
                await asyncio.sleep(60)  # Check every minute
                await self._process_deferred()
                await self._process_batches()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("approval_guard_check_error")

    async def _process_deferred(self) -> None:
        """Process deferred requests that are ready."""
        now = datetime.now()
        ready = []

        for request_id, request in list(self._deferred_requests.items()):
            if request.deferred_until and request.deferred_until <= now:
                ready.append(request)
                del self._deferred_requests[request_id]

        if ready and self._on_deferred_ready:
            logger.info("deferred_approvals_ready", count=len(ready))
            await self._on_deferred_ready(ready)

    async def _process_batches(self) -> None:
        """Process batched requests that are ready."""
        now = datetime.now()
        cutoff = now - timedelta(minutes=self.config.batch_window_minutes)

        ready_batches = []

        for batch_key, requests in list(self._batched_requests.items()):
            # Check if oldest request in batch is old enough
            if requests and requests[0].created_at < cutoff:
                ready_batches.extend(requests)
                del self._batched_requests[batch_key]

        if ready_batches and self._on_deferred_ready:
            logger.info("batched_approvals_ready", count=len(ready_batches))
            await self._on_deferred_ready(ready_batches)
