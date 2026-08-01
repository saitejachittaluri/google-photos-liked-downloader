"""Application service that orchestrates album traversal and liked-photo downloads."""

from __future__ import annotations

import inspect
from typing import Any

from loguru import logger
from playwright.async_api import Page

from infrastructure.browser_manager import BrowserManager
from infrastructure.database_manager import DatabaseManager
from infrastructure.download_manager import DownloadManager
from infrastructure.navigation_engine import NavigationEngine


class PhotoService:
    """Coordinate browser, navigation, detection, persistence, and downloads.

    The service deliberately never clicks a Like/Unlike control. A photo is treated as
    liked only when its activity panel contains at least one accessibility element whose
    label starts with ``Liked by ``.
    """

    def __init__(
        self,
        browser_manager: BrowserManager,
        navigation_engine: NavigationEngine,
        download_manager: DownloadManager,
        database_manager: DatabaseManager,
        *,
        dry_run: bool = False,
        max_photos: int | None = None,
    ) -> None:
        self.browser_manager = browser_manager
        self.navigation_engine = navigation_engine
        self.download_manager = download_manager
        self.database_manager = database_manager
        self.dry_run = dry_run
        self.max_photos = max_photos

    async def process_photos(self, shared_album_url: str) -> None:
        """Process the album from its first photo."""
        await self._run(shared_album_url=shared_album_url, resume_after_photo_id=None)

    async def resume_photos(
        self,
        shared_album_url: str,
        last_photo_id: str,
    ) -> None:
        """Resume processing immediately after ``last_photo_id``."""
        await self._run(
            shared_album_url=shared_album_url,
            resume_after_photo_id=last_photo_id,
        )

    async def _run(
        self,
        *,
        shared_album_url: str,
        resume_after_photo_id: str | None,
    ) -> None:
        processed_count = 0
        seen_photo_ids: set[str] = set()

        await self.browser_manager.launch_browser()
        try:
            page = self._browser_page()
            self._inject_page(page)

            await self.browser_manager.open_url(shared_album_url)
            await self.navigation_engine.open_first_photo()

            if resume_after_photo_id:
                found = await self._seek_to_photo(resume_after_photo_id)
                if not found:
                    raise RuntimeError(
                        f"Unable to find resume photo '{resume_after_photo_id}' in the album."
                    )
                await self.navigation_engine.navigate_next()

            while True:
                photo_id = await self.navigation_engine.current_photo_id()
                if not photo_id:
                    raise RuntimeError("Could not determine the current photo ID.")

                # A repeated ID means ArrowRight did not move or the album wrapped around.
                if photo_id in seen_photo_ids:
                    logger.info("Album traversal completed at photo {}.", photo_id)
                    break
                seen_photo_ids.add(photo_id)

                await self._process_current_photo(page, photo_id)
                await self.database_manager.set_setting("last_photo_id", photo_id)

                processed_count += 1
                if self.max_photos is not None and processed_count >= self.max_photos:
                    logger.info("Reached configured maximum of {} photos.", self.max_photos)
                    break

                previous_photo_id = photo_id
                next_photo_id = await self.navigation_engine.navigate_next()
                if not next_photo_id or next_photo_id == previous_photo_id:
                    logger.info("Reached the end of the album at photo {}.", photo_id)
                    break
        finally:
            await self.browser_manager.shutdown()

    async def _process_current_photo(self, page: Page, photo_id: str) -> None:
        if await self._is_photo_downloaded(photo_id):
            logger.info("Skipping previously downloaded photo {}.", photo_id)
            return

        liked = await self._is_liked_by_anyone(page)
        await self.database_manager.add_photo(photo_id, page.url, liked)

        if not liked:
            logger.info("Photo {} has no likes; skipping.", photo_id)
            return

        if self.dry_run:
            logger.info("[dry-run] Photo {} is liked; download skipped.", photo_id)
            return

        filename = await self.download_manager.download_file()
        await self.database_manager.add_download(photo_id, filename)
        logger.info("Downloaded liked photo {} to {}.", photo_id, filename)

    async def _is_liked_by_anyone(self, page: Page) -> bool:
        """Return True when the current photo activity contains at least one like.

        ``aria-label='Delete like'`` is intentionally not used because it can represent
        only the signed-in user's own removable like. The activity panel is used to find
        likes from any album participant.
        """
        activity_button = page.locator("[aria-label='View activity']")
        if await activity_button.count() > 0:
            try:
                await activity_button.first.click(timeout=5_000)
                await page.wait_for_timeout(500)
            except Exception as exc:  # The panel may already be open.
                logger.debug("Could not toggle the activity panel: {}", exc)

        liked_by = page.locator("[aria-label^='Liked by ']")
        liked = await liked_by.count() > 0
        logger.info("Current photo liked by at least one participant: {}", liked)
        return liked

    async def _seek_to_photo(self, target_photo_id: str) -> bool:
        """Navigate forward until ``target_photo_id`` is reached."""
        visited: set[str] = set()

        while True:
            current_photo_id = await self.navigation_engine.current_photo_id()
            if current_photo_id == target_photo_id:
                return True
            if not current_photo_id or current_photo_id in visited:
                return False

            visited.add(current_photo_id)
            next_photo_id = await self.navigation_engine.navigate_next()
            if not next_photo_id or next_photo_id == current_photo_id:
                return False

    async def _is_photo_downloaded(self, photo_id: str) -> bool:
        """Support both the intended repository method and the current DB wrapper."""
        method = getattr(self.database_manager, "is_photo_downloaded", None)
        if callable(method):
            result = method(photo_id)
            if inspect.isawaitable(result):
                result = await result
            return bool(result)

        database = getattr(self.database_manager, "database", None)
        if database is None:
            return False

        try:
            count = await database.fetch_val(
                "SELECT COUNT(*) FROM downloads WHERE photo_id = :photo_id",
                {"photo_id": photo_id},
            )
            return bool(count)
        except Exception as exc:
            # Database initialization is handled elsewhere; lack of a table should not
            # cause a photo to be incorrectly considered downloaded.
            logger.debug("Could not check download history for {}: {}", photo_id, exc)
            return False

    def _browser_page(self) -> Page:
        page: Any = getattr(self.browser_manager, "page", None)
        if page is None:
            page = getattr(self.browser_manager, "_page", None)
        if page is None:
            raise RuntimeError("BrowserManager did not expose an active Playwright page.")
        return page

    def _inject_page(self, page: Page) -> None:
        """Attach the launched page to collaborators created before browser startup."""
        self.navigation_engine.page = page
        self.download_manager.page = page
