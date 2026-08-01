from playwright.async_api import Page
from loguru import logger
from infrastructure.exceptions import NavigationError
import asyncio


class NavigationEngine:
    def __init__(self, page: Page, max_retries: int = 3, retry_delay: int = 2):
        """
        Handles navigation within a Google Photos album.

        Args:
            page (Page): Playwright Page instance for browser interaction.
            max_retries (int): Maximum number of retries for navigation failures.
            retry_delay (int): Delay (in seconds) between retries.
        """
        self.page = page
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    async def open_first_photo(self):
        """
        Open the first photo in the album.

        Raises:
            NavigationError: If the first photo cannot be opened after retries.
        """
        retries = 0
        while retries < self.max_retries:
            try:
                logger.info("Opening the first photo in the album.")
                first_photo_selector = "css=[data-testid='thumbnail']"  # Adjust selector as needed
                await self.page.locator(first_photo_selector).first.click(timeout=10000)
                logger.info("First photo opened successfully.")
                return
            except Exception as e:
                logger.warning(
                    "Failed to open the first photo. Retrying... (Attempt {}/{})",
                    retries + 1,
                    self.max_retries,
                )
                retries += 1
                await asyncio.sleep(self.retry_delay)
        logger.error("Failed to open the first photo after {} retries.", self.max_retries)
        raise NavigationError("Failed to open the first photo.")

    async def navigate_next(self) -> str:
        """
        Navigate to the next photo using the right arrow key.

        Returns:
            str: The current photo ID after navigation.

        Raises:
            NavigationError: If navigation fails after retries.
        """
        retries = 0
        while retries < self.max_retries:
            try:
                logger.info("Navigating to the next photo.")
                await self.page.keyboard.press("ArrowRight")
                await self.page.wait_for_timeout(1000)  # Wait for navigation to complete
                current_photo_id = await self.current_photo_id()
                if current_photo_id:
                    logger.info("Successfully navigated to the next photo: {}", current_photo_id)
                    return current_photo_id
                else:
                    raise NavigationError("Failed to retrieve current photo ID.")
            except Exception as e:
                logger.warning(
                    "Navigation failed. Retrying... (Attempt {}/{})",
                    retries + 1,
                    self.max_retries,
                )
                retries += 1
                await asyncio.sleep(self.retry_delay)
        logger.error("Failed to navigate to the next photo after {} retries.", self.max_retries)
        raise NavigationError("Failed to navigate to the next photo.")

    async def detect_end_of_album(self) -> bool:
        """
        Detect if the end of the album has been reached.

        Returns:
            bool: True if the end of the album is detected, False otherwise.

        Raises:
            NavigationError: If the end-of-album detection fails.
        """
        try:
            logger.info("Checking if the end of the album has been reached.")
            end_of_album_selector = "css=[data-testid='end-of-album']"  # Adjust selector as needed
            end_of_album = await self.page.locator(end_of_album_selector).count()
            if end_of_album > 0:
                logger.info("End of album detected.")
                return True
            logger.info("End of album not detected.")
            return False
        except Exception as e:
            logger.error("Failed to detect end of album: {}", e)
            raise NavigationError("Failed to detect end of album.") from e

    async def current_photo_id(self) -> str:
        """
        Extract the current photo ID from the URL.

        Returns:
            str: The current photo ID.

        Raises:
            NavigationError: If the photo ID cannot be extracted.
        """
        try:
            url = self.page.url
            logger.info("Current URL: {}", url)
            # Assuming the photo ID is the last part of the URL after the last '/'
            photo_id = url.rstrip("/").split("/")[-1]
            logger.info("Extracted photo ID: {}", photo_id)
            return photo_id
        except Exception as e:
            logger.error("Failed to extract photo ID from URL: {}", e)
            raise NavigationError("Failed to extract photo ID.") from e
