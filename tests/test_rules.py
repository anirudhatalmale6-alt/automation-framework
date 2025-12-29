"""Tests for rules engine."""

import asyncio
import pytest
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from rules.evaluator import ConditionEvaluator, EvaluationContext
from rules.actions import ActionRegistry, ActionResult
from rules.engine import RulesEngine, TriggerEvent
from core.config import RuleDefinition


class TestConditionEvaluator:
    """Test condition evaluation."""

    @pytest.fixture
    def evaluator(self):
        return ConditionEvaluator()

    def test_eq_operator(self, evaluator):
        """Test equality operator."""
        context = EvaluationContext(
            variables={"status": "active"},
            state={},
        )

        conditions = [{"field": "status", "operator": "eq", "value": "active"}]
        assert evaluator.evaluate(conditions, context) is True

        conditions = [{"field": "status", "operator": "eq", "value": "inactive"}]
        assert evaluator.evaluate(conditions, context) is False

    def test_comparison_operators(self, evaluator):
        """Test numeric comparison operators."""
        context = EvaluationContext(
            variables={"count": 50},
            state={},
        )

        assert evaluator.evaluate([{"field": "count", "operator": "gt", "value": 40}], context)
        assert evaluator.evaluate([{"field": "count", "operator": "lt", "value": 60}], context)
        assert evaluator.evaluate([{"field": "count", "operator": "gte", "value": 50}], context)
        assert evaluator.evaluate([{"field": "count", "operator": "lte", "value": 50}], context)
        assert not evaluator.evaluate([{"field": "count", "operator": "gt", "value": 50}], context)

    def test_string_operators(self, evaluator):
        """Test string operators."""
        context = EvaluationContext(
            variables={"url": "https://example.com/page"},
            state={},
        )

        assert evaluator.evaluate(
            [{"field": "url", "operator": "startswith", "value": "https://"}],
            context
        )
        assert evaluator.evaluate(
            [{"field": "url", "operator": "endswith", "value": "/page"}],
            context
        )
        assert evaluator.evaluate(
            [{"field": "url", "operator": "contains", "value": "example"}],
            context
        )

    def test_regex_operator(self, evaluator):
        """Test regex matching."""
        context = EvaluationContext(
            variables={"email": "test@example.com"},
            state={},
        )

        assert evaluator.evaluate(
            [{"field": "email", "operator": "regex", "value": r"^\w+@\w+\.\w+$"}],
            context
        )

    def test_existence_operators(self, evaluator):
        """Test exists/not_exists operators."""
        context = EvaluationContext(
            variables={"present": "value"},
            state={},
        )

        assert evaluator.evaluate([{"field": "present", "operator": "exists"}], context)
        assert evaluator.evaluate([{"field": "missing", "operator": "not_exists"}], context)
        assert not evaluator.evaluate([{"field": "missing", "operator": "exists"}], context)

    def test_in_operator(self, evaluator):
        """Test in/not_in operators."""
        context = EvaluationContext(
            variables={"status": "pending"},
            state={},
        )

        assert evaluator.evaluate(
            [{"field": "status", "operator": "in", "value": ["pending", "active", "done"]}],
            context
        )
        assert evaluator.evaluate(
            [{"field": "status", "operator": "not_in", "value": ["error", "cancelled"]}],
            context
        )

    def test_and_conditions(self, evaluator):
        """Test AND conditions (default)."""
        context = EvaluationContext(
            variables={"a": 1, "b": 2},
            state={},
        )

        # All must pass
        conditions = [
            {"field": "a", "operator": "eq", "value": 1},
            {"field": "b", "operator": "eq", "value": 2},
        ]
        assert evaluator.evaluate(conditions, context)

        # One fails = all fails
        conditions = [
            {"field": "a", "operator": "eq", "value": 1},
            {"field": "b", "operator": "eq", "value": 999},
        ]
        assert not evaluator.evaluate(conditions, context)

    def test_or_conditions(self, evaluator):
        """Test OR conditions."""
        context = EvaluationContext(
            variables={"status": "error"},
            state={},
        )

        conditions = [{
            "or": [
                {"field": "status", "operator": "eq", "value": "ready"},
                {"field": "status", "operator": "eq", "value": "error"},
            ]
        }]
        assert evaluator.evaluate(conditions, context)

    def test_not_conditions(self, evaluator):
        """Test NOT conditions."""
        context = EvaluationContext(
            variables={"disabled": False},
            state={},
        )

        conditions = [{
            "not": {"field": "disabled", "operator": "eq", "value": True}
        }]
        assert evaluator.evaluate(conditions, context)

    def test_nested_conditions(self, evaluator):
        """Test nested logical operators."""
        context = EvaluationContext(
            variables={"count": 50, "override": False},
            state={},
        )

        # (count > 10 AND count < 100) OR override
        conditions = [{
            "or": [
                {
                    "and": [
                        {"field": "count", "operator": "gt", "value": 10},
                        {"field": "count", "operator": "lt", "value": 100},
                    ]
                },
                {"field": "override", "operator": "eq", "value": True},
            ]
        }]
        assert evaluator.evaluate(conditions, context)

    def test_trigger_data_access(self, evaluator):
        """Test accessing trigger data in conditions."""
        context = EvaluationContext(
            variables={},
            state={},
            trigger_data={"user_id": 123, "action": "create"},
        )

        conditions = [
            {"field": "trigger.user_id", "operator": "eq", "value": 123},
            {"field": "trigger.action", "operator": "eq", "value": "create"},
        ]
        assert evaluator.evaluate(conditions, context)

    def test_type_check_operator(self, evaluator):
        """Test is_type operator."""
        context = EvaluationContext(
            variables={
                "text": "hello",
                "number": 42,
                "items": [1, 2, 3],
                "data": {"key": "value"},
            },
            state={},
        )

        assert evaluator.evaluate([{"field": "text", "operator": "is_type", "value": "string"}], context)
        assert evaluator.evaluate([{"field": "number", "operator": "is_type", "value": "integer"}], context)
        assert evaluator.evaluate([{"field": "items", "operator": "is_type", "value": "list"}], context)
        assert evaluator.evaluate([{"field": "data", "operator": "is_type", "value": "object"}], context)


class TestActionRegistry:
    """Test action registration and execution."""

    @pytest.fixture
    def registry(self):
        return ActionRegistry()

    @pytest.mark.asyncio
    async def test_builtin_delay_action(self, registry):
        """Test built-in delay action."""
        result = await registry.execute("delay", {"seconds": 0.1})
        assert result.success
        assert result.output["delayed_seconds"] == 0.1

    @pytest.mark.asyncio
    async def test_builtin_log_action(self, registry):
        """Test built-in log action."""
        result = await registry.execute("log", {"message": "test", "level": "info"})
        assert result.success
        assert result.output["logged"] is True

    @pytest.mark.asyncio
    async def test_builtin_set_variable_action(self, registry):
        """Test built-in set_variable action."""
        result = await registry.execute("set_variable", {"name": "my_var", "value": 42})
        assert result.success
        assert result.output["my_var"] == 42

    @pytest.mark.asyncio
    async def test_custom_action_registration(self, registry):
        """Test custom action registration."""

        async def custom_handler(params):
            return ActionResult(
                success=True,
                output={"custom": params.get("input", "default")},
            )

        registry.register("custom_action", custom_handler)

        result = await registry.execute("custom_action", {"input": "test_value"})
        assert result.success
        assert result.output["custom"] == "test_value"

    @pytest.mark.asyncio
    async def test_unknown_action(self, registry):
        """Test handling of unknown action."""
        result = await registry.execute("nonexistent_action", {})
        assert not result.success
        assert "Unknown action" in result.error

    @pytest.mark.asyncio
    async def test_variable_interpolation(self, registry):
        """Test variable interpolation in params."""
        result = await registry.execute(
            "log",
            {"message": "User {{user_id}} performed {{action}}"},
            context={"variables": {"user_id": 123, "action": "login"}},
        )
        assert result.success
        assert result.output["message"] == "User 123 performed login"


class TestRulesEngine:
    """Test rules engine integration."""

    @pytest.fixture
    def sample_rule(self):
        return RuleDefinition(
            id="test_rule",
            name="Test Rule",
            enabled=True,
            priority=10,
            triggers=[{"type": "command", "conditions": {"command": "/test"}}],
            conditions=[
                {"field": "trigger.confirmed", "operator": "eq", "value": True}
            ],
            actions=[
                {"type": "log", "params": {"message": "Test executed"}},
                {"type": "set_variable", "params": {"name": "result", "value": "success"}},
            ],
        )

    @pytest.mark.asyncio
    async def test_rule_matching(self, sample_rule):
        """Test rule trigger matching."""
        # Would need StateManager mock for full test
        # This is a structural test
        assert sample_rule.id == "test_rule"
        assert len(sample_rule.triggers) == 1
        assert sample_rule.triggers[0]["type"] == "command"

    @pytest.mark.asyncio
    async def test_rule_priority_ordering(self):
        """Test rules are ordered by priority."""
        rules = [
            RuleDefinition(id="low", name="Low", priority=1, triggers=[], conditions=[], actions=[]),
            RuleDefinition(id="high", name="High", priority=100, triggers=[], conditions=[], actions=[]),
            RuleDefinition(id="medium", name="Medium", priority=50, triggers=[], conditions=[], actions=[]),
        ]

        sorted_rules = sorted(rules, key=lambda r: r.priority, reverse=True)
        assert sorted_rules[0].id == "high"
        assert sorted_rules[1].id == "medium"
        assert sorted_rules[2].id == "low"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
