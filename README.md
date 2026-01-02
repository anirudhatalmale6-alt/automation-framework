# Generic Rule-Based Automation Framework

A modular, config-driven automation framework designed for low-resource environments.

## Features

- **Config-Driven**: All behavior defined in YAML/JSON, no code changes needed
- **Deterministic**: Rules always override AI/heuristics
- **Safety-First**: Anti-loop protection, restart storm prevention, circuit breakers
- **Low-Resource**: Runs on i3/8GB RAM with single browser instance
- **Modular**: Clean separation between orchestration, rules, and actions
- **Persistent**: Failure tracking survives restarts (SQLite)
- **Multi-Channel**: Telegram bot + Voice interface abstractions

## Quick Start

### Docker Compose (Recommended)

```bash
# Configure environment
export TELEGRAM_BOT_TOKEN="your_bot_token"
export TELEGRAM_ALLOWED_USERS="your_user_id"

# Start
docker-compose up -d --build

# Check health
curl http://localhost:8080/health
curl http://localhost:8080/status
```

### Local Development

```bash
# Install dependencies
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run demo
python demo.py

# Run full system
PYTHONPATH=src python -m main
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | - |
| `TELEGRAM_ALLOWED_USERS` | Comma-separated user IDs | - |
| `TELEGRAM_ADMIN_CHAT_ID` | Admin chat for escalations | - |
| `VOICE_PROVIDER` | Voice provider (`mock` for testing) | - |
| `VOICE_API_KEY` | Voice provider API key | - |
| `BROWSER_HEADLESS` | Run browser headless | `true` |
| `LOG_LEVEL` | Logging level | `INFO` |

### Framework Settings

Edit `config/framework.yaml`:

```yaml
name: automation-framework

throttle:
  max_concurrent_tasks: 3
  max_tasks_per_minute: 20

retry:
  max_attempts: 3
  base_delay_seconds: 1.0

safety:
  max_loop_iterations: 100
  max_task_runtime_seconds: 300

quarantine:
  max_failures_before_quarantine: 6
  cooldown_ladder_seconds: [1, 5, 15, 300, 1800]
```

### Rules

Create rules in `config/rules/*.yaml` or `*.json`:

```yaml
rules:
  - id: my_rule
    name: My Rule
    triggers:
      - type: command
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
      - type: log
        params:
          message: "Step 1"
      - type: browser_navigate
        params:
          url: "https://example.com"
```

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Check bot status |
| `/status` | System health and stats |
| `/workflows` | List available workflows |
| `/rules` | List loaded rules |
| `/run <workflow>` | Execute workflow |
| `/pause` | Pause execution |
| `/resume` | Resume execution |
| `/quarantined` | View quarantined tasks |
| `/reset <fingerprint>` | Reset failure state |

## Health Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Liveness check |
| `GET /ready` | Readiness check |
| `GET /status` | Detailed status (JSON) |

## Safety Features

### Anti-Loop Protection
- Error fingerprinting for deduplication
- Progressive cooldown ladder [1s, 5s, 15s, 5min, 30min]
- Quarantine after 6 repeated failures

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
automation-framework/
├── config/
│   ├── framework.yaml        # Main configuration
│   ├── rules/                # Rule definitions (YAML/JSON)
│   └── workflows/            # Workflow definitions
├── src/
│   ├── core/                 # Config, state, errors
│   ├── orchestrator/         # Supervisor, scheduler, executor
│   ├── rules/                # Rules engine, evaluator, actions
│   ├── safety/               # Guards, throttling
│   ├── browser/              # Playwright browser automation
│   ├── tg/                   # Telegram bot integration
│   ├── voice/                # Voice call abstraction
│   └── main.py               # Application entry point
├── docs/
│   ├── M1-ARCHITECTURE.md    # Core architecture
│   ├── M2-BROWSER-TELEGRAM.md # Browser & Telegram
│   ├── M3-VOICE-INTERFACE.md # Voice interface
│   ├── CONFIGURATION.md      # Config reference
│   └── ANTI-LOOP.md          # Anti-loop design
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── demo.py                   # Demo script
```

## Modules

### Core (`src/core/`)
- Configuration loading (YAML/JSON)
- SQLite state management
- Error types and fingerprinting

### Orchestrator (`src/orchestrator/`)
- Supervisor with circuit breaker
- Priority queue scheduler
- Task/workflow executor with retry

### Rules (`src/rules/`)
- Rules engine with trigger matching
- Condition evaluator (15+ operators)
- Action registry with variable interpolation

### Browser (`src/browser/`)
- Single persistent Playwright browser
- 12 config-driven browser actions
- Optimized for low-memory environments

### Telegram (`src/tg/`)
- Bot with human-like typing delays
- Command handlers for workflow control
- Inline buttons for approvals

### Voice (`src/voice/`)
- Provider-agnostic interface
- Mock provider for testing
- 10 voice workflow actions

## Documentation

- [Core Architecture](docs/M1-ARCHITECTURE.md)
- [Browser & Telegram](docs/M2-BROWSER-TELEGRAM.md)
- [Voice Interface](docs/M3-VOICE-INTERFACE.md)
- [Configuration Reference](docs/CONFIGURATION.md)
- [Anti-Loop Design](docs/ANTI-LOOP.md)

## Milestones

- [x] **M1**: Orchestrator + Rules Engine + Safety Layer
- [x] **M2**: Browser Automation + Telegram Integration
- [x] **M3**: Voice Interface + Documentation + Packaging

## License

Proprietary - All rights reserved.
