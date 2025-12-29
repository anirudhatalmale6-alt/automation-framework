"""
Telegram Bot - Core bot functionality.

Provides basic Telegram integration without business-specific logic.
All behavior is config-driven.
"""

import asyncio
import os
from typing import Any, Optional, Callable, Awaitable, List
from dataclasses import dataclass
import structlog

try:
    from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import (
        Application,
        CommandHandler,
        MessageHandler,
        CallbackQueryHandler,
        filters,
        ContextTypes,
    )
    from telegram.constants import ParseMode
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    Update = Any
    Bot = Any
    ContextTypes = Any

from .formatter import MessageFormatter, FormattedMessage

logger = structlog.get_logger()


@dataclass
class TelegramConfig:
    """Telegram bot configuration."""
    bot_token: str
    allowed_user_ids: List[int] = None  # Empty = allow all
    typing_delay_ms: int = 500
    message_chunk_size: int = 4000

    def __post_init__(self):
        if self.allowed_user_ids is None:
            self.allowed_user_ids = []


class TelegramBot:
    """
    Telegram bot with human-like interaction.

    Features:
    - Basic message send/receive
    - Inline buttons for approvals
    - File upload/download hooks
    - Typing indicator simulation
    - Message chunking for long content
    """

    def __init__(self, config: TelegramConfig):
        """
        Initialize Telegram bot.

        Args:
            config: Bot configuration
        """
        if not TELEGRAM_AVAILABLE:
            raise RuntimeError("python-telegram-bot not installed. Run: pip install python-telegram-bot")

        self.config = config
        self.formatter = MessageFormatter(chunk_size=config.message_chunk_size)

        self._app: Optional[Application] = None
        self._bot: Optional[Bot] = None
        self._running = False

        # Callbacks
        self._command_handlers: dict[str, Callable] = {}
        self._message_handler: Optional[Callable] = None
        self._callback_handler: Optional[Callable] = None

    async def start(self) -> None:
        """Start the bot."""
        if self._running:
            return

        logger.info("telegram_bot_starting")

        # Build application
        self._app = Application.builder().token(self.config.bot_token).build()
        self._bot = self._app.bot

        # Register internal handlers
        self._app.add_handler(CommandHandler("start", self._handle_start))
        self._app.add_handler(CommandHandler("help", self._handle_help))
        self._app.add_handler(CommandHandler("status", self._handle_status))

        # Generic command handler (for custom commands)
        self._app.add_handler(MessageHandler(
            filters.COMMAND & ~filters.COMMAND.as_reply,
            self._handle_command
        ))

        # Message handler
        self._app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            self._handle_message
        ))

        # Callback query handler (for inline buttons)
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))

        # Initialize and start polling
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()

        self._running = True
        logger.info("telegram_bot_started")

    async def stop(self) -> None:
        """Stop the bot."""
        if not self._running:
            return

        logger.info("telegram_bot_stopping")

        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

        self._running = False
        logger.info("telegram_bot_stopped")

    # ==================== Message Sending ====================

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Optional[Any] = None,
        disable_notification: bool = False,
    ) -> bool:
        """
        Send a text message.

        Args:
            chat_id: Target chat ID
            text: Message text
            parse_mode: HTML or Markdown
            reply_markup: Optional inline keyboard
            disable_notification: Send silently

        Returns:
            True if sent successfully
        """
        if not self._bot:
            logger.error("telegram_not_initialized")
            return False

        try:
            # Simulate typing for human-like behavior
            if self.config.typing_delay_ms > 0:
                await self._bot.send_chat_action(chat_id, "typing")
                await asyncio.sleep(self.config.typing_delay_ms / 1000)

            # Chunk long messages
            chunks = self.formatter._chunk_text(text)

            for chunk in chunks:
                await self._bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup if chunk == chunks[-1] else None,
                    disable_notification=disable_notification,
                )

                if len(chunks) > 1:
                    await asyncio.sleep(0.5)  # Brief delay between chunks

            return True

        except Exception as e:
            logger.error("telegram_send_error", error=str(e), chat_id=chat_id)
            return False

    async def send_formatted(
        self,
        chat_id: int,
        message: FormattedMessage,
        reply_markup: Optional[Any] = None,
    ) -> bool:
        """
        Send a pre-formatted message.

        Args:
            chat_id: Target chat ID
            message: FormattedMessage object
            reply_markup: Optional inline keyboard
        """
        success = True
        for i, chunk in enumerate(message.chunks):
            result = await self.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=message.parse_mode,
                reply_markup=reply_markup if i == len(message.chunks) - 1 else None,
            )
            success = success and result

        return success

    async def send_file(
        self,
        chat_id: int,
        file_path: str,
        caption: Optional[str] = None,
    ) -> bool:
        """
        Send a file.

        Args:
            chat_id: Target chat ID
            file_path: Path to file
            caption: Optional caption

        Returns:
            True if sent successfully
        """
        if not self._bot:
            return False

        try:
            with open(file_path, 'rb') as f:
                await self._bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    caption=caption,
                )
            return True

        except Exception as e:
            logger.error("telegram_file_send_error", error=str(e))
            return False

    async def send_photo(
        self,
        chat_id: int,
        photo: bytes,
        caption: Optional[str] = None,
    ) -> bool:
        """
        Send a photo (screenshot).

        Args:
            chat_id: Target chat ID
            photo: Photo bytes
            caption: Optional caption

        Returns:
            True if sent successfully
        """
        if not self._bot:
            return False

        try:
            await self._bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=caption,
            )
            return True

        except Exception as e:
            logger.error("telegram_photo_send_error", error=str(e))
            return False

    # ==================== Approval Buttons ====================

    def create_approval_buttons(
        self,
        approval_id: int,
    ) -> InlineKeyboardMarkup:
        """
        Create inline keyboard for approval request.

        Args:
            approval_id: Approval request ID

        Returns:
            InlineKeyboardMarkup with approve/reject buttons
        """
        keyboard = [
            [
                InlineKeyboardButton("✓ Approve", callback_data=f"approve:{approval_id}"),
                InlineKeyboardButton("✗ Reject", callback_data=f"reject:{approval_id}"),
            ]
        ]
        return InlineKeyboardMarkup(keyboard)

    def create_confirm_buttons(
        self,
        action_id: str,
    ) -> InlineKeyboardMarkup:
        """
        Create confirm/cancel buttons.

        Args:
            action_id: Action identifier

        Returns:
            InlineKeyboardMarkup with confirm/cancel buttons
        """
        keyboard = [
            [
                InlineKeyboardButton("Confirm", callback_data=f"confirm:{action_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"cancel:{action_id}"),
            ]
        ]
        return InlineKeyboardMarkup(keyboard)

    def create_menu_buttons(
        self,
        options: List[tuple[str, str]],
        columns: int = 2,
    ) -> InlineKeyboardMarkup:
        """
        Create menu with multiple buttons.

        Args:
            options: List of (label, callback_data) tuples
            columns: Buttons per row

        Returns:
            InlineKeyboardMarkup with menu buttons
        """
        keyboard = []
        row = []

        for label, data in options:
            row.append(InlineKeyboardButton(label, callback_data=data))
            if len(row) >= columns:
                keyboard.append(row)
                row = []

        if row:
            keyboard.append(row)

        return InlineKeyboardMarkup(keyboard)

    # ==================== Handler Registration ====================

    def register_command(
        self,
        command: str,
        handler: Callable[[int, str, List[str]], Awaitable[Optional[str]]],
    ) -> None:
        """
        Register a command handler.

        Args:
            command: Command name (without /)
            handler: Async function(chat_id, command, args) -> Optional response
        """
        self._command_handlers[command.lower()] = handler

    def set_message_handler(
        self,
        handler: Callable[[int, str], Awaitable[Optional[str]]],
    ) -> None:
        """
        Set handler for non-command messages.

        Args:
            handler: Async function(chat_id, text) -> Optional response
        """
        self._message_handler = handler

    def set_callback_handler(
        self,
        handler: Callable[[int, str], Awaitable[Optional[str]]],
    ) -> None:
        """
        Set handler for callback queries (button presses).

        Args:
            handler: Async function(chat_id, callback_data) -> Optional response
        """
        self._callback_handler = handler

    # ==================== Internal Handlers ====================

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized")
            return

        await update.message.reply_html(
            "✓ Bot is running\n\n"
            "Use /help to see available commands"
        )

    async def _handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command."""
        if not self._is_authorized(update.effective_user.id):
            return

        commands = ["/start - Check bot status", "/help - Show this help", "/status - Show system status"]

        # Add registered commands
        for cmd in self._command_handlers:
            commands.append(f"/{cmd}")

        await update.message.reply_html(
            "<b>Available Commands</b>\n\n" + "\n".join(commands)
        )

    async def _handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status command."""
        if not self._is_authorized(update.effective_user.id):
            return

        # Status will be filled by the handler registration
        if "status" in self._command_handlers:
            response = await self._command_handlers["status"](
                update.effective_chat.id,
                "status",
                []
            )
            if response:
                await update.message.reply_html(response)
        else:
            await update.message.reply_html("✓ System operational")

    async def _handle_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle custom commands."""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("Unauthorized")
            return

        # Parse command and args
        text = update.message.text
        parts = text.split()
        command = parts[0].lstrip("/").lower()
        args = parts[1:]

        # Check if we have a handler
        handler = self._command_handlers.get(command)
        if handler:
            try:
                response = await handler(update.effective_chat.id, command, args)
                if response:
                    await update.message.reply_html(response)
            except Exception as e:
                logger.exception("command_handler_error", command=command)
                await update.message.reply_text(f"Error: {str(e)[:200]}")
        else:
            await update.message.reply_text(f"Unknown command: /{command}")

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle non-command messages."""
        if not self._is_authorized(update.effective_user.id):
            return

        if self._message_handler:
            try:
                response = await self._message_handler(
                    update.effective_chat.id,
                    update.message.text
                )
                if response:
                    await update.message.reply_html(response)
            except Exception as e:
                logger.exception("message_handler_error")

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle callback queries (button presses)."""
        query = update.callback_query

        if not self._is_authorized(query.from_user.id):
            await query.answer("Unauthorized")
            return

        await query.answer()  # Acknowledge the callback

        if self._callback_handler:
            try:
                response = await self._callback_handler(
                    query.message.chat_id,
                    query.data
                )
                if response:
                    await query.edit_message_text(response, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.exception("callback_handler_error")

    def _is_authorized(self, user_id: int) -> bool:
        """Check if user is authorized."""
        if not self.config.allowed_user_ids:
            return True  # Allow all if no whitelist
        return user_id in self.config.allowed_user_ids

    @property
    def is_running(self) -> bool:
        """Check if bot is running."""
        return self._running
