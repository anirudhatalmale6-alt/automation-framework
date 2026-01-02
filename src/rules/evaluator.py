"""Condition evaluation for rules engine."""

import re
import operator
from typing import Any, Callable, Optional
from dataclasses import dataclass

from core.errors import RuleError


@dataclass
class EvaluationContext:
    """Context for condition evaluation."""
    variables: dict[str, Any]
    state: dict[str, Any]
    trigger_data: Optional[dict[str, Any]] = None


class ConditionEvaluator:
    """
    Evaluates rule conditions.

    Supports:
    - Comparison operators (eq, ne, gt, lt, gte, lte)
    - Logical operators (and, or, not)
    - String matching (contains, startswith, endswith, regex)
    - Existence checks (exists, not_exists)
    - Type checks (is_type)
    - List operations (in, not_in, all, any)
    """

    OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
        "eq": operator.eq,
        "ne": operator.ne,
        "gt": operator.gt,
        "lt": operator.lt,
        "gte": operator.ge,
        "lte": operator.le,
        "contains": lambda a, b: b in a if hasattr(a, "__contains__") else False,
        "startswith": lambda a, b: str(a).startswith(str(b)),
        "endswith": lambda a, b: str(a).endswith(str(b)),
        "in": lambda a, b: a in b if hasattr(b, "__contains__") else False,
        "not_in": lambda a, b: a not in b if hasattr(b, "__contains__") else True,
    }

    def __init__(self):
        self._custom_operators: dict[str, Callable] = {}

    def register_operator(
        self,
        name: str,
        func: Callable[[Any, Any], bool],
    ) -> None:
        """Register a custom operator."""
        self._custom_operators[name] = func

    def evaluate(
        self,
        conditions: list[dict[str, Any]],
        context: EvaluationContext,
    ) -> bool:
        """
        Evaluate a list of conditions (AND by default).

        Returns True if all conditions pass.
        """
        if not conditions:
            return True

        for condition in conditions:
            if not self._evaluate_condition(condition, context):
                return False

        return True

    def _evaluate_condition(
        self,
        condition: dict[str, Any],
        context: EvaluationContext,
    ) -> bool:
        """Evaluate a single condition."""
        # Logical operators
        if "and" in condition:
            return all(
                self._evaluate_condition(c, context)
                for c in condition["and"]
            )

        if "or" in condition:
            return any(
                self._evaluate_condition(c, context)
                for c in condition["or"]
            )

        if "not" in condition:
            return not self._evaluate_condition(condition["not"], context)

        # Field-based condition
        field = condition.get("field")
        if field is None:
            raise RuleError(f"Condition missing 'field': {condition}")

        # Get field value from context
        value = self._get_field_value(field, context)

        # Check operator
        op = condition.get("operator", "eq")

        # Existence checks
        if op == "exists":
            return value is not None

        if op == "not_exists":
            return value is None

        # Type check
        if op == "is_type":
            expected_type = condition.get("value")
            return self._check_type(value, expected_type)

        # Regex match
        if op == "regex":
            pattern = condition.get("value", "")
            try:
                return bool(re.search(pattern, str(value)))
            except re.error as e:
                raise RuleError(f"Invalid regex pattern: {pattern} - {e}")

        # All/any for lists
        if op == "all":
            sub_condition = condition.get("condition", {})
            if not isinstance(value, (list, tuple)):
                return False
            return all(
                self._evaluate_condition(
                    {**sub_condition, "field": "."},
                    EvaluationContext(
                        variables={"item": item, ".": item},
                        state=context.state,
                        trigger_data=context.trigger_data,
                    ),
                )
                for item in value
            )

        if op == "any":
            sub_condition = condition.get("condition", {})
            if not isinstance(value, (list, tuple)):
                return False
            return any(
                self._evaluate_condition(
                    {**sub_condition, "field": "."},
                    EvaluationContext(
                        variables={"item": item, ".": item},
                        state=context.state,
                        trigger_data=context.trigger_data,
                    ),
                )
                for item in value
            )

        # Standard comparison
        expected = condition.get("value")
        expected = self._resolve_value(expected, context)

        # Get operator function
        op_func = self.OPERATORS.get(op) or self._custom_operators.get(op)
        if not op_func:
            raise RuleError(f"Unknown operator: {op}")

        try:
            return op_func(value, expected)
        except Exception as e:
            raise RuleError(f"Error evaluating condition: {e}")

    def _get_field_value(
        self,
        field: str,
        context: EvaluationContext,
    ) -> Any:
        """
        Get field value from context using dot notation.

        Supports:
        - variables.name
        - state.key
        - trigger.field
        - Direct variable reference
        """
        if field == ".":
            return context.variables.get(".")

        parts = field.split(".")
        source = parts[0]

        if source == "variables":
            data = context.variables
            path = parts[1:]
        elif source == "state":
            data = context.state
            path = parts[1:]
        elif source == "trigger":
            data = context.trigger_data or {}
            path = parts[1:]
        else:
            # Try variables first, then trigger
            data = context.variables
            path = parts
            if path[0] not in data and context.trigger_data:
                data = context.trigger_data

        return self._navigate_path(data, path)

    def _navigate_path(self, data: Any, path: list[str]) -> Any:
        """Navigate a dot-separated path in data."""
        current = data
        for part in path:
            if current is None:
                return None

            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, (list, tuple)):
                try:
                    index = int(part)
                    current = current[index] if 0 <= index < len(current) else None
                except ValueError:
                    return None
            elif hasattr(current, part):
                current = getattr(current, part)
            else:
                return None

        return current

    def _resolve_value(self, value: Any, context: EvaluationContext) -> Any:
        """Resolve a value that might be a reference."""
        if isinstance(value, str) and value.startswith("$"):
            # Variable reference
            return self._get_field_value(value[1:], context)
        return value

    def _check_type(self, value: Any, expected_type: str) -> bool:
        """Check if value is of expected type."""
        type_map = {
            "string": str,
            "str": str,
            "int": int,
            "integer": int,
            "float": float,
            "number": (int, float),
            "bool": bool,
            "boolean": bool,
            "list": list,
            "array": list,
            "dict": dict,
            "object": dict,
            "null": type(None),
            "none": type(None),
        }

        expected = type_map.get(expected_type.lower())
        if expected is None:
            raise RuleError(f"Unknown type: {expected_type}")

        return isinstance(value, expected)
