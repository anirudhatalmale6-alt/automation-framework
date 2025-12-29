"""Action registry and execution for rules engine."""

import asyncio
import re
from typing import Any, Callable, Optional, Awaitable
from dataclasses import dataclass, field

from ..core.errors import RuleError, TaskError


@dataclass
class ActionResult:
    """Result of an action execution."""
    success: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


# Type alias for action handlers
ActionHandler = Callable[[dict[str, Any]], Awaitable[ActionResult]]


class ActionRegistry:
    """
    Registry for action handlers.

    Actions are config-driven operations that can be:
    - Browser actions (navigate, click, type, etc.)
    - Communication actions (send_message, etc.)
    - Data actions (transform, store, etc.)
    - Control actions (delay, branch, etc.)
    """

    def __init__(self):
        self._handlers: dict[str, ActionHandler] = {}
        self._register_builtin_actions()

    def register(self, action_type: str, handler: ActionHandler) -> None:
        """Register an action handler."""
        self._handlers[action_type] = handler

    def unregister(self, action_type: str) -> None:
        """Unregister an action handler."""
        self._handlers.pop(action_type, None)

    def get_handler(self, action_type: str) -> Optional[ActionHandler]:
        """Get handler for action type."""
        return self._handlers.get(action_type)

    def list_actions(self) -> list[str]:
        """List all registered action types."""
        return list(self._handlers.keys())

    async def execute(
        self,
        action_type: str,
        params: dict[str, Any],
        context: Optional[dict[str, Any]] = None,
    ) -> ActionResult:
        """
        Execute an action.

        Args:
            action_type: Type of action to execute
            params: Action parameters
            context: Execution context (variables, state)

        Returns:
            ActionResult with success status and output
        """
        handler = self._handlers.get(action_type)
        if not handler:
            return ActionResult(
                success=False,
                error=f"Unknown action type: {action_type}",
            )

        # Interpolate variables in params
        if context:
            params = self._interpolate_params(params, context)

        try:
            return await handler(params)
        except Exception as e:
            return ActionResult(
                success=False,
                error=str(e),
            )

    def _interpolate_params(
        self,
        params: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Interpolate {{variable}} references in params."""

        def replace_vars(value: Any) -> Any:
            if isinstance(value, str):
                pattern = r'\{\{([\w.]+)\}\}'
                matches = re.findall(pattern, value)
                for match in matches:
                    var_value = self._get_nested_value(context, match.split("."))
                    if var_value is not None:
                        if value == f"{{{{{match}}}}}":
                            return var_value
                        value = value.replace(f"{{{{{match}}}}}", str(var_value))
                return value
            elif isinstance(value, dict):
                return {k: replace_vars(v) for k, v in value.items()}
            elif isinstance(value, list):
                return [replace_vars(v) for v in value]
            return value

        return replace_vars(params)

    def _get_nested_value(self, data: dict, path: list[str]) -> Any:
        """Get nested value from dict using path."""
        current = data
        for key in path:
            if isinstance(current, dict):
                current = current.get(key)
            else:
                return None
            if current is None:
                return None
        return current

    def _register_builtin_actions(self) -> None:
        """Register built-in actions."""

        # Control flow actions
        self.register("delay", self._action_delay)
        self.register("log", self._action_log)
        self.register("set_variable", self._action_set_variable)
        self.register("branch", self._action_branch)
        self.register("noop", self._action_noop)

    async def _action_delay(self, params: dict[str, Any]) -> ActionResult:
        """Delay execution for specified seconds."""
        seconds = params.get("seconds", 1)
        try:
            await asyncio.sleep(float(seconds))
            return ActionResult(success=True, output={"delayed_seconds": seconds})
        except Exception as e:
            return ActionResult(success=False, error=str(e))

    async def _action_log(self, params: dict[str, Any]) -> ActionResult:
        """Log a message (for debugging/audit)."""
        message = params.get("message", "")
        level = params.get("level", "info")
        # In production, this would use proper logging
        return ActionResult(
            success=True,
            output={"logged": True, "message": message, "level": level},
        )

    async def _action_set_variable(self, params: dict[str, Any]) -> ActionResult:
        """Set a variable value (output becomes available to next steps)."""
        name = params.get("name")
        value = params.get("value")
        if not name:
            return ActionResult(success=False, error="Variable name required")

        return ActionResult(
            success=True,
            output={name: value},
        )

    async def _action_branch(self, params: dict[str, Any]) -> ActionResult:
        """Branch action - returns which branch to take."""
        condition = params.get("condition", True)
        true_branch = params.get("true_branch", "continue")
        false_branch = params.get("false_branch", "continue")

        branch = true_branch if condition else false_branch
        return ActionResult(
            success=True,
            output={"branch": branch, "condition_result": bool(condition)},
        )

    async def _action_noop(self, params: dict[str, Any]) -> ActionResult:
        """No operation - useful for testing."""
        return ActionResult(success=True, output={"action": "noop"})
