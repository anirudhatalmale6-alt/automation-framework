"""
Main entry point for the automation framework.

Starts the supervisor, health server, browser, Telegram bot, and signal handlers.
"""

import asyncio
import signal
import sys
import os
import logging
from typing import Optional

import structlog
from aiohttp import web

from core.config import ConfigLoader, FrameworkConfig
from core.state import StateManager
from orchestrator.supervisor import Supervisor
from rules.engine import RulesEngine


# Configure standard library logging first
logging.basicConfig(
    format="%(message)s",
    stream=sys.stdout,
    level=logging.INFO,
)

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
        self.voice_provider = None
        self.voice_handlers = None
        self.hot_reloader = None
        self.approval_guard = None
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

        # Initialize Voice if enabled
        await self._init_voice()

        # Initialize Hot-Reload
        await self._init_hot_reload()

        # Initialize Approval Guard
        await self._init_approval_guard()

        # Start health server
        health_port = int(os.getenv("HEALTH_PORT", "8080"))
        self.health_server = HealthServer(self.supervisor, port=health_port)
        await self.health_server.start()

        # Send Telegram alert if there were startup config errors
        await self._notify_startup_errors()

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
            from tg.bot import TelegramBot, TelegramConfig
            from tg.handlers import TelegramHandlers

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

    async def _init_voice(self) -> None:
        """Initialize voice provider if configured."""
        voice_provider_type = os.getenv("VOICE_PROVIDER", "")
        if not voice_provider_type:
            logger.info("voice_disabled", reason="no provider configured")
            return

        try:
            from voice.interface import VoiceConfig, MockVoiceProvider
            from voice.handlers import VoiceHandlers

            # Build config from environment
            config = VoiceConfig(
                provider_name=voice_provider_type,
                api_key=os.getenv("VOICE_API_KEY"),
                api_secret=os.getenv("VOICE_API_SECRET"),
                account_id=os.getenv("VOICE_ACCOUNT_ID"),
                default_timeout_seconds=int(os.getenv("VOICE_TIMEOUT", "30")),
                recording_enabled=os.getenv("VOICE_RECORDING", "false").lower() == "true",
                tts_voice=os.getenv("VOICE_TTS_VOICE"),
                tts_language=os.getenv("VOICE_TTS_LANGUAGE", "en-US"),
            )

            # Use mock provider for testing, or implement real provider
            if voice_provider_type.lower() == "mock":
                self.voice_provider = MockVoiceProvider(config)
            else:
                # For real providers, users should implement VoiceProvider
                # and register it here based on provider_type
                logger.warning(
                    "voice_provider_not_implemented",
                    provider=voice_provider_type,
                    hint="Implement VoiceProvider for your provider or use 'mock' for testing",
                )
                return

            self.voice_handlers = VoiceHandlers(self.voice_provider, self.supervisor)
            await self.voice_handlers.initialize()

            # Register voice action handlers
            voice_actions = self.voice_handlers.get_action_handlers()
            for action_name, handler in voice_actions.items():
                self.supervisor.register_action(action_name, handler)
                logger.info("voice_action_registered", action=action_name)

            logger.info("voice_started", provider=voice_provider_type, actions=list(voice_actions.keys()))

        except ImportError as e:
            logger.warning("voice_module_unavailable", error=str(e))
        except Exception as e:
            logger.warning("voice_init_failed", error=str(e))

    async def _init_hot_reload(self) -> None:
        """Initialize hot-reload for config files."""
        hot_reload_enabled = os.getenv("HOT_RELOAD_ENABLED", "true").lower() == "true"
        if not hot_reload_enabled:
            logger.info("hot_reload_disabled")
            return

        try:
            from core.hot_reload import HotReloader, HotReloadConfig

            config = HotReloadConfig(
                enabled=True,
                check_interval_seconds=int(os.getenv("HOT_RELOAD_INTERVAL", "10")),
                debounce_seconds=float(os.getenv("HOT_RELOAD_DEBOUNCE", "2.0")),
                notify_on_error=True,
                notify_on_success=os.getenv("HOT_RELOAD_NOTIFY_SUCCESS", "false").lower() == "true",
            )

            self.hot_reloader = HotReloader(
                config_loader=self.supervisor.config_loader,
                rules_dir=self.supervisor.config.rules_directory,
                workflows_dir=self.supervisor.config.workflows_directory,
                config=config,
            )

            # Set reload callback
            self.hot_reloader.set_reload_callback(self.supervisor.apply_hot_reload)

            # Set error callback for Telegram notification
            if self.telegram_bot and self.telegram_handlers:
                admin_chat_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID")
                if admin_chat_id:
                    admin_id = int(admin_chat_id)

                    async def hot_reload_error_handler(message: str, errors: list):
                        import html
                        # Escape HTML special chars in error messages
                        escaped_errors = [html.escape(str(e)) for e in errors[:5]]
                        error_text = (
                            f"⚠️ <b>Config Reload Failed</b>\n\n"
                            f"{html.escape(message)}\n\n"
                            f"<b>Errors:</b>\n" +
                            "\n".join(f"• {e}" for e in escaped_errors)
                        )
                        await self.telegram_bot.send_message(admin_id, error_text)
                        logger.info("hot_reload_telegram_alert_sent", admin_id=admin_id)

                    self.hot_reloader.set_error_callback(hot_reload_error_handler)

            await self.hot_reloader.start()
            logger.info("hot_reload_started", interval=config.check_interval_seconds)

        except Exception as e:
            logger.warning("hot_reload_init_failed", error=str(e))

    async def _init_approval_guard(self) -> None:
        """Initialize approval fatigue guard."""
        approval_guard_enabled = os.getenv("APPROVAL_GUARD_ENABLED", "true").lower() == "true"
        if not approval_guard_enabled:
            logger.info("approval_guard_disabled")
            return

        try:
            from core.approval_guard import ApprovalFatigueGuard, ApprovalGuardConfig

            config = ApprovalGuardConfig(
                enabled=True,
                max_approvals_per_hour=int(os.getenv("APPROVAL_MAX_PER_HOUR", "20")),
                max_approvals_per_minute=int(os.getenv("APPROVAL_MAX_PER_MINUTE", "5")),
                burst_limit=int(os.getenv("APPROVAL_BURST_LIMIT", "3")),
                burst_window_seconds=int(os.getenv("APPROVAL_BURST_WINDOW", "60")),
                defer_low_priority=os.getenv("APPROVAL_DEFER_LOW", "true").lower() == "true",
                defer_duration_minutes=int(os.getenv("APPROVAL_DEFER_MINUTES", "30")),
            )

            self.approval_guard = ApprovalFatigueGuard(config)

            # Set deferred callback for Telegram notification
            if self.telegram_bot and self.telegram_handlers:
                admin_chat_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID")
                if admin_chat_id:
                    admin_id = int(admin_chat_id)

                    async def deferred_approvals_handler(requests: list):
                        text = (
                            f"📋 <b>Deferred Approvals Ready</b>\n\n"
                            f"{len(requests)} approval(s) were deferred and are now ready:\n\n"
                        )
                        for req in requests[:5]:
                            text += f"• {req.description}\n"
                        if len(requests) > 5:
                            text += f"\n...and {len(requests) - 5} more"
                        await self.telegram_bot.send_message(admin_id, text)

                    self.approval_guard.set_deferred_callback(deferred_approvals_handler)

            await self.approval_guard.start()
            logger.info(
                "approval_guard_started",
                max_per_hour=config.max_approvals_per_hour,
                max_per_minute=config.max_approvals_per_minute,
            )

        except Exception as e:
            logger.warning("approval_guard_init_failed", error=str(e))

    async def _notify_startup_errors(self) -> None:
        """Send Telegram notification if there were config errors at startup."""
        if not self.supervisor._startup_errors:
            return

        if not self.telegram_bot:
            logger.warning(
                "startup_errors_not_notified",
                reason="telegram not configured",
                errors=self.supervisor._startup_errors,
            )
            return

        admin_chat_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID")
        if not admin_chat_id:
            logger.warning(
                "startup_errors_not_notified",
                reason="TELEGRAM_ADMIN_CHAT_ID not set",
                errors=self.supervisor._startup_errors,
            )
            return

        try:
            import html
            admin_id = int(admin_chat_id)
            escaped_errors = [html.escape(str(e)) for e in self.supervisor._startup_errors[:5]]
            error_text = (
                f"⚠️ <b>Config Errors at Startup</b>\n\n"
                f"The system started with config errors. Some rules/workflows may not be loaded.\n\n"
                f"<b>Errors:</b>\n" +
                "\n".join(f"• {e}" for e in escaped_errors)
            )
            if len(self.supervisor._startup_errors) > 5:
                error_text += f"\n\n...and {len(self.supervisor._startup_errors) - 5} more errors"

            await self.telegram_bot.send_message(admin_id, error_text)
            logger.info("startup_errors_notified", admin_id=admin_id, count=len(self.supervisor._startup_errors))
        except Exception as e:
            logger.error("startup_errors_notify_failed", error=str(e))

    async def stop(self) -> None:
        """Stop all components."""
        logger.info("application_stopping")

        # Stop Hot-Reload
        if self.hot_reloader:
            await self.hot_reloader.stop()

        # Stop Approval Guard
        if self.approval_guard:
            await self.approval_guard.stop()

        # Stop Voice
        if self.voice_handlers:
            await self.voice_handlers.shutdown()

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

        # Telegram send action
        async def telegram_send_handler(payload: dict) -> dict:
            """Send message to Telegram admin."""
            if not self.telegram_bot:
                return {"success": False, "error": "Telegram not configured"}

            admin_chat_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID")
            if not admin_chat_id:
                return {"success": False, "error": "TELEGRAM_ADMIN_CHAT_ID not set"}

            message = payload.get("message", "")
            chat_id = payload.get("chat_id", int(admin_chat_id))

            try:
                await self.telegram_bot.send_message(chat_id, message)
                return {"success": True, "message": message, "chat_id": chat_id}
            except Exception as e:
                return {"success": False, "error": str(e)}

        self.supervisor.register_action("telegram_send", telegram_send_handler)


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
