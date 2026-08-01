"""Application orchestration for downloading liked Google Photos album items."""

from __future__ import annotations

import inspect
from typing import Any

from loguru import logger
from playwright.async_api import Page

from domain.activity_indexer import AlbumActivityIndexer
from infrastructure.browser_manager import BrowserManager
from infrastructure.database_manager import DatabaseManager
from infrastructure.download_manager import DownloadManager
from infrastructure.navigation_engine import NavigationEngine


class PhotoService:
    """Download photos whose exact IDs occur in participant-like activity."""

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
        self._liked_photo_ids: set[str] = set()
        self._summary = {
            "inspected": 0,
            "liked": 0,
            "not_liked": 0,
            "downloaded": 0,
            "already_downloaded": 0,
        }

    async def process_photos(self, shared_album_url: str) -> None:
        await self._run(shared_album_url=shared_album_url, resume_after_photo_id=None)

    async def resume_photos(self, shared_album_url: str, last_photo_id: str) -> None:
        await self._run(shared_album_url=shared_album_url, resume_after_photo_id=last_photo_id)

    async def _run(self, *, shared_album_url: str, resume_after_photo_id: str | None) -> None:
        processed = 0
        seen: set[str] = set()
        await self.browser_manager.launch_browser()
        try:
            page = self._browser_page()
            self._inject_page(page)
            await self.browser_manager.open_url(shared_album_url)

            # Complete the read-only activity index before any download is attempted.
            self._liked_photo_ids = await AlbumActivityIndexer().index(page)

            await page.goto(shared_album_url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(800)
            await self.navigation_engine.open_first_photo()

            if resume_after_photo_id:
                if not await self._seek_to_photo(resume_after_photo_id):
                    raise RuntimeError(f"Unable to find resume photo '{resume_after_photo_id}'.")
                await self.navigation_engine.navigate_next()

            while True:
                photo_id = await self.navigation_engine.current_photo_id()
                if not photo_id:
                    raise RuntimeError("Could not determine the current photo ID.")
                if photo_id in seen:
                    logger.info("Album traversal completed at photo {}.", photo_id)
                    break
                seen.add(photo_id)

                self._summary["inspected"] += 1
                await self._process_current_photo(page, photo_id)
                await self.database_manager.set_setting("last_photo_id", photo_id)
                processed += 1

                if self.max_photos is not None and processed >= self.max_photos:
                    logger.info("Reached configured maximum of {} photos.", self.max_photos)
                    break

                next_id = await self.navigation_engine.navigate_next()
                if not next_id or next_id == photo_id:
                    logger.info("Reached the end of the album at photo {}.", photo_id)
                    break
        finally:
            await self.browser_manager.shutdown()
            logger.info(
                "Run summary: indexed_liked={}, inspected={}, liked={}, not_liked={}, "
                "downloaded={}, already_downloaded={}.",
                len(self._liked_photo_ids), self._summary["inspected"],
                self._summary["liked"], self._summary["not_liked"],
                self._summary["downloaded"], self._summary["already_downloaded"],
            )

    async def _process_current_photo(self, page: Page, photo_id: str) -> None:
        liked = photo_id in self._liked_photo_ids
        await self.database_manager.add_photo(photo_id, page.url, liked)
        if not liked:
            self._summary["not_liked"] += 1
            logger.info("Photo {} has no indexed participant like; skipping.", photo_id)
            return

        self._summary["liked"] += 1
        logger.info("Photo {} is confirmed liked by at least one participant.", photo_id)
        if await self._is_photo_downloaded(photo_id):
            self._summary["already_downloaded"] += 1
            logger.info("Photo {} was already downloaded; skipping duplicate.", photo_id)
            return
        if self.dry_run:
            logger.info("[dry-run] Photo {} is liked; download skipped.", photo_id)
            return

        current_id = await self.navigation_engine.current_photo_id()
        if current_id != photo_id:
            raise RuntimeError(f"Viewer changed from {photo_id} to {current_id}; download refused.")
        filename = await self.download_manager.download_file()
        await self.database_manager.add_download(photo_id, filename)
        self._summary["downloaded"] += 1
        logger.info("Downloaded liked photo {} to {}.", photo_id, filename)

    async def _seek_to_photo(self, target: str) -> bool:
        visited: set[str] = set()
        while True:
            current = await self.navigation_engine.current_photo_id()
            if current == target:
                return True
            if not current or current in visited:
                return False
            visited.add(current)
            next_id = await self.navigation_engine.navigate_next()
            if not next_id or next_id == current:
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
