# Anti-Loop and Restart Storm Prevention

This document explains how the framework prevents infinite loops and restart storms.

## Problem Statement

Without safeguards, automation can enter dangerous states:

1. **Infinite Loops**: A task fails, gets retried, fails again, forever
2. **Restart Storms**: Supervisor crashes, restarts, immediately triggers same failures
3. **Cascade Failures**: One failure triggers related failures, overwhelming system

## Solution Architecture

### 1. Error Fingerprinting

Each error generates a unique fingerprint:

```python
fingerprint = hash(
    error_type +      # e.g., "SelectorNotFound"
    task_id +         # e.g., "click_login_button"
    selector +        # e.g., "#login-btn"
    context_hash      # Serialized relevant context
)
```

This allows tracking **specific** failure patterns, not just any failure.

### 2. Persistent State (SQLite)

Critical data persists across restarts:

```sql
CREATE TABLE failures (
    fingerprint TEXT PRIMARY KEY,
    task_id TEXT,
    error_type TEXT,
    count INTEGER,
    first_seen REAL,
    last_seen REAL,
    cooldown_until REAL,  -- Unix timestamp
    quarantined INTEGER
);
```

**Key point**: On restart, existing cooldowns and quarantines are respected.

### 3. Cooldown Ladder

Progressive delays on repeated failures:

```
Failure 1: Retry after 1 second
Failure 2: Retry after 5 seconds
Failure 3: Retry after 15 seconds
Failure 4: Retry after 5 minutes
Failure 5: Retry after 30 minutes
Failure 6+: QUARANTINED (no auto-retry)
```

The ladder is configurable in `framework.yaml`:

```yaml
quarantine:
  cooldown_ladder_seconds: [1, 5, 15, 300, 1800]
  max_failures_before_quarantine: 6
```

### 4. Restart Storm Detection

On supervisor startup:

```python
async def _check_restart_safety(self):
    # Count recent failures
    recent_failures = await self.state.get_recent_failures_count(
        window_seconds=300  # Last 5 minutes
    )

    if recent_failures > threshold:
        # Start in PAUSED state
        self._state = SupervisorState.PAUSED
        logger.warning("restart_storm_detected")
```

The supervisor starts paused, requiring manual intervention.

### 5. Config Change Detection

When a rule's config hash changes, its failure history resets:

```python
# Rule definition includes config hash
class RuleDefinition:
    config_hash: str  # Hash of triggers + conditions + actions

# On config reload
if stored_hash != rule.config_hash:
    await state.reset_failures_for_rule(rule.id)
    # Assumption: config change = potential fix
```

### 6. Circuit Breaker (Global)

Protects against cascade failures:

```
State: CLOSED (normal)
  ↓ (5 failures in window)
State: OPEN (reject all)
  ↓ (60 seconds timeout)
State: HALF_OPEN (test recovery)
  ↓ (3 successes)
State: CLOSED (recovered)
```

When OPEN, no new tasks are scheduled.

### 7. Loop Detection

Per-task execution tracking:

```python
async def _detect_loop(self, task):
    pattern = f"{task.action}:{task.rule_id}"

    # Check executions in last 60 seconds
    count = self._count_recent_executions(pattern, window=60)

    if count >= max_loop_iterations:  # Default: 100
        raise SafetyError("Loop detected")
```

## How It Works Together

### Scenario: Bad Selector

1. Task `click_login` fails: selector `#login-btn` not found
2. Fingerprint: `abc123` created, count=1, cooldown=1s
3. Retry after 1s, fails again
4. Fingerprint: `abc123` updated, count=2, cooldown=5s
5. ... continues up the ladder ...
6. At count=6: task quarantined
7. Escalation sent to human
8. Human fixes config, changes selector
9. Config hash changes → fingerprint `abc123` cleared
10. Task can retry fresh

### Scenario: Restart Storm

1. Supervisor crashes during high-failure period
2. On restart, loads SQLite state
3. Sees 15 failures in last 5 minutes
4. Starts in PAUSED state
5. Doesn't re-queue any quarantined tasks
6. Respects all cooldowns
7. Waits for human to investigate and resume

### Scenario: Cascade Failure

1. External API goes down
2. Task A fails, Task B fails, Task C fails...
3. Circuit breaker opens after 5 failures
4. All new tasks rejected
5. 60 seconds later, circuit half-opens
6. Test 3 tasks
7. If still failing: re-open circuit
8. If succeeding: close circuit, resume normal

## Manual Reset Commands

Via Telegram (M2) or API:

```
/reset <fingerprint>     # Reset specific failure
/reset-all               # Reset all failures (caution!)
/status                  # View quarantined tasks
/resume                  # Resume from PAUSED state
```

## Configuration Summary

```yaml
# Individual task retries
retry:
  max_attempts: 3
  base_delay_seconds: 1.0
  exponential_base: 2.0

# Persistent failure tracking
quarantine:
  max_failures_before_quarantine: 6
  cooldown_ladder_seconds: [1, 5, 15, 300, 1800]
  auto_reset_after_hours: 24

# Global circuit breaker
circuit_breaker:
  failure_threshold: 5
  recovery_timeout_seconds: 60

# Loop detection
safety:
  max_loop_iterations: 100
  global_failure_threshold: 10
  global_failure_window_seconds: 300
```

## Testing Anti-Loop

To verify the system works:

1. Create a rule with an intentionally bad selector
2. Trigger it multiple times
3. Observe cooldown ladder in logs
4. Verify quarantine after 6 failures
5. Restart supervisor
6. Verify it respects existing quarantine
7. Fix the selector
8. Verify config change resets failure
