"""Application service that orchestrates album traversal and liked-photo downloads."""

from __future__ import annotations

import inspect
from typing import Any

from loguru import logger
from playwright.async_api import Locator, Page

from infrastructure.browser_manager import BrowserManager
from infrastructure.database_manager import DatabaseManager
from infrastructure.download_manager import DownloadManager
from infrastructure.navigation_engine import NavigationEngine


class PhotoService:
    """Coordinate browser, navigation, detection, persistence, and downloads.

    The service deliberately never clicks a Like/Unlike control. A photo is treated as
    liked only when its activity panel contains at least one participant-like entry.
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
        await self._run(shared_album_url=shared_album_url, resume_after_photo_id=None)

    async def resume_photos(self, shared_album_url: str, last_photo_id: str) -> None:
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
        """Return True when the current photo activity contains a participant like."""
        await self._show_viewer_controls(page)
        photo_url = page.url

        if not await self._activity_panel_is_open(page):
            opened = await self._open_activity_panel(page)
            if not opened:
                raise RuntimeError(
                    "Could not open the Google Photos activity panel for the current photo."
                )

        try:
            await page.wait_for_timeout(500)
            liked_selectors = (
                "[aria-label^='Liked by ']",
                "[aria-label*=' liked this photo']",
                "[aria-label*=' liked a photo']",
                "text=/^Liked by /i",
            )
            for selector in liked_selectors:
                if await page.locator(selector).count() > 0:
                    logger.info("Current photo liked by at least one participant: True")
                    return True

            logger.info("Current photo liked by at least one participant: False")
            return False
        finally:
            await self._close_activity_panel(page, photo_url)

    async def _open_activity_panel(self, page: Page) -> bool:
        """Open activity using visible controls first, then safe DOM-click fallbacks."""
        candidates = page.locator("[aria-label='View activity']")
        count = min(await candidates.count(), 20)

        for index in range(count):
            candidate = candidates.nth(index)
            try:
                if await candidate.is_visible() and await candidate.is_enabled():
                    await candidate.click(timeout=5_000)
                    if await self._wait_for_activity_panel(page):
                        return True
            except Exception:
                continue

        for index in range(count):
            candidate = candidates.nth(index)
            try:
                await candidate.evaluate("element => element.click()")
                if await self._wait_for_activity_panel(page):
                    return True
            except Exception:
                continue

        return False

    async def _wait_for_activity_panel(self, page: Page) -> bool:
        for _ in range(10):
            if await self._activity_panel_is_open(page):
                return True
            await page.wait_for_timeout(150)
        return False

    async def _activity_panel_is_open(self, page: Page) -> bool:
        selectors = (
            "[aria-label='Close activity side pane']",
            "[aria-label='Close activity panel']",
            "[aria-label^='Liked by ']",
            "[aria-label*=' liked this photo']",
            "[aria-label*=' liked a photo']",
        )
        for selector in selectors:
            if await page.locator(selector).count() > 0:
                return True
        return False

    async def _show_viewer_controls(self, page: Page) -> None:
        viewport = page.viewport_size or {"width": 1440, "height": 1000}
        await page.mouse.move(viewport["width"] // 2, viewport["height"] // 2)
        await page.wait_for_timeout(100)
        await page.mouse.move(viewport["width"] // 2, 30)
        await page.wait_for_timeout(500)

    async def _first_visible(self, locator: Locator) -> Locator | None:
        count = min(await locator.count(), 20)
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if await candidate.is_visible() and await candidate.is_enabled():
                    return candidate
            except Exception:
                continue
        return None

    async def _close_activity_panel(self, page: Page, expected_photo_url: str) -> None:
        """Close only the activity pane; never press Escape, which exits the viewer."""
        close_candidates = (
            page.locator("button[aria-label='Close activity side pane']"),
            page.locator("[role='button'][aria-label='Close activity side pane']"),
            page.locator("button[aria-label='Close activity panel']"),
            page.locator("[role='button'][aria-label='Close activity panel']"),
        )

        for locator in close_candidates:
            button = await self._first_visible(locator)
            if button is None:
                continue
            try:
                await button.click(timeout=3_000)
                await page.wait_for_timeout(250)
                if page.url == expected_photo_url:
                    return
            except Exception:
                continue

        # In some layouts the same View activity control toggles the pane. Use a DOM
        # click on that exact read-only action. Never use Escape because it closes the
        # entire photo viewer and returns to the album grid.
        activity_controls = page.locator("[aria-label='View activity']")
        count = min(await activity_controls.count(), 20)
        for index in range(count):
            try:
                await activity_controls.nth(index).evaluate("element => element.click()")
                await page.wait_for_timeout(250)
                if page.url == expected_photo_url:
                    return
            except Exception:
                continue

        logger.warning(
            "Could not confirm that the activity panel closed; leaving it open to preserve the photo viewer."
        )

    async def _seek_to_photo(self, target_photo_id: str) -> bool:
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
        self.navigation_engine.page = page
        self.download_manager.page = page
