"""
Browser Actions - Config-driven browser action handlers.

Provides action handlers that can be registered with the orchestrator
and executed via rules/workflows.
"""

import asyncio
import os
from typing import Any, Optional, Callable, Awaitable
from dataclasses import dataclass
import structlog

from browser.manager import BrowserManager
from browser.context import BrowserContext, ActionResult as BrowserActionResult

logger = structlog.get_logger()


@dataclass
class ActionResult:
    """Result compatible with rules engine action registry."""
    success: bool
    output: dict
    error: Optional[str] = None


class BrowserActions:
    """
    Browser action handlers for the automation framework.

    Provides config-driven browser operations that can be:
    - Registered with the orchestrator
    - Called from rules/workflows
    - Executed with rate limiting
    """

    def __init__(self, browser_manager: BrowserManager):
        """
        Initialize browser actions.

        Args:
            browser_manager: Browser manager instance
        """
        self.browser = browser_manager
        self._context: Optional[BrowserContext] = None

    async def _get_context(self) -> BrowserContext:
        """Get or create browser context."""
        if not self._context:
            page = await self.browser.get_page()
            self._context = BrowserContext(page)
        return self._context

    def get_handlers(self) -> dict[str, Callable[[dict], Awaitable[ActionResult]]]:
        """
        Get all action handlers for registration with orchestrator.

        Returns dict of action_name -> handler_function
        """
        return {
            # Primary action names
            "browser_navigate": self.navigate,
            "browser_click": self.click,
            "browser_fill": self.fill,
            "browser_type": self.type_text,
            "browser_select": self.select,
            "browser_wait": self.wait_for_selector,
            "browser_extract_text": self.extract_text,
            "browser_extract_attribute": self.extract_attribute,
            "browser_extract_all": self.extract_all,
            "browser_screenshot": self.screenshot,
            "browser_evaluate": self.evaluate,
            "browser_scroll": self.scroll,
            "browser_wait_navigation": self.wait_for_navigation,
            # Aliases for convenience
            "browser_goto": self.navigate,
            "browser_extract": self.extract_text,
            "browser_input": self.fill,
        }

    async def navigate(self, params: dict) -> ActionResult:
        """
        Navigate to URL.

        Params:
            url: Target URL (required)
            wait_until: load|domcontentloaded|networkidle (default: networkidle)
            wait_for: Alias for wait_until
            timeout: Timeout in ms (optional)
            delay_after: Extra delay in ms after page load (for JS-heavy sites)
        """
        url = params.get("url")
        if not url:
            return ActionResult(success=False, output={}, error="URL is required")

        # Support both wait_until and wait_for as param names
        wait_until = params.get("wait_until") or params.get("wait_for", "networkidle")

        ctx = await self._get_context()
        result = await ctx.navigate(
            url=url,
            wait_until=wait_until,
            timeout=params.get("timeout"),
        )

        # Extra delay for JS-heavy sites that need time to render
        delay_after = params.get("delay_after", 0)
        if delay_after > 0:
            import asyncio
            await asyncio.sleep(delay_after / 1000)

        return self._convert_result(result)

    async def click(self, params: dict) -> ActionResult:
        """
        Click an element.

        Params:
            selector: CSS selector (required)
            timeout: Timeout in ms (optional)
            force: Force click even if hidden (default: false)
        """
        selector = params.get("selector")
        if not selector:
            return ActionResult(success=False, output={}, error="Selector is required")

        ctx = await self._get_context()
        result = await ctx.click(
            selector=selector,
            timeout=params.get("timeout"),
            force=params.get("force", False),
        )

        return self._convert_result(result)

    async def fill(self, params: dict) -> ActionResult:
        """
        Fill input field (clears existing value).

        Params:
            selector: CSS selector (required)
            value: Text to enter (required)
            timeout: Timeout in ms (optional)
        """
        selector = params.get("selector")
        value = params.get("value", "")

        if not selector:
            return ActionResult(success=False, output={}, error="Selector is required")

        ctx = await self._get_context()
        result = await ctx.fill(
            selector=selector,
            value=str(value),
            timeout=params.get("timeout"),
        )

        return self._convert_result(result)

    async def type_text(self, params: dict) -> ActionResult:
        """
        Type text with realistic delays.

        Params:
            selector: CSS selector (required)
            text: Text to type (required)
            delay: Delay between keystrokes in ms (default: 50)
            timeout: Timeout in ms (optional)
        """
        selector = params.get("selector")
        text = params.get("text", "")

        if not selector:
            return ActionResult(success=False, output={}, error="Selector is required")

        ctx = await self._get_context()
        result = await ctx.type_text(
            selector=selector,
            text=str(text),
            delay=params.get("delay", 50),
            timeout=params.get("timeout"),
        )

        return self._convert_result(result)

    async def select(self, params: dict) -> ActionResult:
        """
        Select option from dropdown.

        Params:
            selector: CSS selector (required)
            value: Option value (required)
            timeout: Timeout in ms (optional)
        """
        selector = params.get("selector")
        value = params.get("value")

        if not selector or value is None:
            return ActionResult(success=False, output={}, error="Selector and value are required")

        ctx = await self._get_context()
        result = await ctx.select(
            selector=selector,
            value=str(value),
            timeout=params.get("timeout"),
        )

        return self._convert_result(result)

    async def wait_for_selector(self, params: dict) -> ActionResult:
        """
        Wait for element.

        Params:
            selector: CSS selector (required)
            state: attached|detached|visible|hidden (default: visible)
            timeout: Timeout in ms (optional)
        """
        selector = params.get("selector")
        if not selector:
            return ActionResult(success=False, output={}, error="Selector is required")

        ctx = await self._get_context()
        result = await ctx.wait_for_selector(
            selector=selector,
            state=params.get("state", "visible"),
            timeout=params.get("timeout"),
        )

        return self._convert_result(result)

    async def wait_for_navigation(self, params: dict) -> ActionResult:
        """
        Wait for navigation.

        Params:
            url_pattern: URL pattern to wait for (optional)
            timeout: Timeout in ms (optional)
        """
        ctx = await self._get_context()
        result = await ctx.wait_for_navigation(
            url_pattern=params.get("url_pattern"),
            timeout=params.get("timeout"),
        )

        return self._convert_result(result)

    async def extract_text(self, params: dict) -> ActionResult:
        """
        Extract text from element.

        Params:
            selector: CSS selector (required)
            timeout: Timeout in ms (optional)

        Output:
            text: Extracted text
        """
        selector = params.get("selector")
        if not selector:
            return ActionResult(success=False, output={}, error="Selector is required")

        ctx = await self._get_context()
        result = await ctx.extract_text(
            selector=selector,
            timeout=params.get("timeout"),
        )

        return self._convert_result(result)

    async def extract_attribute(self, params: dict) -> ActionResult:
        """
        Extract attribute from element.

        Params:
            selector: CSS selector (required)
            attribute: Attribute name (required)
            timeout: Timeout in ms (optional)

        Output:
            value: Attribute value
        """
        selector = params.get("selector")
        attribute = params.get("attribute")

        if not selector or not attribute:
            return ActionResult(success=False, output={}, error="Selector and attribute are required")

        ctx = await self._get_context()
        result = await ctx.extract_attribute(
            selector=selector,
            attribute=attribute,
            timeout=params.get("timeout"),
        )

        return self._convert_result(result)

    async def extract_all(self, params: dict) -> ActionResult:
        """
        Extract data from multiple elements.

        Params:
            selector: CSS selector (required)
            attribute: Attribute name (optional, extracts text if not specified)

        Output:
            items: List of extracted values
            count: Number of items
        """
        selector = params.get("selector")
        if not selector:
            return ActionResult(success=False, output={}, error="Selector is required")

        ctx = await self._get_context()
        result = await ctx.extract_all(
            selector=selector,
            attribute=params.get("attribute"),
        )

        return self._convert_result(result)

    async def screenshot(self, params: dict) -> ActionResult:
        """
        Take screenshot.

        Params:
            path: File path to save (optional)
            full_page: Capture full page (default: false)

        Output:
            path: Saved path (if specified)
            size: Screenshot size in bytes
        """
        ctx = await self._get_context()

        path = params.get("path")
        if path:
            # Ensure directory exists
            os.makedirs(os.path.dirname(path), exist_ok=True)

        result = await ctx.screenshot(
            path=path,
            full_page=params.get("full_page", False),
        )

        return self._convert_result(result)

    async def evaluate(self, params: dict) -> ActionResult:
        """
        Execute JavaScript.

        Params:
            script: JavaScript code (required)
            arg: Optional argument to pass

        Output:
            result: JavaScript return value
        """
        script = params.get("script")
        if not script:
            return ActionResult(success=False, output={}, error="Script is required")

        ctx = await self._get_context()
        result = await ctx.evaluate(
            script=script,
            arg=params.get("arg"),
        )

        return self._convert_result(result)

    async def scroll(self, params: dict) -> ActionResult:
        """
        Scroll the page.

        Params:
            direction: up|down (default: down)
            amount: Pixels to scroll (default: 500)
        """
        ctx = await self._get_context()
        result = await ctx.scroll(
            direction=params.get("direction", "down"),
            amount=params.get("amount", 500),
        )

        return self._convert_result(result)

    def _convert_result(self, browser_result: BrowserActionResult) -> ActionResult:
        """Convert browser action result to standard action result."""
        return ActionResult(
            success=browser_result.success,
            output=browser_result.data or {},
            error=browser_result.error,
        )
