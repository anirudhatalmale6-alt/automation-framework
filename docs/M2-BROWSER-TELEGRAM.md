# Milestone 2: Browser Automation & Telegram Integration

## Overview

M2 adds browser automation (Playwright) and Telegram integration for command-driven workflow activation.

## Browser Module

### Architecture

```
src/browser/
├── manager.py      # Single persistent browser instance
├── context.py      # Page operations with error handling
└── actions.py      # Config-driven action handlers
```

### BrowserManager

Manages a single persistent browser instance optimized for low-resource environments.

```python
from browser.manager import BrowserManager

manager = BrowserManager(
    headless=True,
    user_data_dir="./data/browser",
    max_pages=1,  # Single page for low RAM
)

await manager.initialize()
page = await manager.get_page()
await manager.shutdown()
```

Features:
- Single browser context (memory efficient)
- Session persistence (cookies, storage)
- Automatic cleanup on memory pressure
- Graceful restart capability

### BrowserContext

Wraps Playwright page with error handling and state tracking.

```python
from browser.context import BrowserContext

ctx = BrowserContext(page, default_timeout=30000)

# Navigate
result = await ctx.navigate("https://example.com")

# Click
result = await ctx.click("#submit-button")

# Fill input
result = await ctx.fill("#username", "user@example.com")

# Extract text
result = await ctx.extract_text(".result")
print(result.data["text"])

# Screenshot
result = await ctx.screenshot(path="./screenshot.png")
```

### Browser Actions

Config-driven handlers registered with the orchestrator.

| Action | Params | Description |
|--------|--------|-------------|
| `browser_navigate` | `url`, `wait_until`, `timeout` | Navigate to URL |
| `browser_click` | `selector`, `timeout`, `force` | Click element |
| `browser_fill` | `selector`, `value`, `timeout` | Fill input (clears first) |
| `browser_type` | `selector`, `text`, `delay` | Type with delays |
| `browser_select` | `selector`, `value` | Select dropdown option |
| `browser_wait` | `selector`, `state`, `timeout` | Wait for element |
| `browser_extract_text` | `selector` | Get element text |
| `browser_extract_attribute` | `selector`, `attribute` | Get attribute |
| `browser_extract_all` | `selector`, `attribute` | Extract from multiple elements |
| `browser_screenshot` | `path`, `full_page` | Take screenshot |
| `browser_evaluate` | `script`, `arg` | Execute JavaScript |
| `browser_scroll` | `direction`, `amount` | Scroll page |

### Example Rule with Browser Actions

```yaml
rules:
  - id: login_workflow
    name: Login Workflow
    triggers:
      - type: command
        conditions:
          command: "/login"
    actions:
      - type: browser_navigate
        params:
          url: "{{trigger.url}}"
          wait_until: networkidle

      - type: browser_fill
        params:
          selector: "#username"
          value: "{{trigger.username}}"

      - type: browser_fill
        params:
          selector: "#password"
          value: "{{trigger.password}}"

      - type: browser_click
        params:
          selector: "#submit"

      - type: browser_wait
        params:
          selector: ".dashboard"
          state: visible
          timeout: 10000
```

## Telegram Module

### Architecture

```
src/telegram/
├── bot.py          # Core bot functionality
├── handlers.py     # Command and callback handlers
└── formatter.py    # Human-like message formatting
```

### TelegramBot

Core bot with human-like interaction.

```python
from telegram.bot import TelegramBot, TelegramConfig

config = TelegramConfig(
    bot_token="your-token",
    allowed_user_ids=[123456789],  # Empty = allow all
    typing_delay_ms=500,
)

bot = TelegramBot(config)
await bot.start()

# Send message
await bot.send_message(chat_id, "Hello!")

# Send with inline buttons
buttons = bot.create_approval_buttons(approval_id=1)
await bot.send_message(chat_id, "Approve?", reply_markup=buttons)

# Send file
await bot.send_file(chat_id, "/path/to/file.pdf", caption="Report")

await bot.stop()
```

### TelegramHandlers

Connects Telegram commands to the orchestrator.

Built-in commands:

| Command | Description |
|---------|-------------|
| `/start` | Check bot status |
| `/help` | Show available commands |
| `/status` | System status |
| `/run <workflow> [--param=value]` | Execute workflow |
| `/workflows` | List available workflows |
| `/rules` | List loaded rules |
| `/pause` | Pause execution |
| `/resume` | Resume execution |
| `/quarantined` | Show quarantined tasks |
| `/reset <fingerprint>` | Reset failure |
| `/reset all` | Reset all failures |
| `/pending` | Show pending approvals |
| `/approve <id>` | Approve request |
| `/reject <id>` | Reject request |

### MessageFormatter

Human-readable message formatting.

```python
from telegram.formatter import MessageFormatter

fmt = MessageFormatter()

# Status message
msg = fmt.format_status("success", "Task completed", details="Took 5.2s")

# Error message
msg = fmt.format_error("Connection failed", context={"task_id": "abc"})

# Approval request
msg = fmt.format_approval_request(
    approval_id=1,
    action_type="delete",
    description="Delete user data",
)

# Workflow result
msg = fmt.format_workflow_result(
    workflow_id="export_data",
    success=True,
    steps_completed=5,
    total_steps=5,
    output={"file": "report.csv"},
)
```

### Command-Driven Workflow Activation

Workflows can be triggered via Telegram commands:

```yaml
# config/workflows/export.yaml
workflows:
  - id: export_data
    name: Export Data
    trigger_command: "/run export"

    parameters:
      format: csv

    steps:
      - id: navigate
        action: browser_navigate
        params:
          url: "https://app.example.com/export"

      - id: select_format
        action: browser_select
        params:
          selector: "#format"
          value: "{{format}}"

      - id: click_export
        action: browser_click
        params:
          selector: "#export-btn"
```

Trigger from Telegram:

```
/run export --format=json
```

### Escalation to Telegram

The supervisor sends escalations to a configured admin chat:

```python
# Set via environment
TELEGRAM_ADMIN_CHAT_ID=123456789
```

Escalation types:
- `approval_required` - Action needs approval
- `task_quarantined` - Task quarantined after repeated failures
- `global_failure_threshold` - System paused due to failure rate

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | - | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_USERS` | - | Comma-separated user IDs |
| `TELEGRAM_ADMIN_CHAT_ID` | - | Chat ID for escalations |
| `TELEGRAM_TYPING_DELAY` | 500 | Typing indicator delay (ms) |
| `BROWSER_HEADLESS` | true | Run browser headless |
| `BROWSER_DATA_DIR` | ./data/browser | Session storage |

### Framework Config

```yaml
# config/framework.yaml

telegram:
  enabled: true
  typing_delay_ms: 500
  message_chunk_size: 4000

resources:
  max_browser_pages: 1
```

## Docker Setup

```bash
# With Telegram
TELEGRAM_BOT_TOKEN=your-token docker-compose up

# Without Telegram (browser only)
docker-compose up
```

## Testing Browser Actions

```yaml
# config/workflows/browser_test.yaml
workflows:
  - id: browser_test
    name: Browser Test
    trigger_command: "/run test-browser"
    steps:
      - id: navigate
        action: browser_navigate
        params:
          url: "https://example.com"

      - id: screenshot
        action: browser_screenshot
        params:
          path: "/app/data/test.png"

      - id: extract
        action: browser_extract_text
        params:
          selector: "h1"
```

## Testing Telegram Commands

1. Start bot: `docker-compose up`
2. Find your bot on Telegram
3. Send `/start` to verify connection
4. Send `/status` to see system status
5. Send `/run test-browser` to execute workflow
6. Check `/app/data/test.png` for screenshot
