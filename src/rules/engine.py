"""Rules engine - evaluates triggers, conditions, and executes actions."""

import asyncio
import time
from typing import Any, Optional, Callable, Awaitable
from dataclasses import dataclass
import structlog

from ..core.config import RuleDefinition, ConfigLoader
from ..core.state import StateManager
from ..core.errors import RuleError, SafetyError
from .evaluator import ConditionEvaluator, EvaluationContext
from .actions import ActionRegistry, ActionResult


logger = structlog.get_logger()


@dataclass
class TriggerEvent:
    """An event that can trigger rules."""
    type: str                    # Event type (e.g., "command", "schedule", "webhook")
    data: dict[str, Any]         # Event data
    source: Optional[str] = None # Event source identifier
    timestamp: float = 0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()


@dataclass
class RuleExecutionResult:
    """Result of rule execution."""
    rule_id: str
    triggered: bool
    conditions_met: bool
    actions_executed: int
    actions_succeeded: int
    output: dict[str, Any]
    error: Optional[str] = None
    duration_ms: float = 0


class RulesEngine:
    """
    Rules engine that evaluates and executes config-driven rules.

    Flow:
    1. Receive trigger event
    2. Find matching rules (by trigger type)
    3. Evaluate conditions
    4. Execute actions
    5. Return results

    Features:
    - Priority-based rule ordering
    - Condition evaluation with operators
    - Action execution with variable passing
    - Rate limiting per rule
    - Approval requirements
    """

    def __init__(
        self,
        state_manager: StateManager,
        config_loader: Optional[ConfigLoader] = None,
    ):
        self.state = state_manager
        self.config_loader = config_loader

        # Components
        self.evaluator = ConditionEvaluator()
        self.action_registry = ActionRegistry()

        # Loaded rules
        self._rules: list[RuleDefinition] = []
        self._rules_by_trigger: dict[str, list[RuleDefinition]] = {}

        # Execution tracking (for rate limiting)
        self._execution_counts: dict[str, list[float]] = {}

        # Approval callback
        self._approval_handler: Optional[Callable[[str, dict], Awaitable[bool]]] = None

    def load_rules(self, rules: list[RuleDefinition]) -> None:
        """Load rules into the engine."""
        self._rules = sorted(rules, key=lambda r: r.priority, reverse=True)
        self._rules_by_trigger.clear()

        for rule in self._rules:
            if not rule.enabled:
                continue

            for trigger in rule.triggers:
                trigger_type = trigger.get("type", "any")
                if trigger_type not in self._rules_by_trigger:
                    self._rules_by_trigger[trigger_type] = []
                self._rules_by_trigger[trigger_type].append(rule)

        logger.info(
            "rules_loaded",
            total=len(self._rules),
            enabled=sum(1 for r in self._rules if r.enabled),
            trigger_types=list(self._rules_by_trigger.keys()),
        )

    def set_approval_handler(
        self,
        handler: Callable[[str, dict], Awaitable[bool]],
    ) -> None:
        """Set callback for approval requests."""
        self._approval_handler = handler

    def register_action(self, action_type: str, handler: Callable) -> None:
        """Register a custom action handler."""
        self.action_registry.register(action_type, handler)

    async def process_event(
        self,
        event: TriggerEvent,
        context: Optional[dict[str, Any]] = None,
    ) -> list[RuleExecutionResult]:
        """
        Process a trigger event through all matching rules.

        Args:
            event: The trigger event
            context: Additional context (variables, state)

        Returns:
            List of execution results for each triggered rule
        """
        results = []
        context = context or {}

        # Get rules matching this trigger type
        matching_rules = self._get_matching_rules(event)

        for rule in matching_rules:
            try:
                result = await self._execute_rule(rule, event, context)
                results.append(result)

                # Stop on first match if rule indicates
                if result.triggered and result.conditions_met:
                    # Check for stop_on_match in rule config
                    # (could be extended to support this)
                    pass

            except Exception as e:
                logger.exception("rule_execution_error", rule_id=rule.id)
                results.append(RuleExecutionResult(
                    rule_id=rule.id,
                    triggered=True,
                    conditions_met=False,
                    actions_executed=0,
                    actions_succeeded=0,
                    output={},
                    error=str(e),
                ))

        return results

    def _get_matching_rules(self, event: TriggerEvent) -> list[RuleDefinition]:
        """Get rules that match the trigger event type."""
        rules = []

        # Exact match
        if event.type in self._rules_by_trigger:
            rules.extend(self._rules_by_trigger[event.type])

        # Wildcard match
        if "any" in self._rules_by_trigger:
            rules.extend(self._rules_by_trigger["any"])

        # Deduplicate while preserving order
        seen = set()
        unique_rules = []
        for rule in rules:
            if rule.id not in seen:
                seen.add(rule.id)
                unique_rules.append(rule)

        return unique_rules

    async def _execute_rule(
        self,
        rule: RuleDefinition,
        event: TriggerEvent,
        context: dict[str, Any],
    ) -> RuleExecutionResult:
        """Execute a single rule."""
        start_time = time.monotonic()

        # Check rate limit
        if not self._check_rate_limit(rule):
            return RuleExecutionResult(
                rule_id=rule.id,
                triggered=True,
                conditions_met=False,
                actions_executed=0,
                actions_succeeded=0,
                output={},
                error="Rate limit exceeded",
                duration_ms=(time.monotonic() - start_time) * 1000,
            )

        # Check trigger conditions
        if not self._matches_trigger(rule, event):
            return RuleExecutionResult(
                rule_id=rule.id,
                triggered=False,
                conditions_met=False,
                actions_executed=0,
                actions_succeeded=0,
                output={},
                duration_ms=(time.monotonic() - start_time) * 1000,
            )

        # Build evaluation context
        eval_context = EvaluationContext(
            variables=context.get("variables", {}),
            state=context.get("state", {}),
            trigger_data=event.data,
        )

        # Evaluate conditions
        conditions_met = self.evaluator.evaluate(rule.conditions, eval_context)

        if not conditions_met:
            return RuleExecutionResult(
                rule_id=rule.id,
                triggered=True,
                conditions_met=False,
                actions_executed=0,
                actions_succeeded=0,
                output={},
                duration_ms=(time.monotonic() - start_time) * 1000,
            )

        # Check approval requirement
        if rule.requires_approval:
            approved = await self._request_approval(rule, event, context)
            if not approved:
                return RuleExecutionResult(
                    rule_id=rule.id,
                    triggered=True,
                    conditions_met=True,
                    actions_executed=0,
                    actions_succeeded=0,
                    output={},
                    error="Approval denied or pending",
                    duration_ms=(time.monotonic() - start_time) * 1000,
                )

        # Execute actions
        actions_executed = 0
        actions_succeeded = 0
        accumulated_output = dict(context.get("variables", {}))
        accumulated_output.update(event.data)

        for action in rule.actions:
            action_type = action.get("type") or action.get("action")
            params = action.get("params", action.get("payload", {}))

            # Execute action
            result = await self.action_registry.execute(
                action_type,
                params,
                context={"variables": accumulated_output},
            )

            actions_executed += 1
            if result.success:
                actions_succeeded += 1
                accumulated_output.update(result.output)
            else:
                logger.warning(
                    "action_failed",
                    rule_id=rule.id,
                    action_type=action_type,
                    error=result.error,
                )
                # Continue or stop based on action config
                if action.get("stop_on_failure", False):
                    break

        # Record execution for rate limiting
        self._record_execution(rule.id)

        return RuleExecutionResult(
            rule_id=rule.id,
            triggered=True,
            conditions_met=True,
            actions_executed=actions_executed,
            actions_succeeded=actions_succeeded,
            output=accumulated_output,
            duration_ms=(time.monotonic() - start_time) * 1000,
        )

    def _matches_trigger(self, rule: RuleDefinition, event: TriggerEvent) -> bool:
        """Check if event matches rule's trigger definitions."""
        for trigger in rule.triggers:
            trigger_type = trigger.get("type", "any")

            # Type must match (or be "any")
            if trigger_type != "any" and trigger_type != event.type:
                continue

            # Check trigger-specific conditions
            trigger_conditions = trigger.get("conditions", {})
            if not trigger_conditions:
                return True

            # Simple field matching
            for field, expected in trigger_conditions.items():
                actual = event.data.get(field)
                if actual != expected:
                    break
            else:
                return True

        return False

    def _check_rate_limit(self, rule: RuleDefinition) -> bool:
        """Check if rule is within its rate limit."""
        if not rule.max_executions_per_hour:
            return True

        now = time.time()
        hour_ago = now - 3600

        # Get executions in last hour
        executions = self._execution_counts.get(rule.id, [])
        recent = [t for t in executions if t > hour_ago]

        return len(recent) < rule.max_executions_per_hour

    def _record_execution(self, rule_id: str) -> None:
        """Record rule execution for rate limiting."""
        now = time.time()
        if rule_id not in self._execution_counts:
            self._execution_counts[rule_id] = []

        self._execution_counts[rule_id].append(now)

        # Cleanup old entries
        hour_ago = now - 3600
        self._execution_counts[rule_id] = [
            t for t in self._execution_counts[rule_id]
            if t > hour_ago
        ]

    async def _request_approval(
        self,
        rule: RuleDefinition,
        event: TriggerEvent,
        context: dict[str, Any],
    ) -> bool:
        """Request approval for rule execution."""
        if not self._approval_handler:
            logger.warning(
                "approval_required_no_handler",
                rule_id=rule.id,
            )
            return False

        try:
            return await self._approval_handler(
                rule.id,
                {
                    "rule_name": rule.name,
                    "description": rule.description,
                    "event_type": event.type,
                    "event_data": event.data,
                },
            )
        except Exception as e:
            logger.exception("approval_handler_error", rule_id=rule.id)
            return False

    # ==================== Utility Methods ====================

    def get_rule(self, rule_id: str) -> Optional[RuleDefinition]:
        """Get a rule by ID."""
        return next((r for r in self._rules if r.id == rule_id), None)

    def list_rules(
        self,
        enabled_only: bool = True,
        tags: Optional[list[str]] = None,
    ) -> list[RuleDefinition]:
        """List rules with optional filtering."""
        rules = self._rules

        if enabled_only:
            rules = [r for r in rules if r.enabled]

        if tags:
            rules = [r for r in rules if any(t in r.tags for t in tags)]

        return rules

    def get_execution_stats(self, rule_id: str) -> dict[str, Any]:
        """Get execution statistics for a rule."""
        executions = self._execution_counts.get(rule_id, [])
        now = time.time()
        hour_ago = now - 3600

        recent = [t for t in executions if t > hour_ago]

        return {
            "rule_id": rule_id,
            "executions_last_hour": len(recent),
            "last_execution": max(executions) if executions else None,
        }
