"""
Browser Context - Execution context for browser actions.

Provides a clean interface for browser operations with
automatic error handling and state tracking.
"""

import asyncio
from typing import Any, Optional, Callable, Awaitable
from dataclasses import dataclass, field
import time
import structlog

try:
    from playwright.async_api import Page, ElementHandle, Locator
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    Page = Any
    ElementHandle = Any
    Locator = Any

logger = structlog.get_logger()


@dataclass
class ActionResult:
    """Result of a browser action."""
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    duration_ms: float = 0
    screenshot: Optional[bytes] = None


@dataclass
class BrowserState:
    """Current browser state snapshot."""
    url: str = ""
    title: str = ""
    cookies: list = field(default_factory=list)
    storage: dict = field(default_factory=dict)
    timestamp: float = 0


class BrowserContext:
    """
    Execution context for browser operations.

    Wraps Playwright page with:
    - Automatic waiting
    - Error handling
    - State tracking
    - Screenshot capture on failure
    """

    def __init__(
        self,
        page: Page,
        default_timeout: int = 30000,
        screenshot_on_error: bool = True,
    ):
        """
        Initialize browser context.

        Args:
            page: Playwright page instance
            default_timeout: Default timeout in milliseconds
            screenshot_on_error: Capture screenshot on failures
        """
        self.page = page
        self.default_timeout = default_timeout
        self.screenshot_on_error = screenshot_on_error

        self._last_state: Optional[BrowserState] = None
        self._action_count = 0

    async def navigate(
        self,
        url: str,
        wait_until: str = "domcontentloaded",
        timeout: Optional[int] = None,
    ) -> ActionResult:
        """
        Navigate to URL.

        Args:
            url: Target URL
            wait_until: Wait condition (load, domcontentloaded, networkidle)
            timeout: Override default timeout
        """
        start_time = time.monotonic()
        try:
            await self.page.goto(
                url,
                wait_until=wait_until,
                timeout=timeout or self.default_timeout,
            )
            self._action_count += 1

            return ActionResult(
                success=True,
                data={"url": self.page.url, "title": await self.page.title()},
                duration_ms=(time.monotonic() - start_time) * 1000,
            )

        except Exception as e:
            screenshot = await self._capture_error_screenshot()
            return ActionResult(
                success=False,
                error=str(e),
                duration_ms=(time.monotonic() - start_time) * 1000,
                screenshot=screenshot,
            )

    async def click(
        self,
        selector: str,
        timeout: Optional[int] = None,
        force: bool = False,
    ) -> ActionResult:
        """
        Click an element.

        Args:
            selector: CSS selector or text selector
            timeout: Override default timeout
            force: Force click even if element is not visible
        """
        start_time = time.monotonic()
        try:
            await self.page.click(
                selector,
                timeout=timeout or self.default_timeout,
                force=force,
            )
            self._action_count += 1

            return ActionResult(
                success=True,
                data={"selector": selector, "action": "click"},
                duration_ms=(time.monotonic() - start_time) * 1000,
            )

        except Exception as e:
            screenshot = await self._capture_error_screenshot()
            return ActionResult(
                success=False,
                error=str(e),
                duration_ms=(time.monotonic() - start_time) * 1000,
                screenshot=screenshot,
            )

    async def fill(
        self,
        selector: str,
        value: str,
        timeout: Optional[int] = None,
    ) -> ActionResult:
        """
        Fill a text input.

        Args:
            selector: CSS selector for input element
            value: Text to enter
            timeout: Override default timeout
        """
        start_time = time.monotonic()
        try:
            await self.page.fill(
                selector,
                value,
                timeout=timeout or self.default_timeout,
            )
            self._action_count += 1

            return ActionResult(
                success=True,
                data={"selector": selector, "action": "fill", "length": len(value)},
                duration_ms=(time.monotonic() - start_time) * 1000,
            )

        except Exception as e:
            screenshot = await self._capture_error_screenshot()
            return ActionResult(
                success=False,
                error=str(e),
                duration_ms=(time.monotonic() - start_time) * 1000,
                screenshot=screenshot,
            )

    async def type_text(
        self,
        selector: str,
        text: str,
        delay: int = 50,
        timeout: Optional[int] = None,
    ) -> ActionResult:
        """
        Type text with realistic delays (human-like).

        Args:
            selector: CSS selector for input element
            text: Text to type
            delay: Delay between keystrokes in ms
            timeout: Override default timeout
        """
        start_time = time.monotonic()
        try:
            await self.page.type(
                selector,
                text,
                delay=delay,
                timeout=timeout or self.default_timeout,
            )
            self._action_count += 1

            return ActionResult(
                success=True,
                data={"selector": selector, "action": "type", "length": len(text)},
                duration_ms=(time.monotonic() - start_time) * 1000,
            )

        except Exception as e:
            screenshot = await self._capture_error_screenshot()
            return ActionResult(
                success=False,
                error=str(e),
                duration_ms=(time.monotonic() - start_time) * 1000,
                screenshot=screenshot,
            )

    async def select(
        self,
        selector: str,
        value: str,
        timeout: Optional[int] = None,
    ) -> ActionResult:
        """
        Select option from dropdown.

        Args:
            selector: CSS selector for select element
            value: Option value to select
            timeout: Override default timeout
        """
        start_time = time.monotonic()
        try:
            await self.page.select_option(
                selector,
                value,
                timeout=timeout or self.default_timeout,
            )
            self._action_count += 1

            return ActionResult(
                success=True,
                data={"selector": selector, "action": "select", "value": value},
                duration_ms=(time.monotonic() - start_time) * 1000,
            )

        except Exception as e:
            screenshot = await self._capture_error_screenshot()
            return ActionResult(
                success=False,
                error=str(e),
                duration_ms=(time.monotonic() - start_time) * 1000,
                screenshot=screenshot,
            )

    async def wait_for_selector(
        self,
        selector: str,
        state: str = "visible",
        timeout: Optional[int] = None,
    ) -> ActionResult:
        """
        Wait for element to appear.

        Args:
            selector: CSS selector
            state: Target state (attached, detached, visible, hidden)
            timeout: Override default timeout
        """
        start_time = time.monotonic()
        try:
            await self.page.wait_for_selector(
                selector,
                state=state,
                timeout=timeout or self.default_timeout,
            )

            return ActionResult(
                success=True,
                data={"selector": selector, "state": state},
                duration_ms=(time.monotonic() - start_time) * 1000,
            )

        except Exception as e:
            screenshot = await self._capture_error_screenshot()
            return ActionResult(
                success=False,
                error=str(e),
                duration_ms=(time.monotonic() - start_time) * 1000,
                screenshot=screenshot,
            )

    async def wait_for_navigation(
        self,
        url_pattern: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> ActionResult:
        """
        Wait for navigation to complete.

        Args:
            url_pattern: Optional URL pattern to wait for
            timeout: Override default timeout
        """
        start_time = time.monotonic()
        try:
            if url_pattern:
                await self.page.wait_for_url(
                    url_pattern,
                    timeout=timeout or self.default_timeout,
                )
            else:
                await self.page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=timeout or self.default_timeout,
                )

            return ActionResult(
                success=True,
                data={"url": self.page.url},
                duration_ms=(time.monotonic() - start_time) * 1000,
            )

        except Exception as e:
            screenshot = await self._capture_error_screenshot()
            return ActionResult(
                success=False,
                error=str(e),
                duration_ms=(time.monotonic() - start_time) * 1000,
                screenshot=screenshot,
            )

    async def extract_text(
        self,
        selector: str,
        timeout: Optional[int] = None,
    ) -> ActionResult:
        """
        Extract text content from element.

        Args:
            selector: CSS selector
            timeout: Override default timeout
        """
        start_time = time.monotonic()
        try:
            element = await self.page.wait_for_selector(
                selector,
                timeout=timeout or self.default_timeout,
            )
            text = await element.text_content()

            return ActionResult(
                success=True,
                data={"text": text.strip() if text else "", "selector": selector},
                duration_ms=(time.monotonic() - start_time) * 1000,
            )

        except Exception as e:
            screenshot = await self._capture_error_screenshot()
            return ActionResult(
                success=False,
                error=str(e),
                duration_ms=(time.monotonic() - start_time) * 1000,
                screenshot=screenshot,
            )

    async def extract_attribute(
        self,
        selector: str,
        attribute: str,
        timeout: Optional[int] = None,
    ) -> ActionResult:
        """
        Extract attribute value from element.

        Args:
            selector: CSS selector
            attribute: Attribute name
            timeout: Override default timeout
        """
        start_time = time.monotonic()
        try:
            element = await self.page.wait_for_selector(
                selector,
                timeout=timeout or self.default_timeout,
            )
            value = await element.get_attribute(attribute)

            return ActionResult(
                success=True,
                data={"value": value, "attribute": attribute, "selector": selector},
                duration_ms=(time.monotonic() - start_time) * 1000,
            )

        except Exception as e:
            screenshot = await self._capture_error_screenshot()
            return ActionResult(
                success=False,
                error=str(e),
                duration_ms=(time.monotonic() - start_time) * 1000,
                screenshot=screenshot,
            )

    async def extract_all(
        self,
        selector: str,
        attribute: Optional[str] = None,
    ) -> ActionResult:
        """
        Extract data from multiple elements.

        Args:
            selector: CSS selector
            attribute: Optional attribute (if None, extracts text)
        """
        start_time = time.monotonic()
        try:
            elements = await self.page.query_selector_all(selector)
            results = []

            for el in elements:
                if attribute:
                    value = await el.get_attribute(attribute)
                else:
                    value = await el.text_content()
                    value = value.strip() if value else ""
                results.append(value)

            return ActionResult(
                success=True,
                data={"items": results, "count": len(results)},
                duration_ms=(time.monotonic() - start_time) * 1000,
            )

        except Exception as e:
            screenshot = await self._capture_error_screenshot()
            return ActionResult(
                success=False,
                error=str(e),
                duration_ms=(time.monotonic() - start_time) * 1000,
                screenshot=screenshot,
            )

    async def screenshot(
        self,
        path: Optional[str] = None,
        full_page: bool = False,
    ) -> ActionResult:
        """
        Take screenshot.

        Args:
            path: Optional file path to save
            full_page: Capture full scrollable page
        """
        start_time = time.monotonic()
        try:
            screenshot_bytes = await self.page.screenshot(
                path=path,
                full_page=full_page,
            )

            return ActionResult(
                success=True,
                data={"path": path, "size": len(screenshot_bytes)},
                duration_ms=(time.monotonic() - start_time) * 1000,
                screenshot=screenshot_bytes,
            )

        except Exception as e:
            return ActionResult(
                success=False,
                error=str(e),
                duration_ms=(time.monotonic() - start_time) * 1000,
            )

    async def evaluate(
        self,
        script: str,
        arg: Optional[Any] = None,
    ) -> ActionResult:
        """
        Execute JavaScript in page context.

        Args:
            script: JavaScript code
            arg: Optional argument to pass
        """
        start_time = time.monotonic()
        try:
            result = await self.page.evaluate(script, arg)

            return ActionResult(
                success=True,
                data={"result": result},
                duration_ms=(time.monotonic() - start_time) * 1000,
            )

        except Exception as e:
            screenshot = await self._capture_error_screenshot()
            return ActionResult(
                success=False,
                error=str(e),
                duration_ms=(time.monotonic() - start_time) * 1000,
                screenshot=screenshot,
            )

    async def scroll(
        self,
        direction: str = "down",
        amount: int = 500,
    ) -> ActionResult:
        """
        Scroll the page.

        Args:
            direction: Scroll direction (up, down)
            amount: Scroll amount in pixels
        """
        start_time = time.monotonic()
        try:
            delta = amount if direction == "down" else -amount
            await self.page.mouse.wheel(0, delta)

            return ActionResult(
                success=True,
                data={"direction": direction, "amount": amount},
                duration_ms=(time.monotonic() - start_time) * 1000,
            )

        except Exception as e:
            return ActionResult(
                success=False,
                error=str(e),
                duration_ms=(time.monotonic() - start_time) * 1000,
            )

    async def get_state(self) -> BrowserState:
        """Get current browser state snapshot."""
        try:
            cookies = await self.page.context.cookies()
            storage = await self.page.evaluate("() => JSON.stringify(localStorage)")

            self._last_state = BrowserState(
                url=self.page.url,
                title=await self.page.title(),
                cookies=cookies,
                storage=storage,
                timestamp=time.time(),
            )
            return self._last_state

        except Exception:
            return self._last_state or BrowserState()

    async def _capture_error_screenshot(self) -> Optional[bytes]:
        """Capture screenshot on error if enabled."""
        if not self.screenshot_on_error:
            return None

        try:
            return await self.page.screenshot()
        except Exception:
            return None

    @property
    def url(self) -> str:
        """Get current page URL."""
        return self.page.url

    @property
    def action_count(self) -> int:
        """Get total action count."""
        return self._action_count
