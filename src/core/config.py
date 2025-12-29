"""Configuration loading and validation."""

import os
import json
import hashlib
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass, field

import yaml
from pydantic import BaseModel, Field, field_validator
import jsonschema

from .errors import ConfigError


class ThrottleConfig(BaseModel):
    """Throttling configuration."""
    max_concurrent_tasks: int = Field(default=5, ge=1, le=50)
    max_tasks_per_minute: int = Field(default=30, ge=1, le=1000)
    max_browser_actions_per_minute: int = Field(default=60, ge=1, le=500)
    cooldown_on_error_seconds: int = Field(default=30, ge=0)


class RetryConfig(BaseModel):
    """Retry policy configuration."""
    max_attempts: int = Field(default=3, ge=1, le=10)
    base_delay_seconds: float = Field(default=1.0, ge=0.1)
    max_delay_seconds: float = Field(default=300.0, ge=1.0)
    exponential_base: float = Field(default=2.0, ge=1.1, le=5.0)
    jitter: bool = Field(default=True)


class CircuitBreakerConfig(BaseModel):
    """Circuit breaker configuration."""
    failure_threshold: int = Field(default=5, ge=1, le=100)
    recovery_timeout_seconds: int = Field(default=60, ge=10)
    half_open_max_calls: int = Field(default=3, ge=1)


class QuarantineConfig(BaseModel):
    """Quarantine settings for persistent failures."""
    max_failures_before_quarantine: int = Field(default=6, ge=2, le=20)
    cooldown_ladder_seconds: list[int] = Field(
        default=[1, 5, 15, 300, 1800]  # 1s, 5s, 15s, 5min, 30min
    )
    auto_reset_after_hours: int = Field(default=24, ge=1)


class SafetyConfig(BaseModel):
    """Safety and anti-loop configuration."""
    max_loop_iterations: int = Field(default=100, ge=10)
    max_task_runtime_seconds: int = Field(default=300, ge=30)
    global_failure_threshold: int = Field(default=10, ge=3)
    global_failure_window_seconds: int = Field(default=300, ge=60)
    require_approval_for_destructive: bool = Field(default=True)


class ResourceConfig(BaseModel):
    """Resource limits for low-hardware environments."""
    max_memory_mb: int = Field(default=4096, ge=512)
    max_browser_pages: int = Field(default=1, ge=1, le=5)
    cleanup_interval_seconds: int = Field(default=60, ge=10)
    gc_threshold_mb: int = Field(default=3072, ge=256)


class TelegramConfig(BaseModel):
    """Telegram bot configuration."""
    enabled: bool = Field(default=False)
    bot_token: Optional[str] = Field(default=None)
    allowed_user_ids: list[int] = Field(default_factory=list)
    typing_delay_ms: int = Field(default=500, ge=0, le=5000)
    message_chunk_size: int = Field(default=4000, ge=100)


class VoiceConfig(BaseModel):
    """Voice interface abstraction configuration."""
    enabled: bool = Field(default=False)
    provider: Optional[str] = Field(default=None)  # Abstract - no lock-in
    config: dict[str, Any] = Field(default_factory=dict)


class LLMConfig(BaseModel):
    """Local LLM configuration."""
    enabled: bool = Field(default=False)
    provider: str = Field(default="ollama")
    base_url: str = Field(default="http://localhost:11434")
    model: str = Field(default="llama3.2:3b")
    max_tokens: int = Field(default=500, ge=50)
    timeout_seconds: int = Field(default=30, ge=5)


class FrameworkConfig(BaseModel):
    """Main framework configuration."""
    name: str = Field(default="automation-framework")
    version: str = Field(default="0.1.0")

    # Module configs
    throttle: ThrottleConfig = Field(default_factory=ThrottleConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    quarantine: QuarantineConfig = Field(default_factory=QuarantineConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    resources: ResourceConfig = Field(default_factory=ResourceConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)

    # Paths
    rules_directory: str = Field(default="./config/rules")
    workflows_directory: str = Field(default="./config/workflows")
    data_directory: str = Field(default="./data")

    def config_hash(self) -> str:
        """Generate hash of config for change detection."""
        return hashlib.sha256(
            self.model_dump_json().encode()
        ).hexdigest()[:16]


@dataclass
class RuleDefinition:
    """A single rule definition."""
    id: str
    name: str
    description: str = ""
    enabled: bool = True
    priority: int = 0

    # Trigger conditions
    triggers: list[dict[str, Any]] = field(default_factory=list)

    # Conditions that must be true
    conditions: list[dict[str, Any]] = field(default_factory=list)

    # Actions to execute
    actions: list[dict[str, Any]] = field(default_factory=list)

    # Error handling overrides
    retry_config: Optional[dict[str, Any]] = None

    # Safety
    requires_approval: bool = False
    max_executions_per_hour: Optional[int] = None

    # Metadata
    tags: list[str] = field(default_factory=list)
    config_hash: str = ""

    def __post_init__(self):
        if not self.config_hash:
            self.config_hash = self._compute_hash()

    def _compute_hash(self) -> str:
        """Compute hash of rule definition."""
        import json
        content = json.dumps({
            "triggers": self.triggers,
            "conditions": self.conditions,
            "actions": self.actions,
        }, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()[:16]


@dataclass
class WorkflowDefinition:
    """A workflow definition (sequence of rules/steps)."""
    id: str
    name: str
    description: str = ""
    enabled: bool = True

    # Steps to execute in order
    steps: list[dict[str, Any]] = field(default_factory=list)

    # Parameters that can be passed at runtime
    parameters: dict[str, Any] = field(default_factory=dict)

    # Trigger command (e.g., "/run workflow_name")
    trigger_command: Optional[str] = None

    # Error handling
    on_error: str = "stop"  # stop, continue, rollback

    # Safety
    requires_approval: bool = False

    config_hash: str = ""

    def __post_init__(self):
        if not self.config_hash:
            self.config_hash = self._compute_hash()

    def _compute_hash(self) -> str:
        import json
        content = json.dumps({"steps": self.steps}, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()[:16]


class ConfigLoader:
    """Loads and validates YAML/JSON configurations."""

    def __init__(self, config_dir: str = "./config"):
        self.config_dir = Path(config_dir)
        self._cache: dict[str, Any] = {}
        self._hashes: dict[str, str] = {}

    def load_framework_config(self, path: Optional[str] = None) -> FrameworkConfig:
        """Load main framework configuration."""
        if path is None:
            path = self.config_dir / "framework.yaml"
        else:
            path = Path(path)

        data = self._load_file(path)
        try:
            return FrameworkConfig(**data)
        except Exception as e:
            raise ConfigError(f"Invalid framework config: {e}", config_path=str(path))

    def load_rules(self, directory: Optional[str] = None) -> list[RuleDefinition]:
        """Load all rule definitions from directory."""
        if directory is None:
            directory = self.config_dir / "rules"
        else:
            directory = Path(directory)

        rules = []
        if not directory.exists():
            return rules

        for file_path in directory.glob("**/*.yaml"):
            rules.extend(self._load_rules_file(file_path))
        for file_path in directory.glob("**/*.json"):
            rules.extend(self._load_rules_file(file_path))

        # Sort by priority (higher first)
        rules.sort(key=lambda r: r.priority, reverse=True)
        return rules

    def load_workflows(self, directory: Optional[str] = None) -> list[WorkflowDefinition]:
        """Load all workflow definitions from directory."""
        if directory is None:
            directory = self.config_dir / "workflows"
        else:
            directory = Path(directory)

        workflows = []
        if not directory.exists():
            return workflows

        for file_path in directory.glob("**/*.yaml"):
            workflows.extend(self._load_workflows_file(file_path))
        for file_path in directory.glob("**/*.json"):
            workflows.extend(self._load_workflows_file(file_path))

        return workflows

    def has_config_changed(self, path: str) -> bool:
        """Check if a config file has changed since last load."""
        path = Path(path)
        current_hash = self._compute_file_hash(path)
        previous_hash = self._hashes.get(str(path))
        return current_hash != previous_hash

    def _load_file(self, path: Path) -> dict[str, Any]:
        """Load YAML or JSON file."""
        if not path.exists():
            raise ConfigError(f"Config file not found: {path}", config_path=str(path))

        try:
            content = path.read_text()
            self._hashes[str(path)] = hashlib.sha256(content.encode()).hexdigest()[:16]

            if path.suffix in (".yaml", ".yml"):
                return yaml.safe_load(content) or {}
            elif path.suffix == ".json":
                return json.loads(content)
            else:
                raise ConfigError(
                    f"Unsupported config format: {path.suffix}",
                    config_path=str(path)
                )
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML: {e}", config_path=str(path))
        except json.JSONDecodeError as e:
            raise ConfigError(f"Invalid JSON: {e}", config_path=str(path))

    def _load_rules_file(self, path: Path) -> list[RuleDefinition]:
        """Load rules from a single file."""
        data = self._load_file(path)
        rules = []

        # Support both single rule and list of rules
        rule_list = data.get("rules", [data] if "id" in data else [])

        for rule_data in rule_list:
            try:
                rule = RuleDefinition(
                    id=rule_data["id"],
                    name=rule_data.get("name", rule_data["id"]),
                    description=rule_data.get("description", ""),
                    enabled=rule_data.get("enabled", True),
                    priority=rule_data.get("priority", 0),
                    triggers=rule_data.get("triggers", []),
                    conditions=rule_data.get("conditions", []),
                    actions=rule_data.get("actions", []),
                    retry_config=rule_data.get("retry_config"),
                    requires_approval=rule_data.get("requires_approval", False),
                    max_executions_per_hour=rule_data.get("max_executions_per_hour"),
                    tags=rule_data.get("tags", []),
                )
                rules.append(rule)
            except KeyError as e:
                raise ConfigError(
                    f"Missing required field in rule: {e}",
                    config_path=str(path)
                )

        return rules

    def _load_workflows_file(self, path: Path) -> list[WorkflowDefinition]:
        """Load workflows from a single file."""
        data = self._load_file(path)
        workflows = []

        workflow_list = data.get("workflows", [data] if "id" in data else [])

        for wf_data in workflow_list:
            try:
                workflow = WorkflowDefinition(
                    id=wf_data["id"],
                    name=wf_data.get("name", wf_data["id"]),
                    description=wf_data.get("description", ""),
                    enabled=wf_data.get("enabled", True),
                    steps=wf_data.get("steps", []),
                    parameters=wf_data.get("parameters", {}),
                    trigger_command=wf_data.get("trigger_command"),
                    on_error=wf_data.get("on_error", "stop"),
                    requires_approval=wf_data.get("requires_approval", False),
                )
                workflows.append(workflow)
            except KeyError as e:
                raise ConfigError(
                    f"Missing required field in workflow: {e}",
                    config_path=str(path)
                )

        return workflows

    def _compute_file_hash(self, path: Path) -> str:
        """Compute hash of file contents."""
        if not path.exists():
            return ""
        content = path.read_text()
        return hashlib.sha256(content.encode()).hexdigest()[:16]
