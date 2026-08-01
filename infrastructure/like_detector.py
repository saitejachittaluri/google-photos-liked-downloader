from playwright.sync_api import Page
from loguru import logger


class LikeDetector:
    def __init__(self, page: Page):
        self.page = page

    def is_liked(self) -> bool:
        """Check if the current photo is liked."""
        try:
            logger.info("Checking if the photo is liked.")
            result = self.page.evaluate(
                """() => !!document.querySelector('[aria-label="Delete like"]')"""
            )
            logger.info("Photo liked status: {}", result)
            return result
        except Exception as e:
            logger.error("Failed to detect like status: {}", e)
            raise
