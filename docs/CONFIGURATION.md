# Configuration Reference

This document describes all configuration options for the framework.

## Framework Configuration

Main config file: `config/framework.yaml`

### Throttle Settings

Control execution rate to prevent overload:

```yaml
throttle:
  max_concurrent_tasks: 3         # Max tasks running in parallel
  max_tasks_per_minute: 20        # Global rate limit
  max_browser_actions_per_minute: 30  # Browser-specific rate limit
  cooldown_on_error_seconds: 30   # Pause after errors
```

### Retry Policy

Exponential backoff configuration:

```yaml
retry:
  max_attempts: 3                 # Total attempts (including first)
  base_delay_seconds: 1.0         # Initial delay
  max_delay_seconds: 300.0        # Cap on delay (5 minutes)
  exponential_base: 2.0           # Multiplier per attempt
  jitter: true                    # Randomize to prevent thundering herd
```

Example backoff sequence with these settings:
- Attempt 1: immediate
- Attempt 2: ~1s delay (with jitter: 0.5-1.5s)
- Attempt 3: ~2s delay (with jitter: 1-3s)

### Circuit Breaker

Prevents cascade failures:

```yaml
circuit_breaker:
  failure_threshold: 5            # Open after N failures
  recovery_timeout_seconds: 60    # Wait before testing recovery
  half_open_max_calls: 3          # Successful calls to close
```

### Quarantine (Persistent Failures)

Handles tasks that fail repeatedly:

```yaml
quarantine:
  max_failures_before_quarantine: 6   # Quarantine after N failures
  cooldown_ladder_seconds:
    - 1      # 1st retry delay
    - 5      # 2nd retry delay
    - 15     # 3rd retry delay
    - 300    # 4th retry delay (5 min)
    - 1800   # 5th retry delay (30 min)
  auto_reset_after_hours: 24      # Auto-clear after 24h
```

### Safety Constraints

Prevent runaway execution:

```yaml
safety:
  max_loop_iterations: 100        # Detect execution loops
  max_task_runtime_seconds: 300   # Kill after 5 minutes
  global_failure_threshold: 10    # Pause supervisor if exceeded
  global_failure_window_seconds: 300  # Window for counting failures
  require_approval_for_destructive: true
```

### Resource Limits

For low-end hardware (i3/8GB target):

```yaml
resources:
  max_memory_mb: 4096             # Process memory limit
  max_browser_pages: 1            # Single tab only
  cleanup_interval_seconds: 60    # GC frequency
  gc_threshold_mb: 3072           # Trigger cleanup at this level
```

### Telegram (M2)

```yaml
telegram:
  enabled: false
  bot_token: ""                   # From @BotFather
  allowed_user_ids: []            # Whitelist (empty = all)
  typing_delay_ms: 500            # Human-like delay
  message_chunk_size: 4000        # Split long messages
```

### Voice Interface (M2)

```yaml
voice:
  enabled: false
  provider: null                  # No provider lock-in
  config: {}                      # Provider-specific config
```

### Local LLM (Optional)

```yaml
llm:
  enabled: false
  provider: ollama
  base_url: "http://localhost:11434"
  model: "llama3.2:3b"
  max_tokens: 500
  timeout_seconds: 30
```

## Rules Configuration

Rules go in `config/rules/` as `.yaml` or `.json` files.

### Rule Structure

```yaml
rules:
  - id: unique_rule_id          # Required, must be unique
    name: Human Readable Name   # Display name
    description: What it does   # Documentation
    enabled: true               # Can be disabled
    priority: 0                 # Higher = evaluated first

    triggers:                   # When to evaluate this rule
      - type: command
        conditions:
          command: "/mycommand"

    conditions:                 # Must all be true (AND)
      - field: trigger.data.key
        operator: eq
        value: expected

    actions:                    # Execute in order
      - type: log
        params:
          message: "Hello"

    requires_approval: false    # Human approval needed?
    max_executions_per_hour: 10 # Rate limit per rule
    tags: [category, type]      # For filtering
```

### Trigger Types

| Type | Description | Conditions |
|------|-------------|------------|
| `command` | Telegram/CLI command | `command: "/name"` |
| `schedule` | Cron-based trigger | `schedule: "0 * * * *"` |
| `webhook` | HTTP webhook | `source: "api"` |
| `any` | Match all events | - |

### Condition Operators

| Operator | Description | Example |
|----------|-------------|---------|
| `eq` | Equals | `operator: eq, value: 5` |
| `ne` | Not equals | `operator: ne, value: null` |
| `gt` | Greater than | `operator: gt, value: 10` |
| `lt` | Less than | `operator: lt, value: 100` |
| `gte` | Greater or equal | `operator: gte, value: 0` |
| `lte` | Less or equal | `operator: lte, value: 1.0` |
| `contains` | String/list contains | `operator: contains, value: "error"` |
| `startswith` | String starts with | `operator: startswith, value: "http"` |
| `endswith` | String ends with | `operator: endswith, value: ".pdf"` |
| `regex` | Regex match | `operator: regex, value: "^[0-9]+$"` |
| `in` | Value in list | `operator: in, value: [a, b, c]` |
| `not_in` | Value not in list | `operator: not_in, value: [x, y]` |
| `exists` | Field exists | `operator: exists` |
| `not_exists` | Field missing | `operator: not_exists` |
| `is_type` | Type check | `operator: is_type, value: string` |

### Logical Operators

```yaml
conditions:
  # AND (default - all conditions must pass)
  - field: a
    operator: eq
    value: 1
  - field: b
    operator: gt
    value: 0

  # OR
  - or:
      - field: status
        operator: eq
        value: "ready"
      - field: force
        operator: eq
        value: true

  # NOT
  - not:
      field: disabled
      operator: eq
      value: true

  # Nested
  - or:
      - and:
          - field: count
            operator: gt
            value: 10
          - field: count
            operator: lt
            value: 100
      - field: override
        operator: eq
        value: true
```

### Field References

```yaml
# From trigger event data
field: trigger.payload.user_id

# From context variables
field: variables.result

# From state
field: state.last_run

# Direct (tries variables, then trigger)
field: user_id
```

### Variable Interpolation

Use `{{variable}}` in action params:

```yaml
actions:
  - type: log
    params:
      message: "User {{trigger.user_id}} requested {{trigger.action}}"
```

## Workflow Configuration

Workflows go in `config/workflows/` as `.yaml` or `.json` files.

### Workflow Structure

```yaml
workflows:
  - id: my_workflow
    name: My Workflow
    description: What it does
    enabled: true
    trigger_command: "/run myworkflow"  # Command to activate

    parameters:                   # Defaults, can be overridden
      format: json
      verbose: false

    steps:
      - id: step_1
        name: First Step
        action: some_action
        payload:
          input: "{{format}}"     # Variable interpolation

      - id: step_2
        name: Second Step
        action: another_action
        payload:
          previous: "{{step_1_output}}"  # Previous step output

    on_error: stop                # stop | continue | rollback
    requires_approval: false
```

### Error Handling

| Mode | Behavior |
|------|----------|
| `stop` | Stop on first error, return partial result |
| `continue` | Log error, continue to next step |
| `rollback` | Execute rollback actions in reverse order |

### Rollback Actions

```yaml
steps:
  - id: create_resource
    action: create
    payload:
      name: "new-resource"
    rollback_action: delete        # Called if later steps fail
    rollback_payload:
      id: "{{resource_id}}"
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFIG_PATH` | `./config/framework.yaml` | Config file path |
| `DATA_DIR` | `./data` | Data directory |
| `LOG_LEVEL` | `INFO` | Log verbosity |
| `LOG_FORMAT` | `console` | `console` or `json` |
| `HEALTH_PORT` | `8080` | Health server port |
| `TELEGRAM_BOT_TOKEN` | - | Telegram bot token |
| `MAX_MEMORY_MB` | `4096` | Memory limit |
| `BROWSER_HEADLESS` | `true` | Headless browser mode |
