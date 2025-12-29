"""Task executor with retry logic and error handling."""

import asyncio
import time
import random
from typing import Any, Callable, Optional, Awaitable
from dataclasses import dataclass

from ..core.config import RetryConfig, FrameworkConfig
from ..core.state import StateManager, TaskStatus
from ..core.errors import (
    FrameworkError,
    TaskError,
    SafetyError,
    ErrorSeverity,
    ErrorCategory,
)
from .scheduler import ScheduledTask


@dataclass
class ExecutionResult:
    """Result of task execution."""
    success: bool
    output: Optional[dict] = None
    error: Optional[FrameworkError] = None
    duration_ms: float = 0
    attempts: int = 1


# Type alias for action handlers
ActionHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class TaskExecutor:
    """
    Executes tasks with retry logic, timeout handling, and error classification.

    Features:
    - Exponential backoff with jitter
    - Error classification (transient vs permanent)
    - Task timeout enforcement
    - Loop detection
    """

    def __init__(
        self,
        config: FrameworkConfig,
        state_manager: StateManager,
    ):
        self.config = config
        self.state = state_manager
        self.retry_config = config.retry
        self.safety_config = config.safety

        # Action handlers registry
        self._handlers: dict[str, ActionHandler] = {}

        # Execution tracking for loop detection
        self._execution_history: list[tuple[str, float]] = []
        self._history_lock = asyncio.Lock()

    def register_handler(self, action: str, handler: ActionHandler) -> None:
        """Register a handler for an action type."""
        self._handlers[action] = handler

    def unregister_handler(self, action: str) -> None:
        """Unregister a handler."""
        self._handlers.pop(action, None)

    async def execute(
        self,
        task: ScheduledTask,
        retry_config: Optional[RetryConfig] = None,
    ) -> ExecutionResult:
        """
        Execute a task with retry logic.

        Args:
            task: The task to execute
            retry_config: Override retry configuration

        Returns:
            ExecutionResult with success status, output, and any error
        """
        config = retry_config or self.retry_config
        start_time = time.monotonic()
        attempts = 0
        last_error: Optional[FrameworkError] = None

        # Check for handler
        handler = self._handlers.get(task.action)
        if not handler:
            error = TaskError(
                f"No handler registered for action: {task.action}",
                task_id=task.task_id,
                action=task.action,
                severity=ErrorSeverity.HIGH,
                category=ErrorCategory.PERMANENT,
                retryable=False,
            )
            return ExecutionResult(
                success=False,
                error=error,
                duration_ms=(time.monotonic() - start_time) * 1000,
                attempts=1,
            )

        # Loop detection check
        if await self._detect_loop(task):
            error = SafetyError(
                f"Loop detected for task {task.task_id}",
                rule="loop_detection",
            )
            return ExecutionResult(
                success=False,
                error=error,
                duration_ms=(time.monotonic() - start_time) * 1000,
                attempts=0,
            )

        while attempts < config.max_attempts:
            attempts += 1

            try:
                # Execute with timeout
                output = await asyncio.wait_for(
                    handler(task.payload),
                    timeout=self.safety_config.max_task_runtime_seconds,
                )

                # Record successful execution
                await self._record_execution(task.task_id, success=True)

                return ExecutionResult(
                    success=True,
                    output=output,
                    duration_ms=(time.monotonic() - start_time) * 1000,
                    attempts=attempts,
                )

            except asyncio.TimeoutError:
                last_error = TaskError(
                    f"Task timed out after {self.safety_config.max_task_runtime_seconds}s",
                    task_id=task.task_id,
                    action=task.action,
                    severity=ErrorSeverity.MEDIUM,
                    category=ErrorCategory.RESOURCE,
                )

            except FrameworkError as e:
                last_error = e
                e.context["task_id"] = task.task_id
                e.context["action"] = task.action
                e.context["attempt"] = attempts

                # Don't retry non-retryable errors
                if not e.retryable:
                    break

            except Exception as e:
                last_error = TaskError(
                    str(e),
                    task_id=task.task_id,
                    action=task.action,
                    severity=ErrorSeverity.MEDIUM,
                    category=ErrorCategory.TRANSIENT,
                )

            # Record failed attempt
            await self._record_execution(task.task_id, success=False)

            # Calculate backoff delay
            if attempts < config.max_attempts and (last_error is None or last_error.retryable):
                delay = self._calculate_backoff(attempts, config)
                await asyncio.sleep(delay)

        return ExecutionResult(
            success=False,
            error=last_error,
            duration_ms=(time.monotonic() - start_time) * 1000,
            attempts=attempts,
        )

    def _calculate_backoff(self, attempt: int, config: RetryConfig) -> float:
        """Calculate backoff delay with exponential backoff and optional jitter."""
        delay = config.base_delay_seconds * (config.exponential_base ** (attempt - 1))
        delay = min(delay, config.max_delay_seconds)

        if config.jitter:
            # Add random jitter: 0.5x to 1.5x the delay
            jitter_factor = 0.5 + random.random()
            delay *= jitter_factor

        return delay

    async def _detect_loop(self, task: ScheduledTask) -> bool:
        """
        Detect if we're in an execution loop.

        Returns True if the same task/action has been attempted too many times
        in a short window.
        """
        async with self._history_lock:
            now = time.time()
            window_seconds = 60.0  # 1 minute window

            # Clean old entries
            self._execution_history = [
                (tid, ts) for tid, ts in self._execution_history
                if now - ts < window_seconds
            ]

            # Count executions of this task pattern
            pattern = f"{task.action}:{task.rule_id or 'none'}"
            count = sum(1 for tid, _ in self._execution_history if tid == pattern)

            if count >= self.safety_config.max_loop_iterations:
                return True

            # Record this execution
            self._execution_history.append((pattern, now))
            return False

    async def _record_execution(self, task_id: str, success: bool) -> None:
        """Record execution for metrics and loop detection."""
        metric_name = "task_execution_success" if success else "task_execution_failure"
        await self.state.record_metric(
            name=metric_name,
            value=1,
            labels={"task_id": task_id},
        )


class WorkflowExecutor:
    """
    Executes multi-step workflows.

    Features:
    - Sequential step execution
    - Variable passing between steps
    - Rollback support
    - Step-level error handling
    """

    def __init__(
        self,
        task_executor: TaskExecutor,
        state_manager: StateManager,
        config: FrameworkConfig,
    ):
        self.task_executor = task_executor
        self.state = state_manager
        self.config = config

    async def execute_workflow(
        self,
        workflow_id: str,
        steps: list[dict[str, Any]],
        parameters: dict[str, Any],
        on_error: str = "stop",
    ) -> ExecutionResult:
        """
        Execute a workflow (sequence of steps).

        Args:
            workflow_id: Workflow identifier
            steps: List of step definitions
            parameters: Initial parameters/variables
            on_error: Error handling mode ("stop", "continue", "rollback")

        Returns:
            ExecutionResult for the overall workflow
        """
        start_time = time.monotonic()
        variables = dict(parameters)
        executed_steps: list[dict] = []
        step_outputs: list[dict] = []

        for i, step in enumerate(steps):
            step_id = step.get("id", f"step_{i}")
            action = step.get("action")
            payload = step.get("payload", {})

            # Interpolate variables in payload
            payload = self._interpolate_variables(payload, variables)

            # Create task for this step
            task = ScheduledTask(
                priority=0,
                created_at=time.time(),
                task_id=f"{workflow_id}_{step_id}",
                workflow_id=workflow_id,
                action=action,
                payload=payload,
            )

            # Execute step
            result = await self.task_executor.execute(task)
            executed_steps.append(step)

            if result.success:
                step_outputs.append(result.output or {})
                # Update variables with step output
                if result.output:
                    variables.update(result.output)
                    variables[f"step_{i}_output"] = result.output
            else:
                if on_error == "stop":
                    return ExecutionResult(
                        success=False,
                        error=result.error,
                        output={"completed_steps": i, "step_outputs": step_outputs},
                        duration_ms=(time.monotonic() - start_time) * 1000,
                        attempts=1,
                    )
                elif on_error == "rollback":
                    await self._rollback(executed_steps, variables)
                    return ExecutionResult(
                        success=False,
                        error=result.error,
                        output={"completed_steps": i, "rolled_back": True},
                        duration_ms=(time.monotonic() - start_time) * 1000,
                        attempts=1,
                    )
                # on_error == "continue": keep going

        return ExecutionResult(
            success=True,
            output={"completed_steps": len(steps), "step_outputs": step_outputs, "variables": variables},
            duration_ms=(time.monotonic() - start_time) * 1000,
            attempts=1,
        )

    def _interpolate_variables(
        self,
        payload: dict[str, Any],
        variables: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Interpolate variables in payload.

        Supports {{variable_name}} syntax.
        """
        import re

        def replace_vars(value: Any) -> Any:
            if isinstance(value, str):
                pattern = r'\{\{(\w+)\}\}'
                matches = re.findall(pattern, value)
                for match in matches:
                    if match in variables:
                        var_value = variables[match]
                        if value == f"{{{{{match}}}}}":
                            # Entire value is a variable reference
                            return var_value
                        else:
                            # Partial substitution
                            value = value.replace(f"{{{{{match}}}}}", str(var_value))
                return value
            elif isinstance(value, dict):
                return {k: replace_vars(v) for k, v in value.items()}
            elif isinstance(value, list):
                return [replace_vars(v) for v in value]
            return value

        return replace_vars(payload)

    async def _rollback(
        self,
        executed_steps: list[dict],
        variables: dict[str, Any],
    ) -> None:
        """
        Execute rollback actions for steps that have them.

        Rollback is executed in reverse order.
        """
        for step in reversed(executed_steps):
            rollback_action = step.get("rollback_action")
            if rollback_action:
                rollback_payload = step.get("rollback_payload", {})
                rollback_payload = self._interpolate_variables(rollback_payload, variables)

                task = ScheduledTask(
                    priority=0,
                    created_at=time.time(),
                    task_id=f"rollback_{step.get('id', 'unknown')}",
                    action=rollback_action,
                    payload=rollback_payload,
                )

                # Execute rollback (ignore errors during rollback)
                await self.task_executor.execute(task)
