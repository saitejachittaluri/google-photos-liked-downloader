"""Reliable Playwright download handling for Google Photos."""

from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger
from playwright.async_api import Locator, Page

from infrastructure.exceptions import DownloadError


class DownloadManager:
    """Download the currently open Google Photos item without polling temp files."""

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
                await download_control.click(timeout=10_000)
            download = await info.value
        except Exception as exc:
            raise DownloadError("Google Photos did not start a browser download.") from exc

        failure = await download.failure()
        if failure:
            raise DownloadError(f"Browser reported download failure: {failure}")

        destination = self._unique_destination(download.suggested_filename)
        try:
            await download.save_as(destination)
        except Exception as exc:
            raise DownloadError(f"Unable to save download to {destination}") from exc

        if not destination.is_file() or destination.stat().st_size <= 0:
            raise DownloadError(f"Downloaded file is missing or empty: {destination}")

        return destination.resolve()

    async def _open_download_action(self) -> Locator:
        """Open the overflow menu and return the exact Download action."""
        page = self._require_page()

        # Some viewer variants expose a direct Download button.
        direct_candidates = (
            page.get_by_role("button", name="Download", exact=True),
            page.locator("button[aria-label='Download']"),
            page.locator("[role='button'][aria-label='Download']"),
        )
        direct = await self._first_visible(direct_candidates)
        if direct is not None:
            return direct

        more_candidates = (
            page.get_by_role("button", name="More options", exact=True),
            page.locator("button[aria-label='More options']"),
            page.locator("[role='button'][aria-label='More options']"),
        )
        more_options = await self._first_visible(more_candidates)
        if more_options is None:
            raise DownloadError("Could not find the Google Photos 'More options' control.")

        await more_options.click(timeout=10_000)
        await page.locator("[role='menu']:visible").first.wait_for(
            state="visible", timeout=5_000
        )

        menu_candidates = (
            page.get_by_role("menuitem", name="Download", exact=True),
            page.locator("[role='menuitem'][aria-label='Download']"),
            page.locator("[role='menuitem']").filter(has_text="Download"),
            page.get_by_text("Download", exact=True),
        )
        menu_item = await self._first_visible(menu_candidates)
        if menu_item is None:
            await self._dismiss_open_menu()
            raise DownloadError("The Google Photos menu does not contain a Download action.")

        # Avoid accidentally choosing 'Download all'.
        text = (await menu_item.inner_text()).strip()
        if text and text.casefold() != "download":
            exact = page.get_by_text("Download", exact=True)
            if await exact.count() > 0 and await exact.first.is_visible():
                return exact.first
            raise DownloadError(f"Unexpected download menu action: {text}")

        return menu_item

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
            if await page.locator("[role='menu']:visible").count() > 0:
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
