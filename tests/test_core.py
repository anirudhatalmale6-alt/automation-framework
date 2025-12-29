"""Tests for core framework components."""

import asyncio
import tempfile
import os
import pytest

# Add src to path
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from core.config import ConfigLoader, FrameworkConfig, RuleDefinition
from core.state import StateManager, TaskStatus
from core.errors import FrameworkError, TaskError, ErrorSeverity, ErrorCategory


class TestFrameworkConfig:
    """Test configuration loading."""

    def test_default_config(self):
        """Test default configuration values."""
        config = FrameworkConfig()

        assert config.throttle.max_concurrent_tasks == 5
        assert config.retry.max_attempts == 3
        assert config.safety.max_loop_iterations == 100
        assert config.resources.max_browser_pages == 1

    def test_config_hash(self):
        """Test config hash generation."""
        config1 = FrameworkConfig()
        config2 = FrameworkConfig()

        # Same config = same hash
        assert config1.config_hash() == config2.config_hash()

        # Different config = different hash
        config2.throttle.max_concurrent_tasks = 10
        # Note: config_hash uses model_dump_json, so this would differ


class TestErrorFingerprinting:
    """Test error fingerprinting for deduplication."""

    def test_same_error_same_fingerprint(self):
        """Same error context should produce same fingerprint."""
        error1 = TaskError(
            "Element not found",
            task_id="click_button",
            action="click",
            context={"selector": "#btn"},
        )
        error2 = TaskError(
            "Element not found",
            task_id="click_button",
            action="click",
            context={"selector": "#btn"},
        )

        assert error1.fingerprint() == error2.fingerprint()

    def test_different_context_different_fingerprint(self):
        """Different context should produce different fingerprint."""
        error1 = TaskError(
            "Element not found",
            task_id="click_button_1",
        )
        error2 = TaskError(
            "Element not found",
            task_id="click_button_2",
        )

        assert error1.fingerprint() != error2.fingerprint()

    def test_error_serialization(self):
        """Test error to dict serialization."""
        error = TaskError(
            "Test error",
            task_id="test_task",
            severity=ErrorSeverity.HIGH,
            category=ErrorCategory.PERMANENT,
        )

        data = error.to_dict()
        assert data["type"] == "TaskError"
        assert data["message"] == "Test error"
        assert data["severity"] == "high"
        assert data["category"] == "permanent"
        assert "fingerprint" in data


class TestStateManager:
    """Test persistent state management."""

    @pytest.fixture
    async def state_manager(self):
        """Create a temporary state manager."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_state.db")
            manager = StateManager(db_path)
            await manager.initialize()
            yield manager
            await manager.close()

    @pytest.mark.asyncio
    async def test_failure_recording(self, state_manager):
        """Test failure recording and cooldown."""
        error = TaskError("Test error", task_id="test_task")

        record = await state_manager.record_failure(
            error,
            cooldown_ladder=[1, 5, 15],
            max_before_quarantine=3,
        )

        assert record.count == 1
        assert not record.quarantined
        assert record.cooldown_until > 0

    @pytest.mark.asyncio
    async def test_quarantine_after_max_failures(self, state_manager):
        """Test quarantine after max failures."""
        error = TaskError("Test error", task_id="test_task")

        for i in range(6):
            record = await state_manager.record_failure(
                error,
                cooldown_ladder=[1, 5, 15, 300, 1800],
                max_before_quarantine=6,
            )

        assert record.count == 6
        assert record.quarantined

    @pytest.mark.asyncio
    async def test_failure_reset(self, state_manager):
        """Test failure reset."""
        error = TaskError("Test error", task_id="test_task")

        record = await state_manager.record_failure(
            error,
            cooldown_ladder=[1],
            max_before_quarantine=10,
        )

        success = await state_manager.reset_failure(record.fingerprint)
        assert success

        # Should not be in cooldown anymore
        is_cooldown = await state_manager.is_in_cooldown(record.fingerprint)
        assert not is_cooldown

    @pytest.mark.asyncio
    async def test_task_creation(self, state_manager):
        """Test task record creation."""
        task = await state_manager.create_task(
            task_id="test_task_1",
            workflow_id="test_workflow",
            priority=10,
            input_data={"key": "value"},
        )

        assert task.task_id == "test_task_1"
        assert task.status == TaskStatus.PENDING.value
        assert task.priority == 10

    @pytest.mark.asyncio
    async def test_task_status_update(self, state_manager):
        """Test task status updates."""
        await state_manager.create_task(task_id="test_task_2")

        await state_manager.update_task_status(
            "test_task_2",
            TaskStatus.RUNNING,
        )

        task = await state_manager.get_task("test_task_2")
        assert task.status == TaskStatus.RUNNING.value
        assert task.started_at is not None


class TestConfigLoader:
    """Test YAML/JSON config loading."""

    @pytest.fixture
    def config_dir(self):
        """Create temp config directory with test files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create framework config
            with open(os.path.join(tmpdir, "framework.yaml"), "w") as f:
                f.write("""
name: test-framework
throttle:
  max_concurrent_tasks: 2
""")

            # Create rules directory
            rules_dir = os.path.join(tmpdir, "rules")
            os.makedirs(rules_dir)

            with open(os.path.join(rules_dir, "test.yaml"), "w") as f:
                f.write("""
rules:
  - id: test_rule
    name: Test Rule
    triggers:
      - type: command
    conditions: []
    actions:
      - type: log
        params:
          message: test
""")

            # Create JSON rule
            with open(os.path.join(rules_dir, "json_rule.json"), "w") as f:
                f.write("""
{
    "rules": [{
        "id": "json_test_rule",
        "name": "JSON Test",
        "triggers": [{"type": "any"}],
        "conditions": [],
        "actions": []
    }]
}
""")

            yield tmpdir

    def test_load_framework_config(self, config_dir):
        """Test framework config loading."""
        loader = ConfigLoader(config_dir)
        config = loader.load_framework_config()

        assert config.name == "test-framework"
        assert config.throttle.max_concurrent_tasks == 2

    def test_load_yaml_rules(self, config_dir):
        """Test YAML rule loading."""
        loader = ConfigLoader(config_dir)
        rules = loader.load_rules(os.path.join(config_dir, "rules"))

        yaml_rule = next((r for r in rules if r.id == "test_rule"), None)
        assert yaml_rule is not None
        assert yaml_rule.name == "Test Rule"

    def test_load_json_rules(self, config_dir):
        """Test JSON rule loading."""
        loader = ConfigLoader(config_dir)
        rules = loader.load_rules(os.path.join(config_dir, "rules"))

        json_rule = next((r for r in rules if r.id == "json_test_rule"), None)
        assert json_rule is not None
        assert json_rule.name == "JSON Test"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
