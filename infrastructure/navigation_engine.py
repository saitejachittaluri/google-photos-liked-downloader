"""Google Photos album navigation helpers."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

from loguru import logger
from playwright.async_api import Locator, Page

from infrastructure.exceptions import NavigationError


class NavigationEngine:
    """Open photos and move through a Google Photos shared album safely."""

    def __init__(
        self,
        page: Page | None,
        max_retries: int = 3,
        retry_delay: float = 1.5,
        navigation_timeout_ms: int = 15_000,
        navigation_delay_ms: int = 750,
    ) -> None:
        self.page = page
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.navigation_timeout_ms = navigation_timeout_ms
        self.navigation_delay_ms = navigation_delay_ms

    def _require_page(self) -> Page:
        if self.page is None:
            raise NavigationError("NavigationEngine has no active Playwright page.")
        return self.page

    async def open_first_photo(self) -> str:
        """Open the first visible photo and return its stable photo identifier."""
        page = self._require_page()

        for attempt in range(1, self.max_retries + 1):
            try:
                await page.wait_for_load_state("domcontentloaded")
                photo = await self._first_photo_locator()
                if photo is None:
                    raise NavigationError("No visible photo thumbnail was found in the album.")

                before_url = page.url
                await photo.scroll_into_view_if_needed()
                await photo.click(timeout=self.navigation_timeout_ms)
                await self._wait_for_photo_view(before_url)

                photo_id = await self.current_photo_id()
                logger.info("Opened first album photo: {}", photo_id)
                return photo_id
            except Exception as exc:
                logger.warning(
                    "Unable to open first photo (attempt {}/{}): {}",
                    attempt,
                    self.max_retries,
                    exc,
                )
                if attempt == self.max_retries:
                    raise NavigationError("Failed to open the first album photo.") from exc
                await asyncio.sleep(self.retry_delay)
                await page.reload(wait_until="domcontentloaded")

        raise NavigationError("Failed to open the first album photo.")

    async def _first_photo_locator(self) -> Locator | None:
        """Return the first usable thumbnail using accessibility-friendly fallbacks."""
        page = self._require_page()

        selectors = (
            "a[href*='/photo/']",
            "a[href*='/share/'][href*='/photo/']",
            "[role='main'] a:has(img[src*='googleusercontent.com'])",
            "[role='main'] [role='button']:has(img[src*='googleusercontent.com'])",
            "main a:has(img)",
        )

        for selector in selectors:
            candidates = page.locator(selector)
            count = min(await candidates.count(), 20)
            for index in range(count):
                candidate = candidates.nth(index)
                try:
                    if await candidate.is_visible():
                        return candidate
                except Exception:
                    continue
        return None

    async def _wait_for_photo_view(self, previous_url: str) -> None:
        page = self._require_page()

        try:
            await page.wait_for_function(
                "previous => window.location.href !== previous",
                arg=previous_url,
                timeout=self.navigation_timeout_ms,
            )
        except Exception:
            # Some Google Photos views update the viewer without immediately changing URL.
            await page.locator(
                "[aria-label='View activity'], [aria-label='More options']"
            ).first.wait_for(state="visible", timeout=self.navigation_timeout_ms)

        await page.wait_for_timeout(self.navigation_delay_ms)

    async def navigate_next(self) -> str:
        """Move to the next photo.

        At the album boundary Google Photos may ignore ArrowRight. In that case the
        existing photo ID is returned, allowing the caller to terminate cleanly.
        """
        page = self._require_page()
        previous_id = await self.current_photo_id()
        previous_url = page.url

        for attempt in range(1, self.max_retries + 1):
            try:
                await self._close_transient_overlays()
                await page.keyboard.press("ArrowRight")

                try:
                    await page.wait_for_function(
                        "previous => window.location.href !== previous",
                        arg=previous_url,
                        timeout=self.navigation_timeout_ms,
                    )
                except Exception:
                    await page.wait_for_timeout(self.navigation_delay_ms)

                current_id = await self.current_photo_id()
                if current_id != previous_id:
                    logger.debug("Navigated from {} to {}", previous_id, current_id)
                    return current_id

                if await self.detect_end_of_album():
                    logger.info("No next photo is available after {}.", previous_id)
                    return previous_id

                raise NavigationError("ArrowRight did not change the current photo.")
            except Exception as exc:
                logger.warning(
                    "Next-photo navigation failed (attempt {}/{}): {}",
                    attempt,
                    self.max_retries,
                    exc,
                )
                if attempt == self.max_retries:
                    # Returning the original ID is safer than skipping an unknown photo.
                    return previous_id
                await asyncio.sleep(self.retry_delay)

        return previous_id

    async def _close_transient_overlays(self) -> None:
        """Close menus/activity panels that can intercept keyboard navigation."""
        page = self._require_page()
        menu = page.locator("[role='menu']:visible")
        if await menu.count() > 0:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(100)

    async def detect_end_of_album(self) -> bool:
        """Return True when the visible next-photo control is disabled or absent."""
        page = self._require_page()

        disabled_selectors = (
            "[aria-label='Next photo'][aria-disabled='true']",
            "button[aria-label='Next photo']:disabled",
            "[aria-label='Next'][aria-disabled='true']",
            "button[aria-label='Next']:disabled",
        )
        for selector in disabled_selectors:
            if await page.locator(selector).count() > 0:
                return True

        next_controls = page.locator(
            "[aria-label='Next photo'], button[aria-label='Next'], [aria-label='Next']"
        )
        if await next_controls.count() == 0:
            # Google Photos sometimes hides the control until pointer movement. Absence alone
            # is therefore not definitive while the viewer is open.
            return False

        for index in range(await next_controls.count()):
            control = next_controls.nth(index)
            try:
                if await control.is_visible():
                    return not await control.is_enabled()
            except Exception:
                continue
        return False

    async def current_photo_id(self) -> str:
        """Extract a stable identifier for the current photo from the viewer URL."""
        page = self._require_page()
        url = page.url
        parsed = urlparse(url)
        segments = [segment for segment in parsed.path.split("/") if segment]

        # Common Google Photos forms include /photo/<id> and /share/.../photo/<id>.
        for marker in ("photo", "p"):
            if marker in segments:
                index = len(segments) - 1 - segments[::-1].index(marker)
                if index + 1 < len(segments):
                    candidate = segments[index + 1]
                    if candidate:
                        return candidate

        # Shared-view IDs are long URL-safe tokens. Prefer the final plausible token.
        for segment in reversed(segments):
            if re.fullmatch(r"[A-Za-z0-9_-]{12,}", segment):
                return segment

        raise NavigationError(f"Unable to extract a photo ID from URL: {url}")

    async def get_current_photo_id(self) -> str:
        """Backward-compatible alias for older callers and tests."""
        return await self.current_photo_id()
