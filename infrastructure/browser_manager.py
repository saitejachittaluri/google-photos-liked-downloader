from playwright.async_api import async_playwright, Browser, Page, Playwright
from loguru import logger
from infrastructure.exceptions import BrowserError
import asyncio


class BrowserManager:
    def __init__(self, chrome_profile_path: str, max_retries: int = 3, retry_delay: int = 5):
        """
        Manages the lifecycle of a Playwright browser instance.

        Args:
            chrome_profile_path (str): Path to the Chrome user profile directory.
            max_retries (int): Maximum number of retries for reconnecting the browser.
            retry_delay (int): Delay (in seconds) between retries.
        """
        self.chrome_profile_path = chrome_profile_path
        self.max_retries = max_retries
        self.retry_delay = retry_delay

        self._playwright: Playwright = None
        self._browser: Browser = None
        self._page: Page = None

    async def launch_browser(self):
        """Launch the browser with the specified Chrome profile."""
        logger.info("Launching browser with Chrome profile: {}", self.chrome_profile_path)
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=self.chrome_profile_path,
                headless=False,
            )
            self._page = await self._browser.new_page()
            logger.info("Browser launched successfully.")
        except Exception as e:
            logger.error("Failed to launch browser: {}", e)
            await self.shutdown()
            raise BrowserError("Failed to launch browser.") from e

    async def open_url(self, url: str):
        """
        Open a URL in the browser.

        Args:
            url (str): The URL to open.

        Raises:
            BrowserError: If the URL cannot be opened.
        """
        if not self._page:
            raise BrowserError("Browser is not launched.")
        try:
            logger.info("Opening URL: {}", url)
            await self._page.goto(url, timeout=30000)
            await self._page.wait_for_load_state("load")
            logger.info("URL opened successfully.")
        except Exception as e:
            logger.error("Failed to open URL: {}", e)
            raise BrowserError("Failed to open URL.") from e

    async def reconnect_browser(self):
        """
        Detect browser crashes and reconnect automatically.

        Raises:
            BrowserError: If reconnection fails after the maximum number of retries.
        """
        retries = 0
        while retries < self.max_retries:
            try:
                if not self._page or self._page.is_closed():
                    logger.warning("Browser page is closed. Attempting to reconnect...")
                    await self.shutdown()
                    await self.launch_browser()
                    logger.info("Reconnected successfully.")
                    return
            except Exception as e:
                logger.error("Reconnection attempt failed: {}", e)
                retries += 1
                await asyncio.sleep(self.retry_delay)
        logger.critical("Failed to reconnect after {} retries.", self.max_retries)
        raise BrowserError("BrowserManager failed to reconnect.")

    async def shutdown(self):
        """Shutdown the browser manager and clean up resources."""
        logger.info("Shutting down BrowserManager.")
        try:
            if self._page and not self._page.is_closed():
                await self._page.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.error("Error during shutdown: {}", e)
        finally:
            self._page = None
            self._browser = None
            self._playwright = None

    async def is_browser_running(self) -> bool:
        """
        Check if the browser and page are running.

        Returns:
            bool: True if the browser and page are running, False otherwise.
        """
        return self._browser is not None and self._page is not None and not self._page.is_closed()
