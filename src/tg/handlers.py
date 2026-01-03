"""
Telegram Handlers - Command and callback handlers for workflow activation.

Connects Telegram commands to the orchestrator for rule-driven activation.
"""

import asyncio
from typing import Any, Optional, Callable, Awaitable, List
from dataclasses import dataclass
import structlog

from tg.bot import TelegramBot, TelegramConfig
from tg.formatter import MessageFormatter

logger = structlog.get_logger()


# Type alias for supervisor interface
SupervisorInterface = Any  # Will be Supervisor when integrated


class TelegramHandlers:
    """
    Telegram command and callback handlers.

    Connects Telegram to:
    - Workflow activation via commands
    - Approval handling
    - Status queries
    - Manual reset commands
    """

    def __init__(
        self,
        bot: TelegramBot,
        supervisor: Optional[SupervisorInterface] = None,
    ):
        """
        Initialize handlers.

        Args:
            bot: TelegramBot instance
            supervisor: Supervisor instance for workflow execution
        """
        self.bot = bot
        self.supervisor = supervisor
        self.formatter = MessageFormatter()

        # Pending approval tracking
        self._pending_approvals: dict[int, dict] = {}

        # Register handlers
        self._register_handlers()

    def set_supervisor(self, supervisor: SupervisorInterface) -> None:
        """Set the supervisor (for late binding)."""
        self.supervisor = supervisor

    def _register_handlers(self) -> None:
        """Register all command handlers with bot."""
        # Core commands
        self.bot.register_command("run", self._handle_run)
        self.bot.register_command("status", self._handle_status)
        self.bot.register_command("workflows", self._handle_workflows)
        self.bot.register_command("rules", self._handle_rules)

        # Control commands
        self.bot.register_command("pause", self._handle_pause)
        self.bot.register_command("resume", self._handle_resume)

        # Failure management
        self.bot.register_command("quarantined", self._handle_quarantined)
        self.bot.register_command("reset", self._handle_reset)

        # Approval commands
        self.bot.register_command("approve", self._handle_approve)
        self.bot.register_command("reject", self._handle_reject)
        self.bot.register_command("pending", self._handle_pending)

        # Set callback handler for inline buttons
        self.bot.set_callback_handler(self._handle_callback)

    # ==================== Workflow Commands ====================

    async def _handle_run(
        self,
        chat_id: int,
        command: str,
        args: List[str],
    ) -> Optional[str]:
        """
        Handle /run command - execute a workflow.

        Usage: /run [workflow_name] [--param=value ...]
        """
        if not args:
            return "Usage: /run [workflow_name] [--param=value ...]"

        if not self.supervisor:
            return "✗ Supervisor not available"

        workflow_name = args[0]
        params = self._parse_args(args[1:])

        try:
            # Run workflow via supervisor
            task_id = await self.supervisor.run_workflow_by_command(
                f"/run {workflow_name}",
                parameters=params,
            )

            return (
                f"✓ <b>Workflow Started</b>\n\n"
                f"Workflow: <code>{workflow_name}</code>\n"
                f"Task ID: <code>{task_id}</code>\n"
                f"Parameters: {params or 'none'}"
            )

        except Exception as e:
            logger.exception("workflow_run_error", workflow=workflow_name)
            return f"✗ Failed to start workflow: {str(e)[:200]}"

    async def _handle_workflows(
        self,
        chat_id: int,
        command: str,
        args: List[str],
    ) -> Optional[str]:
        """Handle /workflows - list available workflows."""
        if not self.supervisor:
            return "✗ Supervisor not available"

        workflows = self.supervisor._workflows

        if not workflows:
            return "No workflows configured"

        lines = ["<b>Available Workflows</b>\n"]

        for wf in workflows:
            status = "✓" if wf.enabled else "✗"
            lines.append(f"{status} <code>{wf.id}</code>: {wf.name}")
            if wf.trigger_command:
                lines.append(f"   Command: <code>{wf.trigger_command}</code>")

        return "\n".join(lines)

    async def _handle_rules(
        self,
        chat_id: int,
        command: str,
        args: List[str],
    ) -> Optional[str]:
        """Handle /rules - list loaded rules."""
        if not self.supervisor:
            return "✗ Supervisor not available"

        rules = self.supervisor._rules

        if not rules:
            return "No rules configured"

        lines = ["<b>Loaded Rules</b>\n"]

        for rule in rules[:20]:  # Limit to 20
            status = "✓" if rule.enabled else "✗"
            lines.append(f"{status} <code>{rule.id}</code> (priority: {rule.priority})")

        if len(rules) > 20:
            lines.append(f"\n... and {len(rules) - 20} more")

        return "\n".join(lines)

    # ==================== Status Commands ====================

    async def _handle_status(
        self,
        chat_id: int,
        command: str,
        args: List[str],
    ) -> Optional[str]:
        """Handle /status - show system status."""
        if not self.supervisor:
            return "✗ Supervisor not available"

        try:
            status = await self.supervisor.get_status()

            return (
                f"<b>System Status</b>\n\n"
                f"State: <code>{status['state']}</code>\n"
                f"Uptime: {int(status['uptime_seconds'])}s\n"
                f"Circuit Breaker: <code>{status['circuit_breaker']}</code>\n"
                f"Queue Size: {status['queue']['queue_size']}\n"
                f"Running Tasks: {status['queue']['running_count']}\n"
                f"Quarantined: {status['quarantined_count']}\n"
                f"Pending Approvals: {status['pending_approvals']}\n"
                f"Rules: {status['rules_loaded']}\n"
                f"Workflows: {status['workflows_loaded']}"
            )

        except Exception as e:
            return f"✗ Error getting status: {str(e)[:200]}"

    async def _handle_quarantined(
        self,
        chat_id: int,
        command: str,
        args: List[str],
    ) -> Optional[str]:
        """Handle /quarantined - show quarantined tasks."""
        if not self.supervisor:
            return "✗ Supervisor not available"

        try:
            quarantined = await self.supervisor.state_manager.get_quarantined_tasks()

            if not quarantined:
                return "✓ No quarantined tasks"

            import html
            lines = ["<b>Quarantined Tasks</b>\n"]

            for task in quarantined[:10]:
                task_id_escaped = html.escape(str(task.task_id))
                lines.append(
                    f"• <code>{task.fingerprint[:12]}</code>\n"
                    f"  Task: {task_id_escaped}\n"
                    f"  Failures: {task.count}"
                )

            if len(quarantined) > 10:
                lines.append(f"\n... and {len(quarantined) - 10} more")

            lines.append("\nUse /reset [fingerprint] to reset")

            return "\n".join(lines)

        except Exception as e:
            return f"✗ Error: {str(e)[:200]}"

    # ==================== Control Commands ====================

    async def _handle_pause(
        self,
        chat_id: int,
        command: str,
        args: List[str],
    ) -> Optional[str]:
        """Handle /pause - pause execution."""
        if not self.supervisor:
            return "✗ Supervisor not available"

        try:
            await self.supervisor.pause()
            return "✓ Execution paused"
        except Exception as e:
            return f"✗ Error: {str(e)[:200]}"

    async def _handle_resume(
        self,
        chat_id: int,
        command: str,
        args: List[str],
    ) -> Optional[str]:
        """Handle /resume - resume execution."""
        if not self.supervisor:
            return "✗ Supervisor not available"

        try:
            await self.supervisor.resume()
            return "✓ Execution resumed"
        except Exception as e:
            return f"✗ Error: {str(e)[:200]}"

    async def _handle_reset(
        self,
        chat_id: int,
        command: str,
        args: List[str],
    ) -> Optional[str]:
        """
        Handle /reset - reset failure/quarantine.

        Usage: /reset [fingerprint] or /reset all
        """
        if not self.supervisor:
            return "✗ Supervisor not available"

        if not args:
            return "Usage: /reset [fingerprint] or /reset all"

        try:
            if args[0].lower() == "all":
                count = await self.supervisor.reset_all_failures()
                return f"✓ Reset {count} failure records"
            else:
                fingerprint = args[0]
                success = await self.supervisor.reset_failure(fingerprint)
                if success:
                    return f"✓ Reset failure: {fingerprint}"
                else:
                    return f"✗ Fingerprint not found: {fingerprint}"

        except Exception as e:
            return f"✗ Error: {str(e)[:200]}"

    # ==================== Approval Commands ====================

    async def _handle_pending(
        self,
        chat_id: int,
        command: str,
        args: List[str],
    ) -> Optional[str]:
        """Handle /pending - show pending approvals."""
        if not self.supervisor:
            return "✗ Supervisor not available"

        try:
            approvals = await self.supervisor.state_manager.get_pending_approvals()

            if not approvals:
                return "✓ No pending approvals"

            import html
            lines = ["<b>Pending Approvals</b>\n"]

            for approval in approvals:
                action_type = html.escape(str(approval.get('action_type', 'unknown')))
                description = html.escape(str(approval.get('description', ''))[:100])
                lines.append(
                    f"ID: <code>{approval['id']}</code>\n"
                    f"Type: {action_type}\n"
                    f"Description: {description}\n"
                )

            lines.append("\nUse /approve [id] or /reject [id]")

            return "\n".join(lines)

        except Exception as e:
            return f"✗ Error: {str(e)[:200]}"

    async def _handle_approve(
        self,
        chat_id: int,
        command: str,
        args: List[str],
    ) -> Optional[str]:
        """Handle /approve [id] - approve a request."""
        if not args:
            return "Usage: /approve [approval_id]"

        if not self.supervisor:
            return "✗ Supervisor not available"

        try:
            approval_id = int(args[0])
            success = await self.supervisor.state_manager.resolve_approval(
                approval_id,
                resolution="approved",
                resolved_by=f"telegram:{chat_id}",
            )

            if success:
                return f"✓ Approved: {approval_id}"
            else:
                return f"✗ Approval not found or already resolved: {approval_id}"

        except ValueError:
            return "✗ Invalid approval ID"
        except Exception as e:
            return f"✗ Error: {str(e)[:200]}"

    async def _handle_reject(
        self,
        chat_id: int,
        command: str,
        args: List[str],
    ) -> Optional[str]:
        """Handle /reject [id] - reject a request."""
        if not args:
            return "Usage: /reject [approval_id]"

        if not self.supervisor:
            return "✗ Supervisor not available"

        try:
            approval_id = int(args[0])
            success = await self.supervisor.state_manager.resolve_approval(
                approval_id,
                resolution="rejected",
                resolved_by=f"telegram:{chat_id}",
            )

            if success:
                return f"✓ Rejected: {approval_id}"
            else:
                return f"✗ Approval not found or already resolved: {approval_id}"

        except ValueError:
            return "✗ Invalid approval ID"
        except Exception as e:
            return f"✗ Error: {str(e)[:200]}"

    # ==================== Callback Handler ====================

    async def _handle_callback(
        self,
        chat_id: int,
        callback_data: str,
    ) -> Optional[str]:
        """Handle callback queries from inline buttons."""
        parts = callback_data.split(":", 1)
        if len(parts) != 2:
            return "✗ Invalid callback data"

        action, data = parts

        if action == "approve":
            return await self._handle_approve(chat_id, "approve", [data])
        elif action == "reject":
            return await self._handle_reject(chat_id, "reject", [data])
        elif action == "confirm":
            return f"✓ Confirmed: {data}"
        elif action == "cancel":
            return f"✗ Cancelled: {data}"
        else:
            return f"Unknown action: {action}"

    # ==================== Escalation Interface ====================

    async def send_escalation(
        self,
        chat_id: int,
        escalation_type: str,
        data: dict,
    ) -> bool:
        """
        Send an escalation notification.

        Called by supervisor when escalation is needed.

        Args:
            chat_id: Target chat ID
            escalation_type: Type of escalation
            data: Escalation data
        """
        if escalation_type == "approval_required":
            message = self.formatter.format_approval_request(
                approval_id=data.get("approval_id", 0),
                action_type=data.get("action_type", "unknown"),
                description=data.get("description", ""),
                context=data.get("context"),
            )

            buttons = self.bot.create_approval_buttons(data.get("approval_id", 0))
            return await self.bot.send_formatted(chat_id, message, reply_markup=buttons)

        elif escalation_type == "task_quarantined":
            message = self.formatter.format_error(
                error=f"Task quarantined after {data.get('failure_count', 'multiple')} failures",
                context={
                    "task_id": data.get("task_id"),
                    "fingerprint": data.get("fingerprint"),
                    "error": data.get("error"),
                },
            )
            return await self.bot.send_formatted(chat_id, message)

        elif escalation_type == "global_failure_threshold":
            message = self.formatter.format_status(
                status="warning",
                message="Global failure threshold exceeded",
                details=f"Failures: {data.get('failures')}/{data.get('threshold')}. Action: {data.get('action')}",
            )
            return await self.bot.send_formatted(chat_id, message)

        elif escalation_type == "task_error":
            import html
            error_msg = html.escape(str(data.get("error", "Unknown error"))[:500])
            message = self.formatter.format_error(
                error=f"Task failed: {error_msg}",
                context={
                    "task_id": data.get("task_id"),
                    "workflow_id": data.get("workflow_id"),
                    "action": data.get("action"),
                    "severity": data.get("severity"),
                    "category": data.get("category"),
                },
            )
            return await self.bot.send_formatted(chat_id, message)

        else:
            message = self.formatter.format_status(
                status="warning",
                message=f"Escalation: {escalation_type}",
                details=str(data),
            )
            return await self.bot.send_formatted(chat_id, message)

    async def send_workflow_result(
        self,
        chat_id: int,
        workflow_id: str,
        success: bool,
        steps_completed: int,
        total_steps: int,
        output: Optional[dict] = None,
        error: Optional[str] = None,
    ) -> bool:
        """Send workflow execution result."""
        message = self.formatter.format_workflow_result(
            workflow_id=workflow_id,
            success=success,
            steps_completed=steps_completed,
            total_steps=total_steps,
            output=output,
            error=error,
        )
        return await self.bot.send_formatted(chat_id, message)

    # ==================== Utility ====================

    def _parse_args(self, args: List[str]) -> dict:
        """Parse command-line style arguments."""
        result = {}
        for arg in args:
            if "=" in arg:
                key, value = arg.lstrip("-").split("=", 1)
                result[key] = value
            elif arg.startswith("--"):
                result[arg[2:]] = True
        return result
