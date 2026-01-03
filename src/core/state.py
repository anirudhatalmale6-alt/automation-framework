"""Persistent state management using SQLite."""

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass, asdict
from enum import Enum

import aiosqlite

from core.errors import FrameworkError


class TaskStatus(Enum):
    """Task execution status."""
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    CANCELLED = "cancelled"


@dataclass
class FailureRecord:
    """Persistent failure tracking record."""
    fingerprint: str
    task_id: str
    error_type: str
    error_message: str
    context_json: str
    count: int
    first_seen: float
    last_seen: float
    cooldown_until: float
    quarantined: bool


@dataclass
class TaskRecord:
    """Task execution record."""
    task_id: str
    workflow_id: Optional[str]
    rule_id: Optional[str]
    status: str
    priority: int
    created_at: float
    started_at: Optional[float]
    completed_at: Optional[float]
    input_json: str
    output_json: Optional[str]
    error_json: Optional[str]
    attempt: int
    max_attempts: int


class StateManager:
    """Manages persistent state in SQLite for restart resilience."""

    def __init__(self, db_path: str = "./data/state.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize database and create tables."""
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row

        await self._db.executescript("""
            -- Failure tracking table
            CREATE TABLE IF NOT EXISTS failures (
                fingerprint TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                error_type TEXT NOT NULL,
                error_message TEXT,
                context_json TEXT,
                count INTEGER DEFAULT 1,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                cooldown_until REAL DEFAULT 0,
                quarantined INTEGER DEFAULT 0
            );

            -- Task execution history
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                workflow_id TEXT,
                rule_id TEXT,
                status TEXT NOT NULL,
                priority INTEGER DEFAULT 0,
                created_at REAL NOT NULL,
                started_at REAL,
                completed_at REAL,
                input_json TEXT,
                output_json TEXT,
                error_json TEXT,
                attempt INTEGER DEFAULT 1,
                max_attempts INTEGER DEFAULT 3
            );

            -- Config hashes for change detection
            CREATE TABLE IF NOT EXISTS config_hashes (
                path TEXT PRIMARY KEY,
                hash TEXT NOT NULL,
                updated_at REAL NOT NULL
            );

            -- Execution metrics
            CREATE TABLE IF NOT EXISTS metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                metric_name TEXT NOT NULL,
                metric_value REAL NOT NULL,
                labels_json TEXT,
                timestamp REAL NOT NULL
            );

            -- Approval requests pending human review
            CREATE TABLE IF NOT EXISTS approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                description TEXT,
                context_json TEXT,
                requested_at REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                resolved_at REAL,
                resolved_by TEXT,
                resolution TEXT
            );

            -- Create indexes
            CREATE INDEX IF NOT EXISTS idx_failures_cooldown ON failures(cooldown_until);
            CREATE INDEX IF NOT EXISTS idx_failures_quarantine ON failures(quarantined);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_workflow ON tasks(workflow_id);
            CREATE INDEX IF NOT EXISTS idx_metrics_name ON metrics(metric_name, timestamp);
            CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
        """)
        await self._db.commit()

    async def close(self) -> None:
        """Close database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    # ==================== Failure Tracking ====================

    async def record_failure(
        self,
        error: FrameworkError,
        cooldown_ladder: list[int],
        max_before_quarantine: int,
    ) -> FailureRecord:
        """
        Record a failure and update cooldown/quarantine status.

        Returns the updated failure record with current state.
        """
        async with self._lock:
            fingerprint = error.fingerprint()
            now = time.time()

            # Check if we have an existing record
            cursor = await self._db.execute(
                "SELECT * FROM failures WHERE fingerprint = ?",
                (fingerprint,)
            )
            row = await cursor.fetchone()

            if row:
                # Update existing record
                count = row["count"] + 1
                quarantined = count >= max_before_quarantine

                # Calculate cooldown based on ladder
                ladder_index = min(count - 1, len(cooldown_ladder) - 1)
                cooldown_seconds = cooldown_ladder[ladder_index]
                cooldown_until = now + cooldown_seconds if not quarantined else 0

                await self._db.execute("""
                    UPDATE failures
                    SET count = ?, last_seen = ?, cooldown_until = ?, quarantined = ?
                    WHERE fingerprint = ?
                """, (count, now, cooldown_until, int(quarantined), fingerprint))

                record = FailureRecord(
                    fingerprint=fingerprint,
                    task_id=error.context.get("task_id", ""),
                    error_type=type(error).__name__,
                    error_message=error.message,
                    context_json=json.dumps(error.context),
                    count=count,
                    first_seen=row["first_seen"],
                    last_seen=now,
                    cooldown_until=cooldown_until,
                    quarantined=quarantined,
                )
            else:
                # Create new record
                cooldown_seconds = cooldown_ladder[0] if cooldown_ladder else 0
                cooldown_until = now + cooldown_seconds

                await self._db.execute("""
                    INSERT INTO failures
                    (fingerprint, task_id, error_type, error_message, context_json,
                     count, first_seen, last_seen, cooldown_until, quarantined)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, 0)
                """, (
                    fingerprint,
                    error.context.get("task_id", ""),
                    type(error).__name__,
                    error.message,
                    json.dumps(error.context),
                    now,
                    now,
                    cooldown_until,
                ))

                record = FailureRecord(
                    fingerprint=fingerprint,
                    task_id=error.context.get("task_id", ""),
                    error_type=type(error).__name__,
                    error_message=error.message,
                    context_json=json.dumps(error.context),
                    count=1,
                    first_seen=now,
                    last_seen=now,
                    cooldown_until=cooldown_until,
                    quarantined=False,
                )

            await self._db.commit()
            return record

    async def is_in_cooldown(self, fingerprint: str) -> bool:
        """Check if a failure fingerprint is currently in cooldown."""
        cursor = await self._db.execute(
            "SELECT cooldown_until FROM failures WHERE fingerprint = ?",
            (fingerprint,)
        )
        row = await cursor.fetchone()
        if not row:
            return False
        return row["cooldown_until"] > time.time()

    async def is_quarantined(self, fingerprint: str) -> bool:
        """Check if a failure fingerprint is quarantined."""
        cursor = await self._db.execute(
            "SELECT quarantined FROM failures WHERE fingerprint = ?",
            (fingerprint,)
        )
        row = await cursor.fetchone()
        if not row:
            return False
        return bool(row["quarantined"])

    async def reset_failure(self, fingerprint: str) -> bool:
        """Reset a failure record (manual reset or config change)."""
        async with self._lock:
            result = await self._db.execute(
                "DELETE FROM failures WHERE fingerprint = ?",
                (fingerprint,)
            )
            await self._db.commit()
            return result.rowcount > 0

    async def reset_all_failures(self) -> int:
        """Reset all failure records."""
        async with self._lock:
            result = await self._db.execute("DELETE FROM failures")
            await self._db.commit()
            return result.rowcount

    async def get_quarantined_tasks(self) -> list[FailureRecord]:
        """Get all quarantined failure records."""
        cursor = await self._db.execute(
            "SELECT * FROM failures WHERE quarantined = 1"
        )
        rows = await cursor.fetchall()
        return [
            FailureRecord(
                fingerprint=row["fingerprint"],
                task_id=row["task_id"],
                error_type=row["error_type"],
                error_message=row["error_message"],
                context_json=row["context_json"],
                count=row["count"],
                first_seen=row["first_seen"],
                last_seen=row["last_seen"],
                cooldown_until=row["cooldown_until"],
                quarantined=bool(row["quarantined"]),
            )
            for row in rows
        ]

    async def cleanup_expired_failures(self, auto_reset_hours: int) -> int:
        """Remove failure records older than auto_reset_hours."""
        async with self._lock:
            cutoff = time.time() - (auto_reset_hours * 3600)
            result = await self._db.execute(
                "DELETE FROM failures WHERE last_seen < ?",
                (cutoff,)
            )
            await self._db.commit()
            return result.rowcount

    # ==================== Task Tracking ====================

    async def create_task(
        self,
        task_id: str,
        workflow_id: Optional[str] = None,
        rule_id: Optional[str] = None,
        priority: int = 0,
        input_data: Optional[dict] = None,
        max_attempts: int = 3,
    ) -> TaskRecord:
        """Create a new task record."""
        async with self._lock:
            now = time.time()
            await self._db.execute("""
                INSERT INTO tasks
                (task_id, workflow_id, rule_id, status, priority,
                 created_at, input_json, attempt, max_attempts)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            """, (
                task_id,
                workflow_id,
                rule_id,
                TaskStatus.PENDING.value,
                priority,
                now,
                json.dumps(input_data or {}),
                max_attempts,
            ))
            await self._db.commit()

            return TaskRecord(
                task_id=task_id,
                workflow_id=workflow_id,
                rule_id=rule_id,
                status=TaskStatus.PENDING.value,
                priority=priority,
                created_at=now,
                started_at=None,
                completed_at=None,
                input_json=json.dumps(input_data or {}),
                output_json=None,
                error_json=None,
                attempt=1,
                max_attempts=max_attempts,
            )

    async def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        output: Optional[dict] = None,
        error: Optional[dict] = None,
    ) -> None:
        """Update task status."""
        async with self._lock:
            now = time.time()
            updates = ["status = ?"]
            params = [status.value]

            if status == TaskStatus.RUNNING:
                updates.append("started_at = ?")
                params.append(now)
            elif status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                updates.append("completed_at = ?")
                params.append(now)

            if output is not None:
                updates.append("output_json = ?")
                params.append(json.dumps(output))

            if error is not None:
                updates.append("error_json = ?")
                params.append(json.dumps(error))

            params.append(task_id)
            await self._db.execute(
                f"UPDATE tasks SET {', '.join(updates)} WHERE task_id = ?",
                params
            )
            await self._db.commit()

    async def increment_task_attempt(self, task_id: str) -> int:
        """Increment task attempt counter and return new value."""
        async with self._lock:
            await self._db.execute(
                "UPDATE tasks SET attempt = attempt + 1 WHERE task_id = ?",
                (task_id,)
            )
            await self._db.commit()

            cursor = await self._db.execute(
                "SELECT attempt FROM tasks WHERE task_id = ?",
                (task_id,)
            )
            row = await cursor.fetchone()
            return row["attempt"] if row else 0

    async def get_task(self, task_id: str) -> Optional[TaskRecord]:
        """Get task by ID."""
        cursor = await self._db.execute(
            "SELECT * FROM tasks WHERE task_id = ?",
            (task_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None

        return TaskRecord(
            task_id=row["task_id"],
            workflow_id=row["workflow_id"],
            rule_id=row["rule_id"],
            status=row["status"],
            priority=row["priority"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            input_json=row["input_json"],
            output_json=row["output_json"],
            error_json=row["error_json"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
        )

    async def get_pending_tasks(self, limit: int = 100) -> list[TaskRecord]:
        """Get pending tasks ordered by priority."""
        cursor = await self._db.execute("""
            SELECT * FROM tasks
            WHERE status IN (?, ?)
            ORDER BY priority DESC, created_at ASC
            LIMIT ?
        """, (TaskStatus.PENDING.value, TaskStatus.QUEUED.value, limit))
        rows = await cursor.fetchall()
        return [self._row_to_task(row) for row in rows]

    async def get_running_tasks(self) -> list[TaskRecord]:
        """Get all currently running tasks."""
        cursor = await self._db.execute(
            "SELECT * FROM tasks WHERE status = ?",
            (TaskStatus.RUNNING.value,)
        )
        rows = await cursor.fetchall()
        return [self._row_to_task(row) for row in rows]

    def _row_to_task(self, row) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            workflow_id=row["workflow_id"],
            rule_id=row["rule_id"],
            status=row["status"],
            priority=row["priority"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            input_json=row["input_json"],
            output_json=row["output_json"],
            error_json=row["error_json"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
        )

    # ==================== Config Change Detection ====================

    async def get_config_hash(self, path: str) -> Optional[str]:
        """Get stored config hash for change detection."""
        cursor = await self._db.execute(
            "SELECT hash FROM config_hashes WHERE path = ?",
            (path,)
        )
        row = await cursor.fetchone()
        return row["hash"] if row else None

    async def update_config_hash(self, path: str, hash_value: str) -> None:
        """Update stored config hash."""
        async with self._lock:
            await self._db.execute("""
                INSERT OR REPLACE INTO config_hashes (path, hash, updated_at)
                VALUES (?, ?, ?)
            """, (path, hash_value, time.time()))
            await self._db.commit()

    # ==================== Approvals ====================

    async def create_approval_request(
        self,
        task_id: str,
        action_type: str,
        description: str,
        context: Optional[dict] = None,
    ) -> int:
        """Create a pending approval request."""
        async with self._lock:
            cursor = await self._db.execute("""
                INSERT INTO approvals
                (task_id, action_type, description, context_json, requested_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                task_id,
                action_type,
                description,
                json.dumps(context or {}),
                time.time(),
            ))
            await self._db.commit()
            return cursor.lastrowid

    async def get_approval(self, approval_id: int) -> Optional[dict]:
        """Get approval request by ID."""
        cursor = await self._db.execute(
            "SELECT * FROM approvals WHERE id = ?",
            (approval_id,)
        )
        row = await cursor.fetchone()
        if row:
            result = dict(row)
            # Parse context JSON
            if result.get("context_json"):
                result["context"] = json.loads(result["context_json"])
            return result
        return None

    async def resolve_approval(
        self,
        approval_id: int,
        resolution: str,
        resolved_by: str,
    ) -> bool:
        """Resolve an approval request."""
        async with self._lock:
            result = await self._db.execute("""
                UPDATE approvals
                SET status = 'resolved', resolved_at = ?, resolved_by = ?, resolution = ?
                WHERE id = ? AND status = 'pending'
            """, (time.time(), resolved_by, resolution, approval_id))
            await self._db.commit()
            return result.rowcount > 0

    async def get_pending_approvals(self) -> list[dict]:
        """Get all pending approval requests."""
        cursor = await self._db.execute(
            "SELECT * FROM approvals WHERE status = 'pending' ORDER BY requested_at ASC"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ==================== Metrics ====================

    async def record_metric(
        self,
        name: str,
        value: float,
        labels: Optional[dict] = None,
    ) -> None:
        """Record a metric value."""
        async with self._lock:
            await self._db.execute("""
                INSERT INTO metrics (metric_name, metric_value, labels_json, timestamp)
                VALUES (?, ?, ?, ?)
            """, (name, value, json.dumps(labels or {}), time.time()))
            await self._db.commit()

    async def get_recent_failures_count(self, window_seconds: int) -> int:
        """Get count of failures in the recent time window."""
        cutoff = time.time() - window_seconds
        cursor = await self._db.execute(
            "SELECT COUNT(*) as cnt FROM failures WHERE last_seen >= ?",
            (cutoff,)
        )
        row = await cursor.fetchone()
        return row["cnt"] if row else 0

    async def cleanup_old_metrics(self, keep_hours: int = 24) -> int:
        """Remove metrics older than keep_hours."""
        async with self._lock:
            cutoff = time.time() - (keep_hours * 3600)
            result = await self._db.execute(
                "DELETE FROM metrics WHERE timestamp < ?",
                (cutoff,)
            )
            await self._db.commit()
            return result.rowcount
