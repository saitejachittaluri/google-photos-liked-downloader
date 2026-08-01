"""Reliable Playwright download handling for Google Photos."""

from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger
from playwright.async_api import Locator, Page

from infrastructure.exceptions import DownloadError


class DownloadManager:
    """Download the currently open Google Photos item."""

    def __init__(
        self,
        page: Page | None,
        download_dir: str | Path,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        download_timeout_ms: int = 120_000,
    ) -> None:
        self.page = page
        self.download_dir = Path(download_dir)
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.download_timeout_ms = download_timeout_ms
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def _require_page(self) -> Page:
        if self.page is None:
            raise DownloadError("DownloadManager has no active Playwright page.")
        return self.page

    async def download_file(self) -> str:
        """Download the current photo and return its final absolute path."""
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                downloaded_path = await self._download_once()
                logger.info("File downloaded successfully: {}", downloaded_path)
                return str(downloaded_path)
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Download failed (attempt {}/{}): {}",
                    attempt,
                    self.max_retries,
                    exc,
                )
                await self._dismiss_open_menu()
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_delay)

        raise DownloadError(
            f"Failed to download the current photo after {self.max_retries} attempts."
        ) from last_error

    async def _download_once(self) -> Path:
        page = self._require_page()
        download_control = await self._open_download_action()

        try:
            async with page.expect_download(timeout=self.download_timeout_ms) as info:
                await self._activate_download_control(download_control)
            download = await info.value
        except Exception as exc:
            raise DownloadError("Google Photos did not start a browser download.") from exc

        failure = await download.failure()
        if failure:
            raise DownloadError(f"Browser reported download failure: {failure}")

        destination = self._unique_destination(download.suggested_filename)
        try:
            await download.save_as(str(destination))
        except Exception as exc:
            raise DownloadError(f"Unable to save download to {destination}") from exc

        if not destination.is_file() or destination.stat().st_size <= 0:
            raise DownloadError(f"Downloaded file is missing or empty: {destination}")

        return destination.resolve()

    async def _activate_download_control(self, control: Locator) -> None:
        """Activate the real button/menuitem rather than its child text span."""
        try:
            await control.click(timeout=10_000)
            return
        except Exception as click_error:
            logger.debug("Normal download click failed; trying DOM click: {}", click_error)

        try:
            await control.evaluate("element => element.click()")
        except Exception as exc:
            raise DownloadError("Unable to activate the Google Photos Download action.") from exc

    async def _open_download_action(self) -> Locator:
        """Open the overflow menu and return the actual Download button/menuitem."""
        page = self._require_page()
        await self._show_viewer_controls(page)

        direct_candidates = (
            page.locator("button[aria-label='Download']"),
            page.locator("[role='button'][aria-label='Download']"),
            page.get_by_role("button", name="Download", exact=True),
        )
        direct = await self._first_visible(direct_candidates)
        if direct is not None:
            return direct

        more_candidates = (
            page.locator("button[aria-label='More options']"),
            page.locator("[role='button'][aria-label='More options']"),
            page.get_by_role("button", name="More options", exact=True),
        )
        more_options = await self._first_visible(more_candidates)
        if more_options is None:
            raise DownloadError("Could not find the Google Photos 'More options' control.")

        await more_options.click(timeout=10_000)
        menu = page.locator("[role='menu']:visible").first
        await menu.wait_for(state="visible", timeout=5_000)

        # In the current Google Photos UI the actionable element is an LI with an
        # aria-label such as "Download - Shift+D". Clicking its nested text SPAN is
        # unreliable because the parent LI intercepts pointer events.
        menu_items = page.locator("[role='menuitem'][aria-label^='Download']")
        count = min(await menu_items.count(), 20)
        for index in range(count):
            item = menu_items.nth(index)
            try:
                label = (await item.get_attribute("aria-label") or "").strip()
                text = (await item.inner_text()).strip()
                combined = f"{label} {text}".casefold()
                if "download all" in combined:
                    continue
                if await item.is_visible() and await item.is_enabled():
                    return item
            except Exception:
                continue

        # Fallback to a menuitem containing exact Download text, but always return
        # the menuitem parent rather than the nested span.
        text_matches = page.locator("[role='menuitem']").filter(has_text="Download")
        count = min(await text_matches.count(), 20)
        for index in range(count):
            item = text_matches.nth(index)
            try:
                text = (await item.inner_text()).strip().casefold()
                if text == "download" and await item.is_visible() and await item.is_enabled():
                    return item
            except Exception:
                continue

        await self._dismiss_open_menu()
        raise DownloadError("The Google Photos menu does not contain a usable Download action.")

    async def _show_viewer_controls(self, page: Page) -> None:
        viewport = page.viewport_size or {"width": 1440, "height": 1000}
        await page.mouse.move(viewport["width"] // 2, viewport["height"] // 2)
        await page.wait_for_timeout(100)
        await page.mouse.move(viewport["width"] // 2, 30)
        await page.wait_for_timeout(300)

    async def _first_visible(self, candidates: tuple[Locator, ...]) -> Locator | None:
        for candidate in candidates:
            count = min(await candidate.count(), 10)
            for index in range(count):
                item = candidate.nth(index)
                try:
                    if await item.is_visible() and await item.is_enabled():
                        return item
                except Exception:
                    continue
        return None

    async def _dismiss_open_menu(self) -> None:
        page = self.page
        if page is None:
            return
        try:
            menu = page.locator("[role='menu']:visible")
            if await menu.count() > 0:
                # Escape closes only the transient overflow menu here, not the activity pane.
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(100)
        except Exception:
            logger.debug("Unable to dismiss the open Google Photos menu.")

    def _unique_destination(self, suggested_filename: str) -> Path:
        """Return a collision-safe path without overwriting an existing photo."""
        safe_name = Path(suggested_filename or "google-photo-download").name
        candidate = self.download_dir / safe_name
        if not candidate.exists():
            return candidate

        stem = candidate.stem
        suffix = candidate.suffix
        counter = 1
        while True:
            alternative = self.download_dir / f"{stem} ({counter}){suffix}"
            if not alternative.exists():
                return alternative
            counter += 1
