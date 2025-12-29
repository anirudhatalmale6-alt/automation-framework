# Generic Rule-Based Automation Framework

A modular, config-driven automation framework designed for low-resource environments.

## Features

- **Config-Driven**: All behavior defined in YAML/JSON, no code changes needed
- **Deterministic**: Rules always override AI/heuristics
- **Safety-First**: Anti-loop protection, restart storm prevention, circuit breakers
- **Low-Resource**: Runs on i3/8GB RAM with single browser instance
- **Modular**: Clean separation between orchestration, rules, and actions
- **Persistent**: Failure tracking survives restarts (SQLite)

## Quick Start

### Docker

```bash
# Build
docker build -t automation-framework .

# Run
docker run -d \
  -v $(pwd)/config:/app/config:ro \
  -v $(pwd)/data:/app/data \
  -p 8080:8080 \
  automation-framework
```

### Docker Compose

```bash
docker-compose up -d
```

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run
python -m src.main
```

## Configuration

### Framework Settings

Edit `config/framework.yaml`:

```yaml
throttle:
  max_concurrent_tasks: 3
  max_tasks_per_minute: 20

retry:
  max_attempts: 3
  base_delay_seconds: 1.0

safety:
  max_loop_iterations: 100
  max_task_runtime_seconds: 300
```

### Rules

Create rules in `config/rules/*.yaml`:

```yaml
rules:
  - id: my_rule
    name: My Rule
    triggers:
      - type: command
        conditions:
          command: "/mycommand"
    conditions:
      - field: trigger.confirmed
        operator: eq
        value: true
    actions:
      - type: log
        params:
          message: "Rule executed"
```

### Workflows

Create workflows in `config/workflows/*.yaml`:

```yaml
workflows:
  - id: my_workflow
    name: My Workflow
    trigger_command: "/run myworkflow"
    steps:
      - id: step1
        action: log
        payload:
          message: "Step 1"
```

## Health Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Liveness check |
| `GET /ready` | Readiness check |
| `GET /status` | Detailed status |

## Safety Features

### Anti-Loop Protection

- Error fingerprinting
- Progressive cooldown ladder
- Quarantine after repeated failures

### Restart Storm Prevention

- Persistent failure state (SQLite)
- Cooldown enforcement across restarts
- Auto-pause on high failure rate

### Circuit Breaker

- Global failure threshold
- Automatic recovery testing
- Cascade failure protection

## Project Structure

```
├── config/
│   ├── framework.yaml      # Main config
│   ├── rules/              # Rule definitions
│   └── workflows/          # Workflow definitions
├── data/
│   └── state.db           # SQLite state
├── docs/
│   ├── M1-ARCHITECTURE.md
│   ├── CONFIGURATION.md
│   └── ANTI-LOOP.md
├── src/
│   ├── core/              # Config, state, errors
│   ├── orchestrator/      # Supervisor, scheduler
│   ├── rules/             # Rules engine
│   ├── safety/            # Guards, throttling
│   └── main.py            # Entry point
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Milestones

- [x] **M1**: Orchestrator + Rules Engine + Safety Layer
- [ ] **M2**: Browser Automation + Telegram Integration
- [ ] **M3**: Voice Interface + Documentation + Packaging

## Documentation

- [Architecture](docs/M1-ARCHITECTURE.md)
- [Configuration Reference](docs/CONFIGURATION.md)
- [Anti-Loop Design](docs/ANTI-LOOP.md)

## License

Proprietary - All rights reserved.
