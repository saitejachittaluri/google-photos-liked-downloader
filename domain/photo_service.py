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
    """Tri-state result used to fail closed when Google Photos is ambiguous."""

    LIKED = "liked"
    NOT_LIKED = "not_liked"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LikeProbeResult:
    """Result from an isolated current-photo activity probe."""

    status: LikeStatus
    evidence: tuple[str, ...] = ()
    reason: str | None = None


class PhotoService:
    """Coordinate browser, navigation, detection, persistence, and downloads.

    A photo is downloaded only after a separate temporary page, created in the same
    authenticated browser context, proves that the activity surface for that exact photo
    is open and contains a visible ``Liked by ...`` accessibility label. The traversal
    page is never used for like detection, so activity DOM cannot leak between photos.

    Ambiguous results are ``UNKNOWN`` and are skipped. They never stop traversal and they
    never trigger a download. No Like, Unlike, or Delete-like control is ever selected.
    """

    _VIEW_ACTIVITY_SELECTOR = "[aria-label='View activity']"
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
    const rightSide = (element) => {
        const rect = element.getBoundingClientRect();
        return rect.right >= window.innerWidth * 0.72
            && rect.left >= window.innerWidth * 0.35;
    };

    const labelled = Array.from(document.querySelectorAll("[aria-label]"));
    const closeControls = labelled.filter((element) => {
        const label = normalize(element.getAttribute("aria-label"));
        return visible(element)
            && rightSide(element)
            && /^(close|hide).*activity|activity.*(close|hide)$/i.test(label);
    });

    const headingCandidates = Array.from(document.querySelectorAll(
        "h1,h2,h3,h4,[role='heading'],div,span"
    ));
    const activityHeadings = headingCandidates.filter((element) => {
        const text = normalize(element.textContent);
        return visible(element)
            && rightSide(element)
            && /^(activity|comments?)$/i.test(text);
    });

    const regionCandidates = Array.from(document.querySelectorAll(
        "aside,[role='dialog'],[role='complementary'],[role='region']"
    ));
    const activityRegions = regionCandidates.filter((element) => {
        if (!visible(element) || !rightSide(element)) return false;
        const text = normalize(element.textContent).slice(0, 300);
        return /\b(activity|liked by|comments?)\b/i.test(text);
    });

    const roots = [];
    const addRoot = (source) => {
        let node = source;
        for (let depth = 0; node && depth < 10; depth += 1, node = node.parentElement) {
            if (!visible(node)) continue;
            const rect = node.getBoundingClientRect();
            if (rect.width >= 220
                    && rect.width <= window.innerWidth * 0.68
                    && rect.height >= 140
                    && rect.right >= window.innerWidth * 0.82) {
                roots.push(node);
                return;
            }
        }
    };
    closeControls.forEach(addRoot);
    activityHeadings.forEach(addRoot);
    activityRegions.forEach((region) => roots.push(region));

    const likeElements = Array.from(document.querySelectorAll("[aria-label^='Liked by ']"));
    const labels = likeElements
        .filter((element) => {
            if (!visible(element)) return false;
            const rect = element.getBoundingClientRect();
            return roots.some((root) => root.contains(element))
                || (rect.left >= window.innerWidth * 0.50
                    && rect.right >= window.innerWidth * 0.68);
        })
        .map((element) => normalize(element.getAttribute("aria-label")))
        .filter((label) => /^Liked by \S/i.test(label));

    const uniqueLabels = Array.from(new Set(labels)).sort();
    const open = closeControls.length > 0
        || activityHeadings.length > 0
        || activityRegions.length > 0
        || uniqueLabels.length > 0;

    return {
        open,
        labels: uniqueLabels,
        closeCount: closeControls.length,
        headingCount: activityHeadings.length,
        regionCount: activityRegions.length,
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

                self._summary["inspected"] += 1
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

    async def _process_current_photo(self, page: Page, photo_id: str) -> None:
        photo_url = page.url
        if f"/photo/{photo_id}" not in photo_url:
            self._summary["unknown"] += 1
            logger.warning(
                "Photo {} skipped safely because traversal URL did not match: {}",
                photo_id,
                photo_url,
            )
            return

        result = await self._probe_like_status(photo_url, photo_id)

        if result.status is LikeStatus.UNKNOWN:
            self._summary["unknown"] += 1
            logger.warning(
                "Photo {} like status is UNKNOWN; skipped without downloading. Reason: {}",
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

        filename = await self.download_manager.download_file()
        await self.database_manager.add_download(photo_id, filename)
        self._summary["downloaded"] += 1
        logger.info("Downloaded liked photo {} to {}.", photo_id, filename)

    async def _probe_like_status(self, photo_url: str, photo_id: str) -> LikeProbeResult:
        """Classify one photo in a disposable page that cannot contaminate traversal."""
        probe: Page | None = None
        try:
            probe = await self.browser_manager.context.new_page()
            await probe.goto(photo_url, wait_until="domcontentloaded", timeout=30_000)
            await self._wait_for_probe_ready(probe, photo_id)
            await self._show_viewer_controls(probe)

            baseline = await self._activity_state(probe)
            if baseline["labels"]:
                return LikeProbeResult(
                    LikeStatus.UNKNOWN,
                    reason="visible like evidence existed before View activity was invoked",
                )

            opened_state = await self._open_activity_surface(probe, baseline)
            if opened_state is None:
                return LikeProbeResult(
                    LikeStatus.UNKNOWN,
                    reason="View activity could not be opened and verified",
                )

            final_state = await self._wait_for_activity_to_settle(probe, opened_state)
            labels = tuple(final_state["labels"])
            logger.info(
                "Photo {} isolated activity evidence: open={}, labels={}, "
                "close_controls={}, headings={}, regions={}.",
                photo_id,
                final_state["open"],
                labels,
                final_state["closeCount"],
                final_state["headingCount"],
                final_state["regionCount"],
            )

            if not final_state["open"]:
                return LikeProbeResult(
                    LikeStatus.UNKNOWN,
                    reason="activity surface closed before classification completed",
                )

            if labels:
                # Confirm a positive decision in a second independent page. False positives
                # are more harmful than extra latency, so one probe is not enough to download.
                confirmation = await self._confirm_positive_like(photo_url, photo_id)
                if confirmation.status is not LikeStatus.LIKED:
                    return LikeProbeResult(
                        LikeStatus.UNKNOWN,
                        reason=(
                            "positive like evidence was not reproduced by an independent "
                            "confirmation probe: "
                            f"{confirmation.reason or confirmation.status.value}"
                        ),
                    )
                return LikeProbeResult(
                    LikeStatus.LIKED,
                    evidence=tuple(sorted(set(labels + confirmation.evidence))),
                )

            return LikeProbeResult(LikeStatus.NOT_LIKED)
        except Exception as exc:
            logger.debug("Like probe failed for {}: {}", photo_id, exc)
            return LikeProbeResult(LikeStatus.UNKNOWN, reason=str(exc))
        finally:
            if probe is not None and not probe.is_closed():
                await probe.close()

    async def _confirm_positive_like(
        self,
        photo_url: str,
        photo_id: str,
    ) -> LikeProbeResult:
        """Reproduce positive evidence in a second fresh page before downloading."""
        probe: Page | None = None
        try:
            probe = await self.browser_manager.context.new_page()
            await probe.goto(photo_url, wait_until="domcontentloaded", timeout=30_000)
            await self._wait_for_probe_ready(probe, photo_id)
            await self._show_viewer_controls(probe)

            baseline = await self._activity_state(probe)
            if baseline["labels"]:
                return LikeProbeResult(
                    LikeStatus.UNKNOWN,
                    reason="confirmation page had visible like evidence before invocation",
                )

            opened_state = await self._open_activity_surface(probe, baseline)
            if opened_state is None:
                return LikeProbeResult(
                    LikeStatus.UNKNOWN,
                    reason="confirmation activity surface could not be verified",
                )

            final_state = await self._wait_for_activity_to_settle(probe, opened_state)
            labels = tuple(final_state["labels"])
            if final_state["open"] and labels:
                return LikeProbeResult(LikeStatus.LIKED, evidence=labels)
            if final_state["open"]:
                return LikeProbeResult(
                    LikeStatus.NOT_LIKED,
                    reason="confirmation probe found no visible like evidence",
                )
            return LikeProbeResult(
                LikeStatus.UNKNOWN,
                reason="confirmation activity surface did not stay open",
            )
        except Exception as exc:
            return LikeProbeResult(LikeStatus.UNKNOWN, reason=str(exc))
        finally:
            if probe is not None and not probe.is_closed():
                await probe.close()

    async def _wait_for_probe_ready(self, page: Page, photo_id: str) -> None:
        if f"/photo/{photo_id}" not in page.url:
            raise RuntimeError(
                f"Probe opened the wrong photo. Expected {photo_id}, got {page.url}."
            )

        controls = page.locator(self._VIEW_ACTIVITY_SELECTOR)
        await controls.first.wait_for(state="attached", timeout=15_000)
        await page.wait_for_timeout(500)

    async def _open_activity_surface(
        self,
        page: Page,
        baseline: dict[str, Any],
    ) -> dict[str, Any] | None:
        controls = page.locator(self._VIEW_ACTIVITY_SELECTOR)
        count = min(await controls.count(), 20)

        for index in range(count):
            control = controls.nth(index)
            clicked = False
            try:
                if await control.is_visible() and await control.is_enabled():
                    await control.click(timeout=5_000)
                    clicked = True
            except Exception:
                clicked = False

            if not clicked:
                try:
                    clicked = bool(
                        await control.evaluate(
                            """element => {
                                if (!(element instanceof HTMLElement)) return false;
                                if (element.getAttribute('aria-disabled') === 'true') return false;
                                element.click();
                                return true;
                            }"""
                        )
                    )
                except Exception:
                    clicked = False

            if not clicked:
                continue

            for _ in range(20):
                state = await self._activity_state(page)
                if state["open"] and self._state_changed(baseline, state):
                    logger.debug(
                        "Verified View activity using control index {}.",
                        index,
                    )
                    return state
                await page.wait_for_timeout(150)

        return None

    async def _wait_for_activity_to_settle(
        self,
        page: Page,
        initial_state: dict[str, Any],
    ) -> dict[str, Any]:
        """Wait for a verified activity surface to remain stable before deciding."""
        previous = initial_state
        stable_samples = 0

        for _ in range(20):
            await page.wait_for_timeout(200)
            current = await self._activity_state(page)
            if not current["open"]:
                return current

            if self._state_signature(current) == self._state_signature(previous):
                stable_samples += 1
            else:
                stable_samples = 0

            previous = current
            if stable_samples >= 4:
                return current

        return previous

    async def _activity_state(self, page: Page) -> dict[str, Any]:
        state = await page.evaluate(self._ACTIVITY_STATE_SCRIPT)
        return {
            "open": bool(state.get("open")),
            "labels": list(state.get("labels") or []),
            "closeCount": int(state.get("closeCount") or 0),
            "headingCount": int(state.get("headingCount") or 0),
            "regionCount": int(state.get("regionCount") or 0),
        }

    def _state_changed(
        self,
        baseline: dict[str, Any],
        current: dict[str, Any],
    ) -> bool:
        return self._state_signature(baseline) != self._state_signature(current)

    def _state_signature(self, state: dict[str, Any]) -> tuple[Any, ...]:
        return (
            bool(state["open"]),
            tuple(state["labels"]),
            int(state["closeCount"]),
            int(state["headingCount"]),
            int(state["regionCount"]),
        )

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
