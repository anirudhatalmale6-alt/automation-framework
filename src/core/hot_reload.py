"""
Hot-reload module for config files.

Watches config directories for changes and safely reloads
without affecting running workflows.
"""

import asyncio
import os
import hashlib
from pathlib import Path
from typing import Any, Callable, Optional, Awaitable, Dict, List
from dataclasses import dataclass, field
from datetime import datetime
import structlog

from core.config import ConfigLoader, RuleDefinition, WorkflowDefinition
from core.errors import ConfigError

logger = structlog.get_logger()


@dataclass
class ReloadResult:
    """Result of a config reload attempt."""
    success: bool
    rules_loaded: int = 0
    workflows_loaded: int = 0
    errors: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class HotReloadConfig:
    """Configuration for hot-reload behavior."""
    enabled: bool = True
    check_interval_seconds: int = 10
    debounce_seconds: float = 2.0  # Wait for writes to settle
    notify_on_error: bool = True
    notify_on_success: bool = False


class HotReloader:
    """
    Hot-reload manager for config files.

    Features:
    - Watches rules and workflows directories
    - Validates configs before applying
    - Debounces rapid changes
    - Notifies via callback on errors
    - Safe reload (running workflows unaffected)
    """

    def __init__(
        self,
        config_loader: ConfigLoader,
        rules_dir: str,
        workflows_dir: str,
        config: Optional[HotReloadConfig] = None,
    ):
        self.config_loader = config_loader
        self.rules_dir = Path(rules_dir)
        self.workflows_dir = Path(workflows_dir)
        self.config = config or HotReloadConfig()

        # File hashes for change detection
        self._file_hashes: Dict[str, str] = {}

        # Callbacks
        self._on_reload: Optional[Callable[[List[RuleDefinition], List[WorkflowDefinition]], Awaitable[None]]] = None
        self._on_error: Optional[Callable[[str, List[str]], Awaitable[None]]] = None

        # State
        self._running = False
        self._last_change_time: float = 0
        self._pending_reload = False
        self._watch_task: Optional[asyncio.Task] = None

    def set_reload_callback(
        self,
        callback: Callable[[List[RuleDefinition], List[WorkflowDefinition]], Awaitable[None]],
    ) -> None:
        """Set callback for successful reloads."""
        self._on_reload = callback

    def set_error_callback(
        self,
        callback: Callable[[str, List[str]], Awaitable[None]],
    ) -> None:
        """Set callback for reload errors (e.g., Telegram notification)."""
        self._on_error = callback

    async def start(self) -> None:
        """Start watching for config changes."""
        if not self.config.enabled:
            logger.info("hot_reload_disabled")
            return

        self._running = True
        self._scan_files()  # Initial scan
        self._watch_task = asyncio.create_task(self._watch_loop())
        logger.info(
            "hot_reload_started",
            rules_dir=str(self.rules_dir),
            workflows_dir=str(self.workflows_dir),
            interval=self.config.check_interval_seconds,
        )

    async def stop(self) -> None:
        """Stop watching for changes."""
        self._running = False
        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
        logger.info("hot_reload_stopped")

    async def force_reload(self) -> ReloadResult:
        """Force an immediate reload of all configs."""
        return await self._do_reload()

    async def _watch_loop(self) -> None:
        """Background loop to watch for file changes."""
        while self._running:
            try:
                await asyncio.sleep(self.config.check_interval_seconds)

                if self._detect_changes():
                    # Debounce - wait for writes to settle
                    await asyncio.sleep(self.config.debounce_seconds)

                    # Check again in case more changes came
                    self._detect_changes()

                    # Now reload
                    result = await self._do_reload()

                    if not result.success and self.config.notify_on_error:
                        if self._on_error:
                            try:
                                await self._on_error(
                                    "Config reload failed",
                                    result.errors,
                                )
                                logger.info("hot_reload_error_notified", errors=result.errors)
                            except Exception as notify_err:
                                logger.error(
                                    "hot_reload_notify_failed",
                                    error=str(notify_err),
                                    original_errors=result.errors,
                                )
                    elif result.success and self.config.notify_on_success:
                        logger.info(
                            "hot_reload_success",
                            rules=result.rules_loaded,
                            workflows=result.workflows_loaded,
                        )

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("hot_reload_watch_error")

    def _scan_files(self) -> None:
        """Scan directories and compute file hashes."""
        self._file_hashes.clear()

        for directory in [self.rules_dir, self.workflows_dir]:
            if not directory.exists():
                continue
            for file_path in directory.glob("**/*.yaml"):
                self._file_hashes[str(file_path)] = self._compute_hash(file_path)
            for file_path in directory.glob("**/*.json"):
                self._file_hashes[str(file_path)] = self._compute_hash(file_path)

    def _detect_changes(self) -> bool:
        """Check if any config files have changed."""
        changed = False
        current_files = set()

        for directory in [self.rules_dir, self.workflows_dir]:
            if not directory.exists():
                continue
            for file_path in directory.glob("**/*.yaml"):
                current_files.add(str(file_path))
                new_hash = self._compute_hash(file_path)
                old_hash = self._file_hashes.get(str(file_path))
                if new_hash != old_hash:
                    logger.debug("config_file_changed", file=str(file_path))
                    changed = True
                self._file_hashes[str(file_path)] = new_hash
            for file_path in directory.glob("**/*.json"):
                current_files.add(str(file_path))
                new_hash = self._compute_hash(file_path)
                old_hash = self._file_hashes.get(str(file_path))
                if new_hash != old_hash:
                    logger.debug("config_file_changed", file=str(file_path))
                    changed = True
                self._file_hashes[str(file_path)] = new_hash

        # Check for deleted files
        deleted = set(self._file_hashes.keys()) - current_files
        if deleted:
            for f in deleted:
                del self._file_hashes[f]
            changed = True

        return changed

    def _compute_hash(self, path: Path) -> str:
        """Compute hash of file contents."""
        try:
            content = path.read_text()
            return hashlib.sha256(content.encode()).hexdigest()[:16]
        except Exception:
            return ""

    async def _do_reload(self) -> ReloadResult:
        """Perform the actual reload with validation."""
        errors = []
        rules = []
        workflows = []

        # Try to load rules
        try:
            rules = self.config_loader.load_rules(str(self.rules_dir))
            logger.info("hot_reload_rules_validated", count=len(rules))
        except ConfigError as e:
            errors.append(f"Rules error: {e.message}")
            logger.error("hot_reload_rules_error", error=str(e))
        except Exception as e:
            errors.append(f"Rules error: {str(e)}")
            logger.exception("hot_reload_rules_error")

        # Try to load workflows
        try:
            workflows = self.config_loader.load_workflows(str(self.workflows_dir))
            logger.info("hot_reload_workflows_validated", count=len(workflows))
        except ConfigError as e:
            errors.append(f"Workflows error: {e.message}")
            logger.error("hot_reload_workflows_error", error=str(e))
        except Exception as e:
            errors.append(f"Workflows error: {str(e)}")
            logger.exception("hot_reload_workflows_error")

        # If validation passed, apply the changes
        if not errors:
            if self._on_reload:
                try:
                    await self._on_reload(rules, workflows)
                    logger.info(
                        "hot_reload_applied",
                        rules=len(rules),
                        workflows=len(workflows),
                    )
                except Exception as e:
                    errors.append(f"Apply error: {str(e)}")
                    logger.exception("hot_reload_apply_error")

        return ReloadResult(
            success=len(errors) == 0,
            rules_loaded=len(rules) if not errors else 0,
            workflows_loaded=len(workflows) if not errors else 0,
            errors=errors,
        )
