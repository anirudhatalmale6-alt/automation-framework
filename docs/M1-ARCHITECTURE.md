# Milestone 1: Architecture Documentation

## Overview

This document describes the architecture of the core automation framework delivered in Milestone 1.

## Module Structure

```
src/
├── core/                   # Core framework components
│   ├── config.py          # Configuration loading (YAML/JSON)
│   ├── state.py           # Persistent state (SQLite)
│   └── errors.py          # Error classification
│
├── orchestrator/          # Task orchestration
│   ├── supervisor.py      # Central brain
│   ├── scheduler.py       # Priority queue + rate limiting
│   └── executor.py        # Task execution + retry
│
├── rules/                 # Rules engine
│   ├── engine.py          # Rule processing
│   ├── evaluator.py       # Condition evaluation
│   └── actions.py         # Action registry
│
├── safety/                # Safety constraints
│   ├── guards.py          # Resource guards
│   └── throttle.py        # Adaptive throttling
│
└── main.py               # Entry point
```

## Component Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                      SUPERVISOR                              │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │  Scheduler  │  │   Executor   │  │  Circuit Breaker  │  │
│  │             │  │              │  │                   │  │
│  │ - Priority  │  │ - Retry      │  │ - Failure count   │  │
│  │ - Rate Limit│  │ - Timeout    │  │ - Auto recovery   │  │
│  │ - Cooldown  │  │ - Backoff    │  │ - Half-open test  │  │
│  └──────┬──────┘  └──────┬───────┘  └───────────────────┘  │
│         │                │                                   │
│         └────────┬───────┘                                   │
│                  ▼                                           │
│  ┌───────────────────────────────────────────────────────┐  │
│  │                   STATE MANAGER                        │  │
│  │                                                        │  │
│  │  - Failure tracking (fingerprinted)                   │  │
│  │  - Cooldown/quarantine persistence                    │  │
│  │  - Task history                                       │  │
│  │  - Config change detection                            │  │
│  │                        SQLite                         │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      RULES ENGINE                            │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │  Triggers   │  │  Conditions  │  │     Actions       │  │
│  │             │  │              │  │                   │  │
│  │ - command   │  │ - eq, gt, lt │  │ - log             │  │
│  │ - schedule  │  │ - regex      │  │ - set_variable    │  │
│  │ - webhook   │  │ - and/or/not │  │ - delay           │  │
│  │ - any       │  │ - exists     │  │ - (extensible)    │  │
│  └─────────────┘  └──────────────┘  └───────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## Key Design Decisions

### 1. Deterministic Execution

Rules always override AI/heuristics. The rules engine evaluates conditions deterministically - no probabilistic behavior.

### 2. Persistent Failure Tracking

All failure data persists in SQLite:
- Survives restarts
- Prevents restart storms
- Enables cooldown enforcement across process restarts

### 3. Error Fingerprinting

Errors are fingerprinted using:
```
hash(error_type + task_id + selector + context)
```

This allows:
- Identifying recurring identical failures
- Applying escalating cooldowns to specific failure patterns
- Resetting on config changes (config hash changes → reset failures)

### 4. Cooldown Ladder

Progressive delays on repeated failures:
```yaml
cooldown_ladder_seconds:
  - 1      # First retry
  - 5      # Second
  - 15     # Third
  - 300    # Fourth (5 min)
  - 1800   # Fifth (30 min)
```

After `max_failures_before_quarantine` (default: 6), the task is quarantined and requires manual reset.

### 5. Circuit Breaker

Protects against cascade failures:

```
CLOSED → (failures > threshold) → OPEN
   ↑                                ↓
   └── (success after timeout) ← HALF_OPEN
```

When open, all new tasks are rejected until recovery timeout.

### 6. Restart Storm Prevention

On supervisor startup:
1. Load failure history from SQLite
2. Check recent failure count
3. If above threshold → start in PAUSED state
4. Respect existing cooldowns (don't re-queue quarantined tasks)

## Configuration Reference

See `config/framework.yaml` for all options. Key sections:

| Section | Purpose |
|---------|---------|
| `throttle` | Rate limits, concurrent task cap |
| `retry` | Backoff policy |
| `circuit_breaker` | Failure threshold, recovery timing |
| `quarantine` | Cooldown ladder, auto-reset period |
| `safety` | Loop detection, task timeouts |
| `resources` | Memory limits for low-end hardware |

## Rule Configuration

Rules are defined in YAML or JSON:

```yaml
rules:
  - id: unique_id
    name: Human Name
    enabled: true
    priority: 10              # Higher = evaluated first

    triggers:
      - type: command
        conditions:
          command: "/mycommand"

    conditions:
      - field: trigger.param
        operator: eq
        value: expected

    actions:
      - type: log
        params:
          message: "Executed"

    requires_approval: false
    max_executions_per_hour: 10
```

## Workflow Configuration

Workflows are sequences of steps:

```yaml
workflows:
  - id: my_workflow
    name: My Workflow
    trigger_command: "/run myworkflow"

    parameters:
      default_param: value

    steps:
      - id: step1
        action: some_action
        payload:
          key: "{{default_param}}"

    on_error: stop  # or: continue, rollback
```

## Health Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Basic liveness check |
| `GET /ready` | Readiness (supervisor state) |
| `GET /status` | Detailed status JSON |

## Next Steps (M2)

- Browser automation layer (Playwright)
- Telegram integration
- Additional action types
