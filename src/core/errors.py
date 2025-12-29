"""Framework error definitions."""

from typing import Optional, Any
from enum import Enum


class ErrorSeverity(Enum):
    """Error severity levels for classification."""
    LOW = "low"           # Transient, retry immediately
    MEDIUM = "medium"     # Retry with backoff
    HIGH = "high"         # Requires cooldown
    CRITICAL = "critical" # Quarantine, escalate


class ErrorCategory(Enum):
    """Error categories for routing and handling."""
    TRANSIENT = "transient"       # Network, timeout - will likely resolve
    PERMANENT = "permanent"       # Config error, invalid selector - won't resolve
    RESOURCE = "resource"         # Memory, CPU - throttle and retry
    EXTERNAL = "external"         # Third-party service issue
    VALIDATION = "validation"     # Input/output validation failure
    SAFETY = "safety"             # Safety rule triggered


class FrameworkError(Exception):
    """Base exception for all framework errors."""

    def __init__(
        self,
        message: str,
        severity: ErrorSeverity = ErrorSeverity.MEDIUM,
        category: ErrorCategory = ErrorCategory.TRANSIENT,
        context: Optional[dict[str, Any]] = None,
        retryable: bool = True,
    ):
        super().__init__(message)
        self.message = message
        self.severity = severity
        self.category = category
        self.context = context or {}
        self.retryable = retryable

    def fingerprint(self) -> str:
        """Generate error fingerprint for deduplication."""
        import hashlib
        components = [
            self.__class__.__name__,
            self.category.value,
            str(self.context.get("task_id", "")),
            str(self.context.get("selector", "")),
        ]
        return hashlib.sha256(":".join(components).encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        """Serialize error for logging/storage."""
        return {
            "type": self.__class__.__name__,
            "message": self.message,
            "severity": self.severity.value,
            "category": self.category.value,
            "context": self.context,
            "retryable": self.retryable,
            "fingerprint": self.fingerprint(),
        }


class ConfigError(FrameworkError):
    """Configuration loading or validation error."""

    def __init__(self, message: str, config_path: Optional[str] = None, **kwargs):
        kwargs.setdefault("severity", ErrorSeverity.HIGH)
        kwargs.setdefault("category", ErrorCategory.PERMANENT)
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)
        self.context["config_path"] = config_path


class RuleError(FrameworkError):
    """Rule parsing or execution error."""

    def __init__(self, message: str, rule_id: Optional[str] = None, **kwargs):
        kwargs.setdefault("severity", ErrorSeverity.MEDIUM)
        kwargs.setdefault("category", ErrorCategory.VALIDATION)
        super().__init__(message, **kwargs)
        self.context["rule_id"] = rule_id


class TaskError(FrameworkError):
    """Task execution error."""

    def __init__(
        self,
        message: str,
        task_id: Optional[str] = None,
        action: Optional[str] = None,
        **kwargs
    ):
        super().__init__(message, **kwargs)
        self.context["task_id"] = task_id
        self.context["action"] = action


class SafetyError(FrameworkError):
    """Safety constraint violation."""

    def __init__(self, message: str, rule: Optional[str] = None, **kwargs):
        kwargs.setdefault("severity", ErrorSeverity.CRITICAL)
        kwargs.setdefault("category", ErrorCategory.SAFETY)
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)
        self.context["safety_rule"] = rule


class BrowserError(FrameworkError):
    """Browser automation error."""

    def __init__(
        self,
        message: str,
        selector: Optional[str] = None,
        url: Optional[str] = None,
        **kwargs
    ):
        super().__init__(message, **kwargs)
        self.context["selector"] = selector
        self.context["url"] = url


class ThrottleError(FrameworkError):
    """Rate limiting or throttle triggered."""

    def __init__(self, message: str, retry_after: Optional[int] = None, **kwargs):
        kwargs.setdefault("severity", ErrorSeverity.LOW)
        kwargs.setdefault("category", ErrorCategory.RESOURCE)
        super().__init__(message, **kwargs)
        self.context["retry_after"] = retry_after


class EscalationError(FrameworkError):
    """Human escalation required."""

    def __init__(self, message: str, escalation_type: str = "approval", **kwargs):
        kwargs.setdefault("severity", ErrorSeverity.HIGH)
        kwargs.setdefault("category", ErrorCategory.SAFETY)
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)
        self.context["escalation_type"] = escalation_type
