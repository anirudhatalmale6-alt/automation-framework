"""
Message Formatter - Human-like message formatting for Telegram.

Provides formatting utilities without business-specific language.
"""

import re
from typing import Any, Optional, List
from dataclasses import dataclass
import html


@dataclass
class FormattedMessage:
    """A formatted message ready to send."""
    text: str
    parse_mode: str = "HTML"  # HTML or Markdown
    chunks: List[str] = None  # Split messages if too long

    def __post_init__(self):
        if self.chunks is None:
            self.chunks = [self.text]


class MessageFormatter:
    """
    Formats messages for Telegram with human-readable output.

    Features:
    - Text truncation and chunking
    - Code block formatting
    - Status indicators
    - Error formatting
    - Table-like data display
    """

    MAX_MESSAGE_LENGTH = 4096  # Telegram limit
    DEFAULT_CHUNK_SIZE = 4000  # Leave buffer for formatting

    def __init__(self, chunk_size: int = None):
        self.chunk_size = chunk_size or self.DEFAULT_CHUNK_SIZE

    def format_text(
        self,
        text: str,
        bold: bool = False,
        italic: bool = False,
        code: bool = False,
        pre: bool = False,
    ) -> str:
        """
        Format text with HTML tags.

        Args:
            text: Raw text
            bold: Make bold
            italic: Make italic
            code: Inline code
            pre: Code block
        """
        # Escape HTML entities
        text = html.escape(text)

        if pre:
            return f"<pre>{text}</pre>"
        if code:
            return f"<code>{text}</code>"
        if bold:
            text = f"<b>{text}</b>"
        if italic:
            text = f"<i>{text}</i>"

        return text

    def format_status(
        self,
        status: str,
        message: str,
        details: Optional[str] = None,
    ) -> FormattedMessage:
        """
        Format a status message.

        Args:
            status: Status type (success, error, warning, info, pending)
            message: Main message
            details: Optional details
        """
        icons = {
            "success": "✓",
            "error": "✗",
            "warning": "⚠",
            "info": "ℹ",
            "pending": "◐",
            "running": "▶",
            "completed": "✓",
            "failed": "✗",
        }

        icon = icons.get(status.lower(), "•")
        text = f"{icon} {html.escape(message)}"

        if details:
            text += f"\n\n<i>{html.escape(details)}</i>"

        return FormattedMessage(text=text, chunks=self._chunk_text(text))

    def format_error(
        self,
        error: str,
        context: Optional[dict] = None,
        show_traceback: bool = False,
    ) -> FormattedMessage:
        """
        Format an error message.

        Args:
            error: Error message
            context: Optional context dict
            show_traceback: Include traceback (if in context)
        """
        text = f"✗ <b>Error</b>\n\n{html.escape(error)}"

        if context:
            if "task_id" in context:
                text += f"\n\nTask: <code>{html.escape(str(context['task_id']))}</code>"
            if "action" in context:
                text += f"\nAction: <code>{html.escape(str(context['action']))}</code>"
            if show_traceback and "traceback" in context:
                text += f"\n\n<pre>{html.escape(context['traceback'][:1000])}</pre>"

        return FormattedMessage(text=text, chunks=self._chunk_text(text))

    def format_task_status(
        self,
        task_id: str,
        status: str,
        progress: Optional[float] = None,
        details: Optional[dict] = None,
    ) -> FormattedMessage:
        """
        Format task status update.

        Args:
            task_id: Task identifier
            status: Current status
            progress: Optional progress (0-1)
            details: Optional additional details
        """
        status_icons = {
            "pending": "◯",
            "queued": "◐",
            "running": "▶",
            "completed": "✓",
            "failed": "✗",
            "quarantined": "⊘",
            "cancelled": "⊗",
        }

        icon = status_icons.get(status.lower(), "•")
        text = f"{icon} <b>{html.escape(task_id)}</b>: {status}"

        if progress is not None:
            bar = self._progress_bar(progress)
            text += f"\n{bar} {int(progress * 100)}%"

        if details:
            for key, value in details.items():
                text += f"\n{html.escape(key)}: {html.escape(str(value))}"

        return FormattedMessage(text=text, chunks=self._chunk_text(text))

    def format_list(
        self,
        items: List[str],
        title: Optional[str] = None,
        numbered: bool = False,
    ) -> FormattedMessage:
        """
        Format a list of items.

        Args:
            items: List of items
            title: Optional title
            numbered: Use numbered list
        """
        lines = []

        if title:
            lines.append(f"<b>{html.escape(title)}</b>\n")

        for i, item in enumerate(items):
            prefix = f"{i + 1}." if numbered else "•"
            lines.append(f"{prefix} {html.escape(item)}")

        text = "\n".join(lines)
        return FormattedMessage(text=text, chunks=self._chunk_text(text))

    def format_key_value(
        self,
        data: dict,
        title: Optional[str] = None,
    ) -> FormattedMessage:
        """
        Format key-value pairs.

        Args:
            data: Dictionary of key-value pairs
            title: Optional title
        """
        lines = []

        if title:
            lines.append(f"<b>{html.escape(title)}</b>\n")

        for key, value in data.items():
            key_str = html.escape(str(key))
            value_str = html.escape(str(value))
            lines.append(f"<b>{key_str}:</b> {value_str}")

        text = "\n".join(lines)
        return FormattedMessage(text=text, chunks=self._chunk_text(text))

    def format_code_block(
        self,
        code: str,
        language: Optional[str] = None,
    ) -> FormattedMessage:
        """
        Format code block.

        Args:
            code: Code content
            language: Optional language hint
        """
        # Telegram doesn't support language hints in pre tags
        code = html.escape(code)
        text = f"<pre>{code}</pre>"

        return FormattedMessage(text=text, chunks=self._chunk_text(text))

    def format_approval_request(
        self,
        approval_id: int,
        action_type: str,
        description: str,
        context: Optional[dict] = None,
    ) -> FormattedMessage:
        """
        Format an approval request message.

        Args:
            approval_id: Approval request ID
            action_type: Type of action requiring approval
            description: Human-readable description
            context: Optional context
        """
        text = f"⚠ <b>Approval Required</b>\n\n"
        text += f"Action: {html.escape(action_type)}\n"
        text += f"Description: {html.escape(description)}\n"
        text += f"ID: <code>{approval_id}</code>"

        if context:
            text += "\n\nContext:"
            for key, value in context.items():
                text += f"\n• {html.escape(key)}: {html.escape(str(value)[:100])}"

        text += "\n\nRespond with /approve or /reject"

        return FormattedMessage(text=text, chunks=self._chunk_text(text))

    def format_workflow_result(
        self,
        workflow_id: str,
        success: bool,
        steps_completed: int,
        total_steps: int,
        output: Optional[dict] = None,
        error: Optional[str] = None,
    ) -> FormattedMessage:
        """
        Format workflow execution result.

        Args:
            workflow_id: Workflow identifier
            success: Whether workflow succeeded
            steps_completed: Number of completed steps
            total_steps: Total number of steps
            output: Optional output data
            error: Optional error message
        """
        icon = "✓" if success else "✗"
        status = "Completed" if success else "Failed"

        text = f"{icon} <b>Workflow {status}</b>\n\n"
        text += f"ID: <code>{html.escape(workflow_id)}</code>\n"
        text += f"Steps: {steps_completed}/{total_steps}"

        if error:
            text += f"\n\nError: {html.escape(error[:500])}"

        if output and success:
            text += "\n\nOutput:"
            for key, value in list(output.items())[:5]:
                val_str = str(value)[:100]
                text += f"\n• {html.escape(key)}: {html.escape(val_str)}"

        return FormattedMessage(text=text, chunks=self._chunk_text(text))

    def _progress_bar(self, progress: float, width: int = 10) -> str:
        """Generate text progress bar."""
        filled = int(progress * width)
        empty = width - filled
        return f"[{'█' * filled}{'░' * empty}]"

    def _chunk_text(self, text: str) -> List[str]:
        """Split text into chunks that fit Telegram message limit."""
        if len(text) <= self.chunk_size:
            return [text]

        chunks = []
        current_chunk = ""

        # Try to split on newlines
        lines = text.split("\n")

        for line in lines:
            if len(current_chunk) + len(line) + 1 <= self.chunk_size:
                current_chunk += line + "\n"
            else:
                if current_chunk:
                    chunks.append(current_chunk.rstrip())
                current_chunk = line + "\n"

        if current_chunk:
            chunks.append(current_chunk.rstrip())

        return chunks if chunks else [text[:self.chunk_size]]

    def truncate(self, text: str, max_length: int = 100, suffix: str = "...") -> str:
        """Truncate text to max length."""
        if len(text) <= max_length:
            return text
        return text[:max_length - len(suffix)] + suffix
