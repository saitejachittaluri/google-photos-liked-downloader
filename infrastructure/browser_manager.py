"""Playwright browser lifecycle management."""

from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger
from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from infrastructure.exceptions import BrowserError


class BrowserManager:
    """Manage a persistent Playwright Chromium context and its active page.

    A dedicated profile directory is used so that the Google login session can be
    reused between runs. The user's normal Chrome profile should not be supplied,
    because Chrome locks active profiles and automation can corrupt them.
    """

    def __init__(
        self,
        chrome_profile_path: str | Path,
        *,
        headless: bool = False,
        navigation_timeout_ms: int = 30_000,
        max_retries: int = 3,
        retry_delay: float = 2.0,
    ) -> None:
        if max_retries < 1:
            raise ValueError("max_retries must be at least 1")
        if retry_delay < 0:
            raise ValueError("retry_delay cannot be negative")

        self.chrome_profile_path = Path(chrome_profile_path).expanduser().resolve()
        self.headless = headless
        self.navigation_timeout_ms = navigation_timeout_ms
        self.max_retries = max_retries
        self.retry_delay = retry_delay

        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    @property
    def page(self) -> Page:
        """Return the active page or raise a clear lifecycle error."""
        if self._page is None or self._page.is_closed():
            raise BrowserError("BrowserManager has no active page. Launch the browser first.")
        return self._page

    @property
    def context(self) -> BrowserContext:
        """Return the persistent browser context."""
        if self._context is None:
            raise BrowserError("BrowserManager has no active browser context.")
        return self._context

    async def launch_browser(self) -> Page:
        """Launch the persistent Chromium context and return its active page."""
        if await self.is_browser_running():
            return self.page

        self.chrome_profile_path.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Launching Chromium with persistent profile: {}",
            self.chrome_profile_path,
        )

        try:
            self._playwright = await async_playwright().start()
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.chrome_profile_path),
                headless=self.headless,
                accept_downloads=True,
                viewport={"width": 1440, "height": 1000},
                args=["--disable-notifications"],
            )
            self._context.set_default_timeout(self.navigation_timeout_ms)
            self._context.set_default_navigation_timeout(self.navigation_timeout_ms)

            open_pages = [page for page in self._context.pages if not page.is_closed()]
            self._page = open_pages[0] if open_pages else await self._context.new_page()
            self._page.on("crash", self._on_page_crash)
            self._page.on("close", self._on_page_close)

            logger.info("Browser launched successfully.")
            return self._page
        except Exception as exc:
            await self.shutdown()
            profile_hint = (
                " Ensure no other Chrome/Chromium process is using the configured "
                "browser profile directory."
            )
            raise BrowserError(f"Failed to launch browser.{profile_hint}") from exc

    async def open_url(self, url: str) -> Page:
        """Open a URL and wait until the DOM is ready."""
        page = self.page
        try:
            logger.info("Opening URL: {}", url)
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.navigation_timeout_ms,
            )
            logger.info("URL opened successfully: {}", page.url)
            return page
        except Exception as exc:
            raise BrowserError(f"Failed to open URL: {url}") from exc

    async def reconnect_browser(self) -> Page:
        """Recreate the browser context when the page or context is no longer usable."""
        if await self.is_browser_running():
            return self.page

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            logger.warning(
                "Browser is unavailable; reconnecting ({}/{}).",
                attempt,
                self.max_retries,
            )
            try:
                await self.shutdown()
                return await self.launch_browser()
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_delay)

        raise BrowserError(
            f"Failed to reconnect after {self.max_retries} attempts."
        ) from last_error

    async def shutdown(self) -> None:
        """Close the persistent context and stop Playwright safely."""
        context, playwright = self._context, self._playwright
        self._page = None
        self._context = None
        self._playwright = None

        if context is not None:
            try:
                await context.close()
            except Exception as exc:
                logger.debug("Browser context was already closed: {}", exc)

        if playwright is not None:
            try:
                await playwright.stop()
            except Exception as exc:
                logger.debug("Playwright was already stopped: {}", exc)

        logger.info("BrowserManager shut down.")

    async def is_browser_running(self) -> bool:
        """Return True when a usable page exists in an active context."""
        return (
            self._context is not None
            and self._page is not None
            and not self._page.is_closed()
        )

    def _on_page_crash(self, _page: Page) -> None:
        logger.error("The automated browser page crashed.")

    def _on_page_close(self, _page: Page) -> None:
        logger.debug("The automated browser page was closed.")
