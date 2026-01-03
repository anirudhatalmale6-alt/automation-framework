"""
Supervisor - Central orchestration brain.

Controls execution order, schedules tasks, enforces rate limits,
and prevents infinite loops and restart storms.
"""

import asyncio
import signal
import time
from typing import Any, Callable, Optional, Awaitable
from enum import Enum
import structlog

from core.config import ConfigLoader, FrameworkConfig, WorkflowDefinition, RuleDefinition
from core.state import StateManager, TaskStatus
from core.errors import (
    FrameworkError,
    SafetyError,
    ConfigError,
    ErrorSeverity,
)
from orchestrator.scheduler import TaskScheduler, ScheduledTask
from orchestrator.executor import TaskExecutor, WorkflowExecutor, ExecutionResult


logger = structlog.get_logger()


class SupervisorState(Enum):
    """Supervisor operational states."""
    INITIALIZING = "initializing"
    RUNNING = "running"
    PAUSED = "paused"
    DRAINING = "draining"  # Completing current tasks, not accepting new
    SHUTDOWN = "shutdown"


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing recovery


class CircuitBreaker:
    """
    Circuit breaker for global failure protection.

    Prevents cascade failures by stopping all execution when
    failure rate exceeds threshold.
    """

    def __init__(
        self,
        failure_threshold: int,
        recovery_timeout: int,
        half_open_max_calls: int,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._half_open_calls = 0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    async def record_success(self) -> None:
        """Record a successful execution."""
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_calls += 1
                if self._half_open_calls >= self.half_open_max_calls:
                    # Recovery confirmed, close circuit
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    self._half_open_calls = 0
                    logger.info("circuit_breaker_closed", reason="recovery_confirmed")

    async def record_failure(self) -> None:
        """Record a failed execution."""
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                # Failed during recovery test, reopen
                self._state = CircuitState.OPEN
                self._half_open_calls = 0
                logger.warning("circuit_breaker_reopened", reason="recovery_failed")

            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.failure_threshold:
                    self._state = CircuitState.OPEN
                    logger.warning(
                        "circuit_breaker_opened",
                        failure_count=self._failure_count,
                        threshold=self.failure_threshold,
                    )

    async def allow_request(self) -> bool:
        """Check if a request should be allowed."""
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                # Check if recovery timeout has passed
                if time.time() - self._last_failure_time >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_calls = 0
                    logger.info("circuit_breaker_half_open", reason="timeout_expired")
                    return True
                return False

            # HALF_OPEN - allow limited calls
            return self._half_open_calls < self.half_open_max_calls

    async def reset(self) -> None:
        """Manually reset the circuit breaker."""
        async with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._half_open_calls = 0
            logger.info("circuit_breaker_reset", reason="manual")


class Supervisor:
    """
    Central orchestration supervisor.

    Responsibilities:
    - Manage task scheduling and execution
    - Load and monitor configuration changes
    - Enforce safety constraints
    - Handle graceful shutdown
    - Coordinate between modules
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        data_dir: str = "./data",
    ):
        self.config_path = config_path
        self.data_dir = data_dir

        # State
        self._state = SupervisorState.INITIALIZING
        self._start_time: Optional[float] = None

        # Components (initialized in start())
        self.config: Optional[FrameworkConfig] = None
        self.config_loader: Optional[ConfigLoader] = None
        self.state_manager: Optional[StateManager] = None
        self.scheduler: Optional[TaskScheduler] = None
        self.task_executor: Optional[TaskExecutor] = None
        self.workflow_executor: Optional[WorkflowExecutor] = None
        self.circuit_breaker: Optional[CircuitBreaker] = None

        # Loaded configs
        self._rules: list[RuleDefinition] = []
        self._workflows: list[WorkflowDefinition] = []
        self._workflow_commands: dict[str, WorkflowDefinition] = {}
        self._startup_errors: list[str] = []

        # Background tasks
        self._worker_tasks: list[asyncio.Task] = []
        self._maintenance_task: Optional[asyncio.Task] = None
        self._schedule_task: Optional[asyncio.Task] = None

        # Schedule tracking
        self._last_schedule_check: dict[str, int] = {}  # rule_id -> last minute checked

        # Shutdown coordination
        self._shutdown_event = asyncio.Event()

        # Escalation callback
        self._escalation_handler: Optional[Callable[[str, dict], Awaitable[None]]] = None

    async def start(self) -> None:
        """Initialize and start the supervisor."""
        logger.info("supervisor_starting")

        # Load configuration
        self.config_loader = ConfigLoader()
        if self.config_path:
            self.config = self.config_loader.load_framework_config(self.config_path)
        else:
            self.config = FrameworkConfig()

        # Initialize state manager
        self.state_manager = StateManager(f"{self.data_dir}/state.db")
        await self.state_manager.initialize()

        # Check for restart storm - load failure state before scheduling
        await self._check_restart_safety()

        # Initialize scheduler and executor
        self.scheduler = TaskScheduler(self.config, self.state_manager)
        self.task_executor = TaskExecutor(self.config, self.state_manager)
        self.workflow_executor = WorkflowExecutor(
            self.task_executor,
            self.state_manager,
            self.config,
        )

        # Initialize circuit breaker
        cb_config = self.config.circuit_breaker
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=cb_config.failure_threshold,
            recovery_timeout=cb_config.recovery_timeout_seconds,
            half_open_max_calls=cb_config.half_open_max_calls,
        )

        # Load rules and workflows (don't crash on errors - start with partial config)
        self._startup_errors = await self._load_configs(raise_on_error=False)

        # Start worker tasks
        self._state = SupervisorState.RUNNING
        self._start_time = time.time()

        # Start worker coroutines
        num_workers = min(self.config.throttle.max_concurrent_tasks, 3)
        for i in range(num_workers):
            task = asyncio.create_task(self._worker_loop(i))
            self._worker_tasks.append(task)

        # Start maintenance task
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

        # Start schedule runner task
        self._schedule_task = asyncio.create_task(self._schedule_runner_loop())

        logger.info(
            "supervisor_started",
            workers=num_workers,
            rules=len(self._rules),
            workflows=len(self._workflows),
        )

    async def stop(self, timeout: float = 30.0) -> None:
        """Gracefully stop the supervisor."""
        if self._state == SupervisorState.SHUTDOWN:
            return

        logger.info("supervisor_stopping")
        self._state = SupervisorState.DRAINING
        self._shutdown_event.set()

        # Wait for workers to finish
        if self._worker_tasks:
            await asyncio.wait(self._worker_tasks, timeout=timeout)
            for task in self._worker_tasks:
                if not task.done():
                    task.cancel()

        # Stop maintenance
        if self._maintenance_task:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except asyncio.CancelledError:
                pass

        # Shutdown scheduler
        if self.scheduler:
            await self.scheduler.shutdown(timeout=5.0)

        # Close state manager
        if self.state_manager:
            await self.state_manager.close()

        self._state = SupervisorState.SHUTDOWN
        logger.info("supervisor_stopped")

    async def pause(self) -> None:
        """Pause task execution (complete current, don't start new)."""
        self._state = SupervisorState.PAUSED
        logger.info("supervisor_paused")

    async def resume(self) -> None:
        """Resume task execution."""
        if self._state == SupervisorState.PAUSED:
            self._state = SupervisorState.RUNNING
            logger.info("supervisor_resumed")

    # ==================== Task Submission ====================

    async def submit_task(
        self,
        action: str,
        payload: dict[str, Any],
        priority: int = 0,
        rule_id: Optional[str] = None,
    ) -> str:
        """Submit a task for execution."""
        if self._state not in (SupervisorState.RUNNING, SupervisorState.PAUSED):
            raise RuntimeError(f"Cannot submit tasks in state: {self._state}")

        return await self.scheduler.schedule(
            action=action,
            payload=payload,
            priority=priority,
            rule_id=rule_id,
        )

    async def run_workflow(
        self,
        workflow_id: str,
        parameters: Optional[dict[str, Any]] = None,
    ) -> str:
        """
        Run a workflow by ID.

        Returns task ID for tracking.
        """
        workflow = next((w for w in self._workflows if w.id == workflow_id), None)
        if not workflow:
            raise ConfigError(f"Workflow not found: {workflow_id}")

        if not workflow.enabled:
            raise ConfigError(f"Workflow is disabled: {workflow_id}")

        # Merge provided parameters with defaults
        params = dict(workflow.parameters)
        if parameters:
            params.update(parameters)

        # Check if approval required
        if workflow.requires_approval:
            approval_id = await self.state_manager.create_approval_request(
                task_id=f"workflow_{workflow_id}",
                action_type="workflow_execution",
                description=f"Execute workflow: {workflow.name}",
                context={"workflow_id": workflow_id, "parameters": params},
            )
            if self._escalation_handler:
                await self._escalation_handler(
                    "approval_required",
                    {
                        "approval_id": approval_id,
                        "workflow": workflow.name,
                        "action_type": "workflow_execution",
                        "description": f"Execute workflow: {workflow.name}",
                        "context": {"workflow_id": workflow_id, "parameters": params},
                    },
                )
            raise SafetyError(
                f"Workflow requires approval: {workflow.name}",
                rule="requires_approval",
            )

        # Schedule workflow execution
        return await self.scheduler.schedule(
            action="_workflow",
            payload={
                "workflow_id": workflow_id,
                "steps": workflow.steps,
                "parameters": params,
                "on_error": workflow.on_error,
            },
            workflow_id=workflow_id,
        )

    async def run_workflow_by_command(
        self,
        command: str,
        parameters: Optional[dict[str, Any]] = None,
    ) -> str:
        """
        Run a workflow by its trigger command.

        E.g., "/run export --format=csv"
        """
        # Parse command
        parts = command.strip().split()
        if not parts:
            raise ConfigError("Empty command")

        # Build the full command key (e.g., "run voice-test")
        full_cmd = command.strip().lstrip("/")

        # Extract just the command name for arg parsing
        cmd_parts = full_cmd.split()
        cmd_args = self._parse_command_args(cmd_parts[2:]) if len(cmd_parts) > 2 else {}

        # Find workflow by full command match
        workflow = self._workflow_commands.get(full_cmd.split("--")[0].strip())
        if not workflow:
            # Also try just the base command without parameters
            base_cmd = " ".join(cmd_parts[:2]) if len(cmd_parts) >= 2 else full_cmd
            workflow = self._workflow_commands.get(base_cmd)
        if not workflow:
            raise ConfigError(f"Unknown command: {full_cmd}")

        # Merge args with provided parameters
        params = dict(parameters or {})
        params.update(cmd_args)

        return await self.run_workflow(workflow.id, params)

    def _parse_command_args(self, args: list[str]) -> dict[str, Any]:
        """Parse command-line style arguments."""
        result = {}
        i = 0
        while i < len(args):
            arg = args[i]
            if arg.startswith("--"):
                key = arg[2:]
                if "=" in key:
                    k, v = key.split("=", 1)
                    result[k] = v
                elif i + 1 < len(args) and not args[i + 1].startswith("-"):
                    result[key] = args[i + 1]
                    i += 1
                else:
                    result[key] = True
            elif arg.startswith("-"):
                key = arg[1:]
                if i + 1 < len(args) and not args[i + 1].startswith("-"):
                    result[key] = args[i + 1]
                    i += 1
                else:
                    result[key] = True
            i += 1
        return result

    # ==================== Handler Registration ====================

    def register_action(self, action: str, handler: Callable) -> None:
        """Register a handler for an action type."""
        self.task_executor.register_handler(action, handler)

    def set_escalation_handler(
        self,
        handler: Callable[[str, dict], Awaitable[None]],
    ) -> None:
        """Set the handler for escalation events."""
        self._escalation_handler = handler

    # ==================== Internal Methods ====================

    async def _worker_loop(self, worker_id: int) -> None:
        """Worker coroutine that processes tasks from the queue."""
        logger.debug("worker_started", worker_id=worker_id)
        cb_was_open = False  # Track circuit breaker state to avoid log spam

        while not self._shutdown_event.is_set():
            # Check state
            if self._state != SupervisorState.RUNNING:
                await asyncio.sleep(0.5)
                continue

            # Check circuit breaker
            if not await self.circuit_breaker.allow_request():
                if not cb_was_open:
                    logger.warning("circuit_breaker_open", worker_id=worker_id)
                    cb_was_open = True
                await asyncio.sleep(1.0)
                continue
            elif cb_was_open:
                logger.info("circuit_breaker_recovered", worker_id=worker_id)
                cb_was_open = False

            # Get next task
            task = await self.scheduler.get_next_task(timeout=1.0)
            if not task:
                continue

            # Execute task
            try:
                if task.action == "_workflow":
                    result = await self._execute_workflow_task(task)
                else:
                    result = await self.task_executor.execute(task)

                # Update circuit breaker
                if result.success:
                    await self.circuit_breaker.record_success()
                else:
                    await self.circuit_breaker.record_failure()
                    await self._handle_failure(task, result.error)

                # Complete task in scheduler
                await self.scheduler.complete_task(
                    task.task_id,
                    result.success,
                    output=result.output,
                    error=result.error.to_dict() if result.error else None,
                )

            except Exception as e:
                logger.exception("worker_error", worker_id=worker_id, task_id=task.task_id)
                await self.circuit_breaker.record_failure()
                await self.scheduler.complete_task(
                    task.task_id,
                    success=False,
                    error={"message": str(e), "type": type(e).__name__},
                )

        logger.debug("worker_stopped", worker_id=worker_id)

    async def _execute_workflow_task(self, task: ScheduledTask) -> ExecutionResult:
        """Execute a workflow task."""
        return await self.workflow_executor.execute_workflow(
            workflow_id=task.payload["workflow_id"],
            steps=task.payload["steps"],
            parameters=task.payload["parameters"],
            on_error=task.payload.get("on_error", "stop"),
        )

    async def _handle_failure(
        self,
        task: ScheduledTask,
        error: Optional[FrameworkError],
    ) -> None:
        """Handle task failure - record, escalate if needed."""
        if not error:
            return

        # Always escalate critical/high severity errors to Telegram
        if self._escalation_handler and error.severity in (ErrorSeverity.CRITICAL, ErrorSeverity.HIGH):
            await self._escalation_handler(
                "task_error",
                {
                    "task_id": task.task_id,
                    "workflow_id": task.workflow_id,
                    "action": task.action,
                    "error": error.message,
                    "severity": error.severity.value,
                    "category": error.category.value if error.category else "unknown",
                },
            )

        # Record failure in persistent store
        quarantine_config = self.config.quarantine
        record = await self.state_manager.record_failure(
            error,
            cooldown_ladder=quarantine_config.cooldown_ladder_seconds,
            max_before_quarantine=quarantine_config.max_failures_before_quarantine,
        )

        if record.quarantined:
            logger.warning(
                "task_quarantined",
                task_id=task.task_id,
                fingerprint=record.fingerprint,
                failure_count=record.count,
            )

            # Escalate
            if self._escalation_handler:
                await self._escalation_handler(
                    "task_quarantined",
                    {
                        "task_id": task.task_id,
                        "fingerprint": record.fingerprint,
                        "error": error.message,
                        "failure_count": record.count,
                    },
                )

    async def _maintenance_loop(self) -> None:
        """Background maintenance tasks."""
        cleanup_interval = self.config.resources.cleanup_interval_seconds

        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(cleanup_interval)

                # Cleanup expired failures
                expired = await self.state_manager.cleanup_expired_failures(
                    self.config.quarantine.auto_reset_after_hours
                )
                if expired > 0:
                    logger.info("expired_failures_cleaned", count=expired)

                # Cleanup old metrics
                await self.state_manager.cleanup_old_metrics(keep_hours=24)

                # Check for config changes and reload
                await self._check_config_changes()

                # Check global failure rate
                await self._check_global_failure_rate()

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("maintenance_error")

    async def _schedule_runner_loop(self) -> None:
        """Background task that checks and runs scheduled rules."""
        import datetime

        logger.info("schedule_runner_started")

        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(30)  # Check every 30 seconds

                if self._state != SupervisorState.RUNNING:
                    continue

                now = datetime.datetime.now()
                current_minute = now.hour * 60 + now.minute

                for rule in self._rules:
                    if not rule.enabled:
                        continue

                    schedule = rule.schedule if hasattr(rule, 'schedule') else None
                    if not schedule:
                        continue

                    # Check if this schedule matches current time
                    if self._schedule_matches(schedule, now):
                        # Avoid running same rule twice in same minute
                        last_run = self._last_schedule_check.get(rule.id, -1)
                        if last_run == current_minute:
                            continue

                        self._last_schedule_check[rule.id] = current_minute

                        logger.info(
                            "scheduled_rule_triggered",
                            rule_id=rule.id,
                            schedule=schedule,
                        )

                        # Execute rule actions
                        for action in rule.actions:
                            action_type = action.get("type") or action.get("action")
                            params = action.get("params", action.get("payload", {}))

                            try:
                                handler = self.task_executor._handlers.get(action_type)
                                if handler:
                                    await handler(params)
                                    logger.info(
                                        "scheduled_action_executed",
                                        rule_id=rule.id,
                                        action=action_type,
                                    )
                                else:
                                    logger.warning(
                                        "scheduled_action_handler_not_found",
                                        rule_id=rule.id,
                                        action=action_type,
                                    )
                            except Exception as e:
                                logger.error(
                                    "scheduled_action_error",
                                    rule_id=rule.id,
                                    action=action_type,
                                    error=str(e),
                                )

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("schedule_runner_error")

        logger.info("schedule_runner_stopped")

    def _schedule_matches(self, schedule: str, dt: 'datetime.datetime') -> bool:
        """Check if cron schedule matches given datetime."""
        try:
            parts = schedule.split()
            if len(parts) != 5:
                return False

            minute, hour, day, month, weekday = parts

            # Check minute
            if minute != '*' and int(minute) != dt.minute:
                return False

            # Check hour
            if hour != '*' and int(hour) != dt.hour:
                return False

            # Check day of month
            if day != '*' and int(day) != dt.day:
                return False

            # Check month
            if month != '*' and int(month) != dt.month:
                return False

            # Check weekday (0=Monday, 6=Sunday in Python; cron uses 0=Sunday)
            if weekday != '*':
                cron_weekday = int(weekday)
                # Convert cron weekday (0=Sunday) to Python (0=Monday)
                python_weekday = (cron_weekday - 1) % 7 if cron_weekday > 0 else 6
                if python_weekday != dt.weekday():
                    return False

            return True
        except Exception:
            return False

    async def _check_restart_safety(self) -> None:
        """
        Check for restart storm conditions on startup.

        Prevents rapid restart loops by checking:
        - Recent failure count
        - Quarantined tasks
        """
        # Check recent failures
        recent_failures = await self.state_manager.get_recent_failures_count(
            window_seconds=300  # Last 5 minutes
        )

        if recent_failures > self.config.safety.global_failure_threshold:
            logger.warning(
                "restart_storm_detected",
                recent_failures=recent_failures,
                threshold=self.config.safety.global_failure_threshold,
            )
            # Start in paused state
            self._state = SupervisorState.PAUSED

        # Log quarantined tasks
        quarantined = await self.state_manager.get_quarantined_tasks()
        if quarantined:
            logger.warning(
                "quarantined_tasks_on_startup",
                count=len(quarantined),
                task_ids=[t.task_id for t in quarantined],
            )

    async def _check_global_failure_rate(self) -> None:
        """Check global failure rate and pause if needed."""
        recent_failures = await self.state_manager.get_recent_failures_count(
            window_seconds=self.config.safety.global_failure_window_seconds
        )

        if recent_failures >= self.config.safety.global_failure_threshold:
            if self._state == SupervisorState.RUNNING:
                logger.warning(
                    "global_failure_threshold_exceeded",
                    failures=recent_failures,
                    threshold=self.config.safety.global_failure_threshold,
                )
                await self.pause()

                # Escalate
                if self._escalation_handler:
                    await self._escalation_handler(
                        "global_failure_threshold",
                        {
                            "failures": recent_failures,
                            "threshold": self.config.safety.global_failure_threshold,
                            "action": "paused",
                        },
                    )

    async def _load_configs(self, raise_on_error: bool = True) -> list[str]:
        """
        Load rules and workflows from config directories.

        Args:
            raise_on_error: If True, raises on config errors. If False, logs errors
                           and continues with empty/partial configs.

        Returns:
            List of error messages (empty if successful)
        """
        errors = []

        # Try loading rules
        try:
            self._rules = self.config_loader.load_rules(
                self.config.rules_directory
            )
        except ConfigError as e:
            error_msg = f"Rules: {e}"
            errors.append(error_msg)
            logger.error("rules_load_error", error=str(e))
            if raise_on_error:
                raise
            self._rules = []

        # Try loading workflows
        try:
            self._workflows = self.config_loader.load_workflows(
                self.config.workflows_directory
            )
        except ConfigError as e:
            error_msg = f"Workflows: {e}"
            errors.append(error_msg)
            logger.error("workflows_load_error", error=str(e))
            if raise_on_error:
                raise
            self._workflows = []

        # Build command lookup
        self._workflow_commands.clear()
        for wf in self._workflows:
            if wf.trigger_command:
                cmd = wf.trigger_command.lstrip("/")
                self._workflow_commands[cmd] = wf

        logger.info(
            "configs_loaded",
            rules=len(self._rules),
            workflows=len(self._workflows),
            commands=list(self._workflow_commands.keys()),
            errors=len(errors),
        )

        return errors

    async def apply_hot_reload(
        self,
        rules: list[RuleDefinition],
        workflows: list[WorkflowDefinition],
    ) -> None:
        """
        Apply hot-reloaded configs without affecting running workflows.

        Called by HotReloader after validation passes.
        """
        # Update rules
        self._rules = rules

        # Update workflows
        self._workflows = workflows

        # Rebuild command lookup
        self._workflow_commands.clear()
        for wf in self._workflows:
            if wf.trigger_command:
                cmd = wf.trigger_command.lstrip("/")
                self._workflow_commands[cmd] = wf

        logger.info(
            "hot_reload_applied",
            rules=len(self._rules),
            workflows=len(self._workflows),
            commands=list(self._workflow_commands.keys()),
        )

    async def _check_config_changes(self) -> None:
        """Check for config file changes and reload if needed."""
        # Check rules directory
        rules_changed = False
        for rule in self._rules:
            # Check if rule config hash changed
            stored_hash = await self.state_manager.get_config_hash(f"rule:{rule.id}")
            if stored_hash != rule.config_hash:
                rules_changed = True
                # Reset failures for this rule (config changed = might be fixed)
                # Note: This is a simplified approach
                logger.info("rule_config_changed", rule_id=rule.id)

        if rules_changed:
            await self._load_configs()

    # ==================== Status & Diagnostics ====================

    async def get_status(self) -> dict[str, Any]:
        """Get supervisor status for diagnostics."""
        queue_stats = await self.scheduler.get_queue_stats() if self.scheduler else {}
        quarantined = await self.state_manager.get_quarantined_tasks() if self.state_manager else []
        pending_approvals = await self.state_manager.get_pending_approvals() if self.state_manager else []

        return {
            "state": self._state.value,
            "uptime_seconds": time.time() - self._start_time if self._start_time else 0,
            "circuit_breaker": self.circuit_breaker.state.value if self.circuit_breaker else "unknown",
            "queue": queue_stats,
            "quarantined_count": len(quarantined),
            "pending_approvals": len(pending_approvals),
            "rules_loaded": len(self._rules),
            "workflows_loaded": len(self._workflows),
        }

    async def reset_failure(self, fingerprint: str) -> bool:
        """Manually reset a failure (remove from quarantine/cooldown)."""
        success = await self.state_manager.reset_failure(fingerprint)
        if success:
            logger.info("failure_reset", fingerprint=fingerprint)
        return success

    async def reset_all_failures(self) -> int:
        """Reset all failures and quarantines."""
        count = await self.state_manager.reset_all_failures()
        logger.info("all_failures_reset", count=count)
        return count
