"""Application service that orchestrates album traversal and liked-photo downloads."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from enum import Enum
from typing import Any

from loguru import logger
from playwright.async_api import Locator, Page

from infrastructure.browser_manager import BrowserManager
from infrastructure.database_manager import DatabaseManager
from infrastructure.download_manager import DownloadManager
from infrastructure.navigation_engine import NavigationEngine


class LikeStatus(str, Enum):
    """Tri-state result. UNKNOWN is always skipped and never downloaded."""

    LIKED = "liked"
    NOT_LIKED = "not_liked"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LikeProbeResult:
    """Result from an isolated probe of one exact photo URL."""

    status: LikeStatus
    evidence: tuple[str, ...] = ()
    reason: str | None = None


class PhotoService:
    """Traverse an album and download only photos with proven like evidence.

    The main page is reserved for traversal and downloading. A single secondary page is
    reused as an isolated verifier. The verifier is brought to the foreground before
    interacting because Google Photos lazily creates viewer controls only for the active
    tab. Every positive result is reproduced after a fresh navigation before downloading.

    No Like, Unlike, Delete-like, upload, share, edit, or delete control is ever selected.
    """

    _VIEW_ACTIVITY_SELECTORS = (
        "[aria-label='View activity']",
        "button[title='View activity']",
        "[role='button'][title='View activity']",
    )
    _MORE_OPTIONS_SELECTORS = (
        "[aria-label='More options']",
        "button[title='More options']",
        "[role='button'][title='More options']",
    )

    _ACTIVITY_STATE_SCRIPT = r"""
() => {
    const visible = (element) => {
        if (!(element instanceof Element)) return false;
        const style = window.getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        const opacity = Number.parseFloat(style.opacity || "1");
        return style.display !== "none"
            && style.visibility !== "hidden"
            && opacity > 0.01
            && rect.width > 0
            && rect.height > 0
            && rect.right > 0
            && rect.bottom > 0
            && rect.left < window.innerWidth
            && rect.top < window.innerHeight;
    };

    const normalize = (value) => (value || "").replace(/\s+/g, " ").trim();
    const allLabelled = Array.from(document.querySelectorAll("[aria-label]"));

    const labels = Array.from(
        document.querySelectorAll("[aria-label^='Liked by ']")
    )
        .filter(visible)
        .map((element) => normalize(element.getAttribute("aria-label")))
        .filter((label) => /^Liked by \S/i.test(label));

    const closeControls = allLabelled.filter((element) => {
        if (!visible(element)) return false;
        const label = normalize(element.getAttribute("aria-label"));
        return /^(close|hide).*activity|activity.*(close|hide)$/i.test(label);
    });

    const expandedActivityControls = allLabelled.filter((element) => {
        if (!visible(element)) return false;
        const label = normalize(element.getAttribute("aria-label"));
        const expanded = element.getAttribute("aria-expanded");
        const pressed = element.getAttribute("aria-pressed");
        return /^view activity$/i.test(label)
            && (expanded === "true" || pressed === "true");
    });

    const rightSide = (element) => {
        const rect = element.getBoundingClientRect();
        return rect.left >= window.innerWidth * 0.42
            && rect.right >= window.innerWidth * 0.78;
    };

    const headings = Array.from(
        document.querySelectorAll("h1,h2,h3,h4,[role='heading']")
    ).filter((element) => {
        if (!visible(element) || !rightSide(element)) return false;
        const text = normalize(element.textContent);
        return /^(activity|comments?)$/i.test(text);
    });

    const regions = Array.from(
        document.querySelectorAll(
            "aside,[role='dialog'],[role='complementary'],[role='region']"
        )
    ).filter((element) => {
        if (!visible(element) || !rightSide(element)) return false;
        const text = normalize(element.textContent).slice(0, 1000);
        return /\b(activity|liked by|comments?|no activity|add a comment)\b/i.test(text);
    });

    const genericPanels = Array.from(document.querySelectorAll("div")).filter((element) => {
        if (!visible(element) || !rightSide(element)) return false;
        const rect = element.getBoundingClientRect();
        if (rect.width < 220 || rect.height < 180) return false;
        if (rect.width > window.innerWidth * 0.65) return false;
        const text = normalize(element.textContent).slice(0, 1000);
        return /^(activity|comments?)\b|\b(no activity|add a comment|liked by)\b/i.test(text);
    });

    const uniqueLabels = Array.from(new Set(labels)).sort();
    const open = uniqueLabels.length > 0
        || closeControls.length > 0
        || expandedActivityControls.length > 0
        || headings.length > 0
        || regions.length > 0
        || genericPanels.length > 0;

    return {
        open,
        labels: uniqueLabels,
        closeCount: closeControls.length,
        expandedCount: expandedActivityControls.length,
        headingCount: headings.length,
        regionCount: regions.length,
        panelCount: genericPanels.length,
    };
}
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
        self._probe_page: Page | None = None
        self._summary = {
            "inspected": 0,
            "liked": 0,
            "not_liked": 0,
            "unknown": 0,
            "downloaded": 0,
            "already_downloaded": 0,
        }

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
            traversal_page = self._browser_page()
            self._inject_page(traversal_page)

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

                self._summary["inspected"] += 1
                await self._process_current_photo(traversal_page, photo_id)
                await self.database_manager.set_setting("last_photo_id", photo_id)

                processed_count += 1
                if self.max_photos is not None and processed_count >= self.max_photos:
                    logger.info("Reached configured maximum of {} photos.", self.max_photos)
                    break

                await traversal_page.bring_to_front()
                previous_photo_id = photo_id
                next_photo_id = await self.navigation_engine.navigate_next()
                if not next_photo_id or next_photo_id == previous_photo_id:
                    logger.info("Reached the end of the album at photo {}.", photo_id)
                    break
        finally:
            await self._close_probe_page()
            await self.browser_manager.shutdown()
            logger.info(
                "Run summary: inspected={}, liked={}, not_liked={}, unknown={}, "
                "downloaded={}, already_downloaded={}.",
                self._summary["inspected"],
                self._summary["liked"],
                self._summary["not_liked"],
                self._summary["unknown"],
                self._summary["downloaded"],
                self._summary["already_downloaded"],
            )

    async def _process_current_photo(self, traversal_page: Page, photo_id: str) -> None:
        photo_url = traversal_page.url
        if f"/photo/{photo_id}" not in photo_url:
            self._record_unknown(
                photo_id,
                f"traversal URL did not match the expected photo: {photo_url}",
            )
            return

        result = await self._probe_like_status(photo_url, photo_id)
        if result.status is LikeStatus.UNKNOWN:
            self._record_unknown(
                photo_id,
                result.reason or "activity state could not be proven",
            )
            return

        liked = result.status is LikeStatus.LIKED
        await self.database_manager.add_photo(photo_id, photo_url, liked)

        if not liked:
            self._summary["not_liked"] += 1
            logger.info("Photo {} has no likes; skipping.", photo_id)
            return

        self._summary["liked"] += 1
        logger.info("Photo {} confirmed liked; evidence={}", photo_id, result.evidence)

        if await self._is_photo_downloaded(photo_id):
            self._summary["already_downloaded"] += 1
            logger.info("Photo {} was already downloaded; skipping duplicate.", photo_id)
            return

        if self.dry_run:
            logger.info("[dry-run] Photo {} is liked; download skipped.", photo_id)
            return

        await traversal_page.bring_to_front()
        current_id = await self.navigation_engine.current_photo_id()
        if current_id != photo_id or f"/photo/{photo_id}" not in traversal_page.url:
            self._record_unknown(
                photo_id,
                "traversal photo changed after verification; download refused",
            )
            self._summary["liked"] -= 1
            return

        filename = await self.download_manager.download_file()
        await self.database_manager.add_download(photo_id, filename)
        self._summary["downloaded"] += 1
        logger.info("Downloaded liked photo {} to {}.", photo_id, filename)

    async def _probe_like_status(self, photo_url: str, photo_id: str) -> LikeProbeResult:
        """Probe the exact photo and independently reproduce positive evidence."""
        first = await self._probe_once(photo_url, photo_id, confirmation=False)
        if first.status is not LikeStatus.LIKED:
            return first

        second = await self._probe_once(photo_url, photo_id, confirmation=True)
        if second.status is not LikeStatus.LIKED:
            return LikeProbeResult(
                LikeStatus.UNKNOWN,
                reason=(
                    "positive evidence was not reproduced on a fresh confirmation load: "
                    f"{second.reason or second.status.value}"
                ),
            )

        return LikeProbeResult(
            LikeStatus.LIKED,
            evidence=tuple(sorted(set(first.evidence + second.evidence))),
        )

    async def _probe_once(
        self,
        photo_url: str,
        photo_id: str,
        *,
        confirmation: bool,
    ) -> LikeProbeResult:
        probe = await self._get_probe_page()
        phase = "confirmation" if confirmation else "primary"
        try:
            await probe.bring_to_front()
            await probe.goto(photo_url, wait_until="domcontentloaded", timeout=30_000)
            await self._wait_for_probe_ready(probe, photo_id)

            baseline = await self._activity_state(probe)
            if baseline["labels"]:
                return LikeProbeResult(
                    LikeStatus.UNKNOWN,
                    reason=(
                        f"{phase} verifier showed visible like evidence before "
                        "View activity was invoked"
                    ),
                )

            opened_state = await self._open_activity_surface(probe, baseline)
            if opened_state is None:
                labels = await self._visible_toolbar_labels(probe)
                return LikeProbeResult(
                    LikeStatus.UNKNOWN,
                    reason=(
                        f"{phase} verifier could not open and prove the activity surface; "
                        f"visible controls={labels}"
                    ),
                )

            final_state = await self._wait_for_activity_to_settle(probe, opened_state)
            labels = tuple(final_state["labels"])
            logger.info(
                "Photo {} {} activity evidence: open={}, labels={}, "
                "close={}, expanded={}, headings={}, regions={}, panels={}.",
                photo_id,
                phase,
                final_state["open"],
                labels,
                final_state["closeCount"],
                final_state["expandedCount"],
                final_state["headingCount"],
                final_state["regionCount"],
                final_state["panelCount"],
            )

            if not final_state["open"]:
                return LikeProbeResult(
                    LikeStatus.UNKNOWN,
                    reason=f"{phase} activity surface was not verifiably open",
                )
            if labels:
                return LikeProbeResult(LikeStatus.LIKED, evidence=labels)
            return LikeProbeResult(LikeStatus.NOT_LIKED)
        except Exception as exc:
            logger.debug("{} like probe failed for {}: {}", phase, photo_id, exc)
            return LikeProbeResult(LikeStatus.UNKNOWN, reason=f"{phase}: {exc}")

    async def _get_probe_page(self) -> Page:
        if self._probe_page is None or self._probe_page.is_closed():
            self._probe_page = await self.browser_manager.context.new_page()
        return self._probe_page

    async def _close_probe_page(self) -> None:
        if self._probe_page is not None and not self._probe_page.is_closed():
            try:
                await self._probe_page.close()
            except Exception as exc:
                logger.debug("Unable to close verifier page cleanly: {}", exc)
        self._probe_page = None

    async def _wait_for_probe_ready(self, page: Page, photo_id: str) -> None:
        """Foreground the verifier and force Google Photos to materialize its toolbar."""
        if f"/photo/{photo_id}" not in page.url:
            raise RuntimeError(
                f"Verifier opened the wrong photo. Expected {photo_id}, got {page.url}."
            )

        await page.bring_to_front()
        await page.locator("body").wait_for(state="visible", timeout=15_000)
        await page.wait_for_timeout(500)
        await self._show_viewer_controls(page)

        if await self._find_view_activity_control(page) is not None:
            return

        viewport = page.viewport_size or {"width": 1440, "height": 1000}
        await page.mouse.click(viewport["width"] // 2, viewport["height"] // 2)
        await page.wait_for_timeout(250)
        await self._show_viewer_controls(page)

        for _ in range(40):
            if await self._find_view_activity_control(page) is not None:
                return
            await page.wait_for_timeout(200)

        labels = await self._visible_toolbar_labels(page)
        raise RuntimeError(
            "View activity was not available after foregrounding and revealing "
            f"the verifier toolbar; visible controls={labels}"
        )

    async def _open_activity_surface(
        self,
        page: Page,
        baseline: dict[str, Any],
    ) -> dict[str, Any] | None:
        direct = await self._find_view_activity_control(page)
        if direct is not None:
            if await self._click_read_only_control(direct):
                state = await self._wait_for_activity_open(page, baseline)
                if state is not None:
                    return state

        more = await self._find_more_options_control(page)
        if more is not None and await self._click_read_only_control(more):
            await page.wait_for_timeout(250)
            menu_item = await self._find_view_activity_menu_item(page)
            if menu_item is not None and await self._click_read_only_control(menu_item):
                state = await self._wait_for_activity_open(page, baseline)
                if state is not None:
                    return state

        return None

    async def _wait_for_activity_open(
        self,
        page: Page,
        baseline: dict[str, Any],
    ) -> dict[str, Any] | None:
        for _ in range(30):
            state = await self._activity_state(page)
            if state["open"] and self._state_signature(state) != self._state_signature(baseline):
                return state
            await page.wait_for_timeout(150)
        return None

    async def _wait_for_activity_to_settle(
        self,
        page: Page,
        initial_state: dict[str, Any],
    ) -> dict[str, Any]:
        previous = initial_state
        stable_samples = 0

        for _ in range(30):
            await page.wait_for_timeout(200)
            current = await self._activity_state(page)
            if not current["open"]:
                return current

            if self._state_signature(current) == self._state_signature(previous):
                stable_samples += 1
            else:
                stable_samples = 0
            previous = current

            if stable_samples >= 5:
                return current

        return previous

    async def _find_view_activity_control(self, page: Page) -> Locator | None:
        candidates = tuple(page.locator(selector) for selector in self._VIEW_ACTIVITY_SELECTORS)
        return await self._first_usable(candidates)

    async def _find_more_options_control(self, page: Page) -> Locator | None:
        candidates = tuple(page.locator(selector) for selector in self._MORE_OPTIONS_SELECTORS)
        return await self._first_usable(candidates)

    async def _find_view_activity_menu_item(self, page: Page) -> Locator | None:
        candidates = (
            page.get_by_role("menuitem", name="View activity", exact=True),
            page.locator("[role='menuitem'][aria-label='View activity']"),
            page.locator("[role='menuitem']").filter(has_text="View activity"),
        )
        return await self._first_usable(candidates, visible_only=True)

    async def _first_usable(
        self,
        candidates: tuple[Locator, ...],
        *,
        visible_only: bool = False,
    ) -> Locator | None:
        fallback: Locator | None = None
        for locator in candidates:
            count = min(await locator.count(), 20)
            for index in range(count):
                item = locator.nth(index)
                try:
                    if await item.is_visible() and await item.is_enabled():
                        return item
                    if not visible_only and fallback is None:
                        disabled = await item.get_attribute("aria-disabled")
                        if disabled != "true":
                            fallback = item
                except Exception:
                    continue
        return fallback

    async def _click_read_only_control(self, locator: Locator) -> bool:
        try:
            if await locator.is_visible() and await locator.is_enabled():
                await locator.click(timeout=5_000)
                return True
        except Exception:
            pass

        try:
            return bool(
                await locator.evaluate(
                    """element => {
                        if (!(element instanceof HTMLElement)) return false;
                        if (element.getAttribute('aria-disabled') === 'true') return false;
                        element.click();
                        return true;
                    }"""
                )
            )
        except Exception:
            return False

    async def _activity_state(self, page: Page) -> dict[str, Any]:
        state = await page.evaluate(self._ACTIVITY_STATE_SCRIPT)
        return {
            "open": bool(state.get("open")),
            "labels": list(state.get("labels") or []),
            "closeCount": int(state.get("closeCount") or 0),
            "expandedCount": int(state.get("expandedCount") or 0),
            "headingCount": int(state.get("headingCount") or 0),
            "regionCount": int(state.get("regionCount") or 0),
            "panelCount": int(state.get("panelCount") or 0),
        }

    def _state_signature(self, state: dict[str, Any]) -> tuple[Any, ...]:
        return (
            bool(state["open"]),
            tuple(state["labels"]),
            int(state["closeCount"]),
            int(state["expandedCount"]),
            int(state["headingCount"]),
            int(state["regionCount"]),
            int(state["panelCount"]),
        )

    async def _visible_toolbar_labels(self, page: Page) -> list[str]:
        labels = await page.evaluate(
            """() => Array.from(document.querySelectorAll('[aria-label]'))
                .filter(element => {
                    const style = getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && rect.width > 0
                        && rect.height > 0
                        && rect.top < Math.max(180, innerHeight * 0.25);
                })
                .map(element => (element.getAttribute('aria-label') || '').trim())
                .filter(Boolean)
                .filter(label => !/^delete like$/i.test(label))
                .slice(0, 80)"""
        )
        return sorted(set(str(label) for label in labels))

    async def _show_viewer_controls(self, page: Page) -> None:
        viewport = page.viewport_size or {"width": 1440, "height": 1000}
        await page.mouse.move(viewport["width"] // 2, viewport["height"] // 2)
        await page.wait_for_timeout(100)
        await page.mouse.move(viewport["width"] - 80, 40)
        await page.wait_for_timeout(300)
        await page.mouse.move(viewport["width"] // 2, 35)
        await page.wait_for_timeout(300)

    def _record_unknown(self, photo_id: str, reason: str) -> None:
        self._summary["unknown"] += 1
        logger.warning(
            "Photo {} like status is UNKNOWN; skipped without downloading. Reason: {}",
            photo_id,
            reason,
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
