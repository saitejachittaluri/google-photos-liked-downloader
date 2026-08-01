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

    Safety rule: a photo is downloaded only when a freshly reloaded current-photo page
    exposes a visible accessibility label beginning with ``Liked by `` after the exact
    read-only ``View activity`` action is invoked. Like/Unlike controls are never used.
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
        # Always re-evaluate the live UI. Earlier incorrect database classifications must
        # never bypass the current safety check.
        liked = await self._is_liked_by_anyone(page, photo_id)
        await self.database_manager.add_photo(photo_id, page.url, liked)

        if not liked:
            logger.info("Photo {} has no visible 'Liked by' activity; skipping.", photo_id)
            return

        if await self._is_photo_downloaded(photo_id):
            logger.info("Photo {} is liked but was already downloaded; skipping.", photo_id)
            return

        if self.dry_run:
            logger.info("[dry-run] Photo {} is liked; download skipped.", photo_id)
            return

        filename = await self.download_manager.download_file()
        await self.database_manager.add_download(photo_id, filename)
        logger.info("Downloaded liked photo {} to {}.", photo_id, filename)

    async def _is_liked_by_anyone(self, page: Page, expected_photo_id: str) -> bool:
        """Detect likes from a clean page and fail closed on ambiguous state.

        Google Photos is a single-page application and retains stale activity nodes.
        Reloading the exact photo before and after detection isolates each decision. Pane
        visibility is deliberately not inferred from brittle close-button selectors;
        Google Photos uses different pane markup across accounts and screen sizes.
        """
        photo_url = page.url
        if f"/photo/{expected_photo_id}" not in photo_url:
            raise RuntimeError(
                f"Current URL does not match expected photo {expected_photo_id}; refusing detection."
            )

        await self._reload_current_photo(page, photo_url, expected_photo_id)
        await self._show_viewer_controls(page)

        liked_by = page.locator("[aria-label^='Liked by ']")
        preexisting = await self._visible_aria_labels(liked_by)
        if preexisting:
            raise RuntimeError(
                "Visible 'Liked by' evidence existed before View activity was opened; "
                "refusing to classify this photo."
            )

        invoked = await self._invoke_view_activity(page)
        if not invoked:
            raise RuntimeError(
                "Could not invoke the exact Google Photos 'View activity' action."
            )

        try:
            # Activity content is loaded asynchronously. Sample repeatedly so a slow pane
            # does not become a false negative, while never treating hidden nodes as likes.
            visible_labels: list[str] = []
            for _ in range(12):
                visible_labels = await self._visible_aria_labels(liked_by)
                if visible_labels:
                    break
                await page.wait_for_timeout(200)

            liked = bool(visible_labels)
            logger.info(
                "Photo {} like evidence: count={}, labels={}",
                expected_photo_id,
                len(visible_labels),
                visible_labels,
            )
            logger.info("Current photo liked by at least one participant: {}", liked)
            return liked
        finally:
            await self._reload_current_photo(page, photo_url, expected_photo_id)

    async def _invoke_view_activity(self, page: Page) -> bool:
        """Invoke only the exact read-only View activity action.

        A successful invocation means the action itself was dispatched. We intentionally
        do not require a particular close button or side-pane role because those selectors
        vary in Google Photos. The subsequent decision uses only newly visible ``Liked by``
        evidence on a freshly reloaded page.
        """
        candidates = page.locator("[aria-label='View activity']")
        count = min(await candidates.count(), 20)

        for index in range(count):
            candidate = candidates.nth(index)
            try:
                if await candidate.is_visible() and await candidate.is_enabled():
                    await candidate.click(timeout=5_000)
                    await page.wait_for_timeout(500)
                    logger.debug("Invoked visible View activity control at index {}.", index)
                    return True
            except Exception:
                continue

        # Some Google Photos layouts expose the functional toolbar button while
        # Playwright reports it as hidden during an animation. Dispatching click on this
        # exact aria-label is safe and cannot toggle a photo like.
        for index in range(count):
            candidate = candidates.nth(index)
            try:
                clicked = await candidate.evaluate(
                    """element => {
                        if (!(element instanceof HTMLElement)) return false;
                        element.click();
                        return true;
                    }"""
                )
                if clicked:
                    await page.wait_for_timeout(500)
                    logger.debug("Invoked DOM View activity control at index {}.", index)
                    return True
            except Exception:
                continue

        return False

    async def _reload_current_photo(
        self,
        page: Page,
        expected_url: str,
        expected_photo_id: str,
    ) -> None:
        await page.reload(wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(900)
        if page.url != expected_url or f"/photo/{expected_photo_id}" not in page.url:
            raise RuntimeError(
                f"Photo changed while refreshing {expected_photo_id}; refusing to continue."
            )

    async def _visible_aria_labels(self, locator: Locator) -> list[str]:
        labels: list[str] = []
        count = min(await locator.count(), 50)
        for index in range(count):
            item = locator.nth(index)
            try:
                if not await item.is_visible():
                    continue
                label = (await item.get_attribute("aria-label") or "").strip()
                if label.startswith("Liked by "):
                    labels.append(label)
            except Exception:
                continue
        return sorted(set(labels))

    async def _show_viewer_controls(self, page: Page) -> None:
        viewport = page.viewport_size or {"width": 1440, "height": 1000}
        await page.mouse.move(viewport["width"] // 2, viewport["height"] // 2)
        await page.wait_for_timeout(100)
        await page.mouse.move(viewport["width"] // 2, 30)
        await page.wait_for_timeout(500)

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
