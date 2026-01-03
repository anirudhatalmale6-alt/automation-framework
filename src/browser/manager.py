"""
Browser Manager - Single persistent browser instance management.

Designed for low-resource environments (single browser, limited RAM).
"""

import asyncio
import os
from typing import Optional, Any
from pathlib import Path
import structlog

try:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    Browser = Any
    BrowserContext = Any
    Page = Any
    Playwright = Any

logger = structlog.get_logger()


class BrowserManager:
    """
    Manages a single persistent browser instance.

    Features:
    - Single browser context (memory efficient)
    - Session persistence (cookies, storage)
    - Automatic cleanup on memory pressure
    - Graceful restart capability
    """

    def __init__(
        self,
        headless: bool = True,
        user_data_dir: Optional[str] = None,
        max_pages: int = 1,
        viewport_width: int = 1280,
        viewport_height: int = 720,
    ):
        """
        Initialize browser manager.

        Args:
            headless: Run browser in headless mode
            user_data_dir: Directory for persistent session data
            max_pages: Maximum concurrent pages (default 1 for low-resource)
            viewport_width: Browser viewport width
            viewport_height: Browser viewport height
        """
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Playwright not installed. Run: pip install playwright && playwright install chromium")

        self.headless = headless
        self.user_data_dir = user_data_dir or "./data/browser"
        self.max_pages = max_pages
        self.viewport = {"width": viewport_width, "height": viewport_height}

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize browser instance."""
        async with self._lock:
            if self._initialized:
                return

            logger.info("browser_initializing", headless=self.headless)

            # Ensure user data directory exists
            Path(self.user_data_dir).mkdir(parents=True, exist_ok=True)

            # Start Playwright
            self._playwright = await async_playwright().start()

            # Launch browser with settings optimized for Docker/headless
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=[
                    "--disable-dev-shm-usage",  # Overcome limited /dev/shm in Docker
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-default-apps",
                    "--disable-sync",
                    "--disable-translate",
                    "--metrics-recording-only",
                    "--mute-audio",
                    "--no-first-run",
                    "--safebrowsing-disable-auto-update",
                    f"--window-size={self.viewport['width']},{self.viewport['height']}",
                    # Enable GPU rendering for proper page display
                    "--enable-features=NetworkService,NetworkServiceInProcess",
                    "--force-color-profile=srgb",
                    # Disable features that can cause blank pages
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--disable-ipc-flooding-protection",
                    # Memory optimization
                    "--single-process",
                    "--memory-pressure-off",
                ],
            )

            # Create persistent context with full browser capabilities
            self._context = await self._browser.new_context(
                viewport=self.viewport,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                storage_state=self._get_storage_path() if Path(self._get_storage_path()).exists() else None,
                java_script_enabled=True,
                bypass_csp=True,  # Bypass Content Security Policy for scraping
                ignore_https_errors=True,
                locale="en-US",
                timezone_id="America/New_York",
            )

            # Create initial page
            self._page = await self._context.new_page()

            self._initialized = True
            logger.info("browser_initialized")

    async def shutdown(self) -> None:
        """Gracefully shutdown browser."""
        async with self._lock:
            if not self._initialized:
                return

            logger.info("browser_shutting_down")

            try:
                # Save session state
                await self._save_storage_state()

                # Close page
                if self._page:
                    await self._page.close()
                    self._page = None

                # Close context
                if self._context:
                    await self._context.close()
                    self._context = None

                # Close browser
                if self._browser:
                    await self._browser.close()
                    self._browser = None

                # Stop Playwright
                if self._playwright:
                    await self._playwright.stop()
                    self._playwright = None

            except Exception as e:
                logger.error("browser_shutdown_error", error=str(e))

            self._initialized = False
            logger.info("browser_shutdown_complete")

    async def get_page(self) -> Page:
        """
        Get the current page instance.

        Ensures browser is initialized.
        """
        if not self._initialized:
            await self.initialize()

        if not self._page or self._page.is_closed():
            async with self._lock:
                self._page = await self._context.new_page()

        return self._page

    async def new_page(self) -> Page:
        """
        Create a new page (within max_pages limit).

        For single-page mode, this returns the existing page.
        """
        if self.max_pages == 1:
            return await self.get_page()

        async with self._lock:
            pages = self._context.pages
            if len(pages) >= self.max_pages:
                # Close oldest page
                await pages[0].close()

            return await self._context.new_page()

    async def close_page(self, page: Page) -> None:
        """Close a specific page."""
        if page and not page.is_closed():
            await page.close()

    async def restart(self) -> None:
        """Restart browser (for recovery)."""
        logger.info("browser_restarting")
        await self.shutdown()
        await self.initialize()

    async def cleanup(self) -> None:
        """
        Memory cleanup - called during memory pressure.

        Closes extra pages, clears cache.
        """
        async with self._lock:
            if not self._context:
                return

            # Close all pages except current
            pages = self._context.pages
            for page in pages[:-1]:
                await page.close()

            # Clear browser cache if available
            try:
                await self._context.clear_cookies()
            except Exception:
                pass

            logger.info("browser_cleanup_complete")

    async def _save_storage_state(self) -> None:
        """Save session state for persistence."""
        if self._context:
            try:
                storage_path = self._get_storage_path()
                await self._context.storage_state(path=storage_path)
                logger.debug("storage_state_saved", path=storage_path)
            except Exception as e:
                logger.warning("storage_state_save_failed", error=str(e))

    def _get_storage_path(self) -> str:
        """Get path for storage state file."""
        return os.path.join(self.user_data_dir, "storage_state.json")

    @property
    def is_initialized(self) -> bool:
        """Check if browser is initialized."""
        return self._initialized

    async def get_status(self) -> dict:
        """Get browser status for diagnostics."""
        return {
            "initialized": self._initialized,
            "headless": self.headless,
            "max_pages": self.max_pages,
            "current_pages": len(self._context.pages) if self._context else 0,
            "current_url": self._page.url if self._page and not self._page.is_closed() else None,
        }
