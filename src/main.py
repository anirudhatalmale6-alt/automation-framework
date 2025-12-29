"""
Main entry point for the automation framework.

Starts the supervisor, health server, browser, Telegram bot, and signal handlers.
"""

import asyncio
import signal
import sys
import os
from typing import Optional

import structlog
from aiohttp import web

from core.config import ConfigLoader, FrameworkConfig
from core.state import StateManager
from orchestrator.supervisor import Supervisor
from rules.engine import RulesEngine


# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer() if os.getenv("LOG_FORMAT") == "json"
        else structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()


class HealthServer:
    """Simple HTTP health check server."""

    def __init__(self, supervisor: Supervisor, port: int = 8080):
        self.supervisor = supervisor
        self.port = port
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None

    async def start(self) -> None:
        """Start the health server."""
        self._app = web.Application()
        self._app.router.add_get("/health", self._health_handler)
        self._app.router.add_get("/status", self._status_handler)
        self._app.router.add_get("/ready", self._ready_handler)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()

        logger.info("health_server_started", port=self.port)

    async def stop(self) -> None:
        """Stop the health server."""
        if self._runner:
            await self._runner.cleanup()
            logger.info("health_server_stopped")

    async def _health_handler(self, request: web.Request) -> web.Response:
        """Basic health check - is the process alive."""
        return web.json_response({"status": "healthy"})

    async def _ready_handler(self, request: web.Request) -> web.Response:
        """Readiness check - is the supervisor ready for work."""
        status = await self.supervisor.get_status()

        if status["state"] in ("running", "paused"):
            return web.json_response({"status": "ready", "state": status["state"]})
        else:
            return web.json_response(
                {"status": "not_ready", "state": status["state"]},
                status=503,
            )

    async def _status_handler(self, request: web.Request) -> web.Response:
        """Detailed status information."""
        status = await self.supervisor.get_status()
        return web.json_response(status)


class Application:
    """Main application container."""

    def __init__(self):
        self.supervisor: Optional[Supervisor] = None
        self.health_server: Optional[HealthServer] = None
        self.rules_engine: Optional[RulesEngine] = None
        self.browser_manager = None
        self.browser_actions = None
        self.telegram_bot = None
        self.telegram_handlers = None
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """Start all components."""
        logger.info("application_starting")

        # Determine config path
        config_path = os.getenv("CONFIG_PATH", "./config/framework.yaml")
        data_dir = os.getenv("DATA_DIR", "./data")

        # Create and start supervisor
        self.supervisor = Supervisor(
            config_path=config_path if os.path.exists(config_path) else None,
            data_dir=data_dir,
        )
        await self.supervisor.start()

        # Initialize rules engine
        self.rules_engine = RulesEngine(self.supervisor.state_manager)
        self.rules_engine.load_rules(self.supervisor._rules)

        # Register the workflow action handler
        self.supervisor.register_action("_workflow", self._workflow_handler)

        # Register built-in action handlers
        self._register_builtin_actions()

        # Initialize browser if enabled
        await self._init_browser()

        # Initialize Telegram if enabled
        await self._init_telegram()

        # Start health server
        health_port = int(os.getenv("HEALTH_PORT", "8080"))
        self.health_server = HealthServer(self.supervisor, port=health_port)
        await self.health_server.start()

        logger.info("application_started")

    async def _init_browser(self) -> None:
        """Initialize browser automation if Playwright is available."""
        try:
            from browser.manager import BrowserManager
            from browser.actions import BrowserActions

            headless = os.getenv("BROWSER_HEADLESS", "true").lower() == "true"
            user_data_dir = os.getenv("BROWSER_DATA_DIR", "./data/browser")

            self.browser_manager = BrowserManager(
                headless=headless,
                user_data_dir=user_data_dir,
                max_pages=1,
            )

            # Initialize browser lazily (on first use)
            self.browser_actions = BrowserActions(self.browser_manager)

            # Register browser action handlers
            for action_name, handler in self.browser_actions.get_handlers().items():
                self.supervisor.register_action(action_name, handler)

            logger.info("browser_actions_registered")

        except ImportError:
            logger.warning("browser_module_unavailable", reason="playwright not installed")
        except Exception as e:
            logger.warning("browser_init_failed", error=str(e))

    async def _init_telegram(self) -> None:
        """Initialize Telegram bot if configured."""
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not bot_token:
            logger.info("telegram_disabled", reason="no token configured")
            return

        try:
            from telegram.bot import TelegramBot, TelegramConfig
            from telegram.handlers import TelegramHandlers

            # Parse allowed users
            allowed_users_str = os.getenv("TELEGRAM_ALLOWED_USERS", "")
            allowed_users = []
            if allowed_users_str:
                allowed_users = [int(x.strip()) for x in allowed_users_str.split(",") if x.strip()]

            config = TelegramConfig(
                bot_token=bot_token,
                allowed_user_ids=allowed_users,
                typing_delay_ms=int(os.getenv("TELEGRAM_TYPING_DELAY", "500")),
            )

            self.telegram_bot = TelegramBot(config)
            self.telegram_handlers = TelegramHandlers(self.telegram_bot, self.supervisor)

            # Set escalation handler
            admin_chat_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID")
            if admin_chat_id:
                admin_id = int(admin_chat_id)

                async def escalation_handler(event_type: str, data: dict):
                    await self.telegram_handlers.send_escalation(admin_id, event_type, data)

                self.supervisor.set_escalation_handler(escalation_handler)

            await self.telegram_bot.start()
            logger.info("telegram_started")

        except ImportError:
            logger.warning("telegram_module_unavailable", reason="python-telegram-bot not installed")
        except Exception as e:
            logger.warning("telegram_init_failed", error=str(e))

    async def stop(self) -> None:
        """Stop all components."""
        logger.info("application_stopping")

        # Stop Telegram
        if self.telegram_bot:
            await self.telegram_bot.stop()

        # Stop browser
        if self.browser_manager:
            await self.browser_manager.shutdown()

        # Stop health server
        if self.health_server:
            await self.health_server.stop()

        # Stop supervisor
        if self.supervisor:
            await self.supervisor.stop()

        logger.info("application_stopped")

    async def run(self) -> None:
        """Run until shutdown signal."""
        await self._shutdown_event.wait()

    def request_shutdown(self) -> None:
        """Request graceful shutdown."""
        self._shutdown_event.set()

    async def _workflow_handler(self, payload: dict) -> dict:
        """Handle workflow execution."""
        result = await self.supervisor.workflow_executor.execute_workflow(
            workflow_id=payload["workflow_id"],
            steps=payload["steps"],
            parameters=payload["parameters"],
            on_error=payload.get("on_error", "stop"),
        )

        if result.success:
            return result.output
        else:
            raise Exception(result.error.message if result.error else "Workflow failed")

    def _register_builtin_actions(self) -> None:
        """Register built-in action handlers with supervisor."""

        async def echo_handler(payload: dict) -> dict:
            """Echo action for testing."""
            return {"echo": payload}

        async def http_request_handler(payload: dict) -> dict:
            """Simple HTTP request action."""
            import httpx

            url = payload.get("url")
            method = payload.get("method", "GET").upper()
            headers = payload.get("headers", {})
            body = payload.get("body")
            timeout = payload.get("timeout", 30)

            async with httpx.AsyncClient() as client:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=body if isinstance(body, dict) else None,
                    content=body if isinstance(body, str) else None,
                    timeout=timeout,
                )

                return {
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                    "body": response.text[:10000],  # Limit response size
                }

        async def delay_handler(payload: dict) -> dict:
            """Delay execution."""
            seconds = payload.get("seconds", 1)
            await asyncio.sleep(float(seconds))
            return {"delayed_seconds": seconds}

        async def log_handler(payload: dict) -> dict:
            """Log a message."""
            message = payload.get("message", "")
            level = payload.get("level", "info")
            logger.log(level.upper(), message)
            return {"logged": True, "message": message}

        self.supervisor.register_action("echo", echo_handler)
        self.supervisor.register_action("http_request", http_request_handler)
        self.supervisor.register_action("delay", delay_handler)
        self.supervisor.register_action("log", log_handler)


async def main() -> None:
    """Main entry point."""
    app = Application()

    # Setup signal handlers
    loop = asyncio.get_running_loop()

    def signal_handler():
        logger.info("shutdown_signal_received")
        app.request_shutdown()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await app.start()
        await app.run()
    except Exception:
        logger.exception("application_error")
        sys.exit(1)
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
