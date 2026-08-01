import asyncio
from pathlib import Path
from loguru import logger
from playwright.async_api import Page
from infrastructure.exceptions import DownloadError


class DownloadManager:
    def __init__(self, page: Page, download_dir: str, max_retries: int = 3, retry_delay: int = 2):
        """
        Handles downloading files from Google Photos.

        Args:
            page (Page): Playwright Page instance for browser interaction.
            download_dir (str): Directory where files will be downloaded.
            max_retries (int): Maximum number of retries for download failures.
            retry_delay (int): Delay (in seconds) between retries.
        """
        self.page = page
        self.download_dir = Path(download_dir)
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    async def download_file(self) -> str:
        """
        Download a file and return the downloaded filename.

        Returns:
            str: The path to the downloaded file.

        Raises:
            DownloadError: If the download fails after retries.
        """
        retries = 0
        while retries < self.max_retries:
            try:
                logger.info("Starting file download.")
                await self._click_download()
                downloaded_file = await self._wait_for_download()
                logger.info("File downloaded successfully: {}", downloaded_file)
                return downloaded_file
            except Exception as e:
                logger.warning(
                    "Download failed. Retrying... (Attempt {}/{})",
                    retries + 1,
                    self.max_retries,
                )
                retries += 1
                await asyncio.sleep(self.retry_delay)
        logger.error("Failed to download file after {} retries.", self.max_retries)
        raise DownloadError("Failed to download file.")

    async def _click_download(self):
        """
        Click the 'More Options' and 'Download' buttons.

        Raises:
            DownloadError: If the buttons cannot be clicked.
        """
        try:
            logger.info("Clicking 'More Options' button.")
            more_options_selector = "css=[aria-label='More options']"  # Adjust selector as needed
            await self.page.locator(more_options_selector).click(timeout=10000)

            logger.info("Clicking 'Download' button.")
            download_selector = "css=[aria-label='Download']"  # Adjust selector as needed
            await self.page.locator(download_selector).click(timeout=10000)
        except Exception as e:
            logger.error("Failed to click download buttons: {}", e)
            raise DownloadError("Failed to click download buttons.") from e

    async def _wait_for_download(self) -> str:
        """
        Wait for the download to complete and return the filename.

        Returns:
            str: The path to the downloaded file.

        Raises:
            DownloadError: If the download does not complete or the file is not found.
        """
        logger.info("Waiting for the download to complete.")
        crdownload_file = None

        # Wait for the .crdownload file to appear
        while not crdownload_file:
            crdownload_files = list(self.download_dir.glob("*.crdownload"))
            if crdownload_files:
                crdownload_file = crdownload_files[0]
                logger.info("Download started: {}", crdownload_file)
            else:
                await asyncio.sleep(1)

        # Wait for the .crdownload file to disappear
        while crdownload_file.exists():
            logger.info("Download in progress: {}", crdownload_file)
            await asyncio.sleep(1)

        # Verify the final file exists
        downloaded_file = crdownload_file.with_suffix("")
        if not downloaded_file.exists():
            raise DownloadError(f"Downloaded file not found: {downloaded_file}")

        return str(downloaded_file)
