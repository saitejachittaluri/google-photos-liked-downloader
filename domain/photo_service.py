"""Application service that indexes liked photos from album activity and downloads them."""

from __future__ import annotations

import inspect
import re
from typing import Any
from urllib.parse import urlparse

from loguru import logger
from playwright.async_api import Locator, Page

from infrastructure.browser_manager import BrowserManager
from infrastructure.database_manager import DatabaseManager
from infrastructure.download_manager import DownloadManager
from infrastructure.navigation_engine import NavigationEngine


class PhotoService:
    """Download only photos referenced by the shared album's activity feed.

    Google Photos exposes "View activity" on the shared-album page, not reliably on an
    individually opened photo. The service therefore builds a read-only set of liked photo
    IDs from the album activity feed before opening the first photo. Traversal and download
    decisions then use only that immutable set.

    No Like, Unlike, Delete-like, upload, share, edit, or delete control is selected.
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

    _ACTIVITY_SNAPSHOT_SCRIPT = r"""
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
    const photoId = (href) => {
        if (!href) return null;
        try {
            const url = new URL(href, window.location.href);
            const match = url.pathname.match(/\/(?:photo|p)\/([^/?#]+)/);
            return match ? decodeURIComponent(match[1]) : null;
        } catch (_) {
            return null;
        }
    };

    const all = Array.from(document.querySelectorAll("*"));
    const activityHeadings = all.filter((element) => {
        if (!visible(element)) return false;
        const role = element.getAttribute("role");
        const tag = element.tagName.toLowerCase();
        if (!["h1", "h2", "h3", "h4"].includes(tag) && role !== "heading") return false;
        return /^(activity|comments?)$/i.test(normalize(element.textContent));
    });

    const closeControls = Array.from(document.querySelectorAll("[aria-label]")).filter(
        (element) => {
            if (!visible(element)) return false;
            const label = normalize(element.getAttribute("aria-label"));
            return /^(close|hide).*activity|activity.*(close|hide)$/i.test(label);
        }
    );

    const activityRegions = Array.from(document.querySelectorAll(
        "aside,[role='dialog'],[role='complementary'],[role='region']"
    )).filter((element) => {
        if (!visible(element)) return false;
        const text = normalize(element.textContent).slice(0, 1500);
        return /\b(activity|liked by|liked (?:a|this|your) photo|comments?)\b/i.test(text);
    });

    const roots = [];
    const addRoot = (source) => {
        let node = source;
        for (let depth = 0; node && depth < 12; depth += 1, node = node.parentElement) {
            if (!visible(node)) continue;
            const rect = node.getBoundingClientRect();
            if (rect.width >= 240 && rect.height >= 160) {
                roots.push(node);
                return;
            }
        }
    };
    activityHeadings.forEach(addRoot);
    closeControls.forEach(addRoot);
    activityRegions.forEach((element) => roots.push(element));

    const uniqueRoots = Array.from(new Set(roots));
    const routeLooksLikeActivity = /\/activit(?:y|ies)(?:\/|$)/i.test(
        window.location.pathname
    );
    const activityOpen = routeLooksLikeActivity
        || uniqueRoots.length > 0
        || activityHeadings.length > 0
        || closeControls.length > 0;

    const searchRoots = uniqueRoots.length > 0
        ? uniqueRoots
        : (activityOpen ? [document.body] : []);

    const likeSignals = [];
    for (const root of searchRoots) {
        for (const element of root.querySelectorAll("[aria-label]")) {
            if (!visible(element)) continue;
            const label = normalize(element.getAttribute("aria-label"));
            if (/^Liked by \S/i.test(label)
                    || /\bliked (?:a|this|your) photo\b/i.test(label)) {
                likeSignals.push(element);
            }
        }

        for (const element of root.querySelectorAll("div,span,p")) {
            if (!visible(element)) continue;
            const text = normalize(element.textContent);
            if (text.length > 240) continue;
            if (/\bliked (?:a|this|your) photo(?:s)?\b/i.test(text)
                    || /\bliked \d+ photo(?:s)?\b/i.test(text)) {
                likeSignals.push(element);
            }
        }
    }

    const ids = new Set();
    const unresolved = [];
    const signalLabels = [];

    for (const signal of Array.from(new Set(likeSignals))) {
        const descriptor = normalize(
            signal.getAttribute("aria-label") || signal.textContent
        ).slice(0, 220);
        if (descriptor) signalLabels.push(descriptor);

        let node = signal;
        let mapped = [];
        for (let depth = 0; node && depth < 14; depth += 1, node = node.parentElement) {
            const candidates = [];
            if (node.matches && node.matches("a[href]")) candidates.push(node);
            candidates.push(...node.querySelectorAll("a[href]"));

            const nodeIds = Array.from(new Set(
                candidates.map((anchor) => photoId(anchor.href)).filter(Boolean)
            ));
            if (nodeIds.length > 0 && nodeIds.length <= 100) {
                mapped = nodeIds;
                break;
            }
        }

        if (mapped.length === 0) {
            unresolved.push(descriptor || "<unlabelled like activity>");
        } else {
            mapped.forEach((id) => ids.add(id));
        }
    }

    const scrollables = [];
    for (const element of all) {
        if (!visible(element)) continue;
        const style = window.getComputedStyle(element);
        if (!/(auto|scroll)/.test(style.overflowY)) continue;
        if (element.scrollHeight <= element.clientHeight + 40) continue;

        const text = normalize(element.textContent).slice(0, 1200);
        const containsActivity = /\b(activity|liked by|liked (?:a|this|your) photo)\b/i.test(text);
        const containsRoot = uniqueRoots.some(
            (root) => element === root || element.contains(root) || root.contains(element)
        );
        if (!containsActivity && !containsRoot) continue;

        const rect = element.getBoundingClientRect();
        const score = (containsRoot ? 1000 : 0)
            + (containsActivity ? 500 : 0)
            + Math.max(0, element.scrollHeight - element.clientHeight)
            + (rect.left > window.innerWidth * 0.35 ? 200 : 0);
        scrollables.push({element, score});
    }

    scrollables.sort((a, b) => b.score - a.score);
    const scroller = scrollables.length > 0 ? scrollables[0].element : null;

    return {
        open: activityOpen,
        ids: Array.from(ids).sort(),
        unresolved: Array.from(new Set(unresolved)).slice(0, 20),
        signals: Array.from(new Set(signalLabels)).slice(0, 20),
        headingCount: activityHeadings.length,
        closeCount: closeControls.length,
        regionCount: activityRegions.length,
        scrollableCount: scrollables.length,
        scrollTop: scroller ? scroller.scrollTop : window.scrollY,
        scrollHeight: scroller ? scroller.scrollHeight : document.documentElement.scrollHeight,
        clientHeight: scroller ? scroller.clientHeight : window.innerHeight,
        atEnd: scroller
            ? scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 8
            : window.scrollY + window.innerHeight
                >= document.documentElement.scrollHeight - 8,
    };
}
"""

    _SCROLL_ACTIVITY_SCRIPT = r"""
() => {
    const visible = (element) => {
        if (!(element instanceof Element)) return false;
        const style = window.getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== "none"
            && style.visibility !== "hidden"
            && rect.width > 0
            && rect.height > 0;
    };
    const normalize = (value) => (value || "").replace(/\s+/g, " ").trim();

    const candidates = Array.from(document.querySelectorAll("*"))
        .filter((element) => {
            if (!visible(element)) return false;
            const style = window.getComputedStyle(element);
            if (!/(auto|scroll)/.test(style.overflowY)) return false;
            if (element.scrollHeight <= element.clientHeight + 40) return false;
            const text = normalize(element.textContent).slice(0, 1200);
            return /\b(activity|liked by|liked (?:a|this|your) photo)\b/i.test(text);
        })
        .map((element) => {
            const rect = element.getBoundingClientRect();
            const score = Math.max(0, element.scrollHeight - element.clientHeight)
                + (rect.left > window.innerWidth * 0.35 ? 500 : 0);
            return {element, score};
        })
        .sort((a, b) => b.score - a.score);

    if (candidates.length > 0) {
        const element = candidates[0].element;
        const before = element.scrollTop;
        element.scrollTop = Math.min(
            element.scrollHeight,
            element.scrollTop + Math.max(300, Math.floor(element.clientHeight * 0.82))
        );
        element.dispatchEvent(new Event("scroll", {bubbles: true}));
        return {
            target: "element",
            before,
            after: element.scrollTop,
            atEnd: element.scrollTop + element.clientHeight >= element.scrollHeight - 8,
        };
    }

    const before = window.scrollY;
    window.scrollBy(0, Math.max(400, Math.floor(window.innerHeight * 0.82)));
    return {
        target: "window",
        before,
        after: window.scrollY,
        atEnd: window.scrollY + window.innerHeight
            >= document.documentElement.scrollHeight - 8,
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
            self._liked_photo_ids = await self._index_liked_photos(page)

            # Reset to the clean album grid. No activity panel state is carried into the
            # photo viewer, and no second tab is used.
            await page.goto(
                shared_album_url,
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            await page.wait_for_timeout(800)
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
                "Run summary: indexed_liked={}, inspected={}, liked={}, not_liked={}, "
                "downloaded={}, already_downloaded={}.",
                len(self._liked_photo_ids),
                self._summary["inspected"],
                self._summary["liked"],
                self._summary["not_liked"],
                self._summary["downloaded"],
                self._summary["already_downloaded"],
            )

    async def _index_liked_photos(self, page: Page) -> set[str]:
        """Build the complete liked-photo set from the album-level activity feed."""
        await page.bring_to_front()
        await page.wait_for_timeout(700)
        await self._show_album_controls(page)

        opened = await self._open_album_activity(page)
        if not opened:
            controls = await self._visible_control_labels(page)
            raise RuntimeError(
                "The shared album's View activity surface could not be opened. "
                f"Visible controls were: {controls}"
            )

        liked_ids: set[str] = set()
        unresolved: set[str] = set()
        previous_signature: tuple[Any, ...] | None = None
        stable_rounds = 0

        for round_number in range(1, 301):
            snapshot = await page.evaluate(self._ACTIVITY_SNAPSHOT_SCRIPT)
            if not snapshot.get("open"):
                raise RuntimeError(
                    "The album activity surface closed while likes were being indexed."
                )

            liked_ids.update(str(value) for value in snapshot.get("ids", []))
            unresolved.update(str(value) for value in snapshot.get("unresolved", []))

            signature = (
                len(liked_ids),
                len(unresolved),
                int(snapshot.get("scrollTop", 0)),
                int(snapshot.get("scrollHeight", 0)),
            )
            if signature == previous_signature:
                stable_rounds += 1
            else:
                stable_rounds = 0
            previous_signature = signature

            logger.debug(
                "Album activity scan round {}: liked_ids={}, unresolved={}, "
                "signals={}, scroll={}/{}, at_end={}.",
                round_number,
                len(liked_ids),
                len(unresolved),
                snapshot.get("signals", []),
                snapshot.get("scrollTop", 0),
                snapshot.get("scrollHeight", 0),
                snapshot.get("atEnd", False),
            )

            if snapshot.get("atEnd") and stable_rounds >= 4:
                break

            scroll_result = await page.evaluate(self._SCROLL_ACTIVITY_SCRIPT)
            await page.wait_for_timeout(450)

            if (
                bool(scroll_result.get("atEnd"))
                and int(scroll_result.get("after", 0))
                == int(scroll_result.get("before", 0))
            ):
                stable_rounds += 1
        else:
            raise RuntimeError(
                "Album activity indexing exceeded the safety limit before reaching the end."
            )

        if unresolved:
            samples = sorted(unresolved)[:10]
            raise RuntimeError(
                "Like activity was found, but some liked entries could not be mapped to "
                f"photo URLs. No downloads were attempted. Examples: {samples}"
            )

        logger.info(
            "Indexed {} unique liked photos from the shared album activity feed.",
            len(liked_ids),
        )
        if not liked_ids:
            logger.warning(
                "The album activity feed contained no mappable liked-photo entries. "
                "The run will inspect the album but download nothing."
            )

        invalid = [value for value in liked_ids if not re.fullmatch(r"[A-Za-z0-9_-]{8,}", value)]
        if invalid:
            raise RuntimeError(
                f"Activity indexing returned invalid photo identifiers: {invalid[:5]}"
            )

        return liked_ids

    async def _open_album_activity(self, page: Page) -> bool:
        baseline_url = page.url

        direct = await self._first_visible(
            tuple(page.locator(selector) for selector in self._VIEW_ACTIVITY_SELECTORS)
        )
        if direct is not None:
            try:
                await direct.click(timeout=8_000)
                if await self._wait_for_activity_surface(page, baseline_url):
                    return True
            except Exception:
                pass

        more = await self._first_visible(
            tuple(page.locator(selector) for selector in self._MORE_OPTIONS_SELECTORS)
        )
        if more is not None:
            try:
                await more.click(timeout=8_000)
                await page.locator("[role='menu']:visible").first.wait_for(
                    state="visible",
                    timeout=4_000,
                )

                menu_items = (
                    page.locator("[role='menuitem'][aria-label='View activity']"),
                    page.locator("[role='menuitem'][aria-label^='View activity']"),
                    page.get_by_role("menuitem", name="View activity", exact=True),
                )
                activity_item = await self._first_visible(menu_items)
                if activity_item is not None:
                    await activity_item.click(timeout=8_000)
                    if await self._wait_for_activity_surface(page, baseline_url):
                        return True
            except Exception:
                await self._dismiss_menu(page)

        return False

    async def _wait_for_activity_surface(self, page: Page, baseline_url: str) -> bool:
        for _ in range(50):
            snapshot = await page.evaluate(self._ACTIVITY_SNAPSHOT_SCRIPT)
            if snapshot.get("open"):
                return True
            if page.url != baseline_url and re.search(
                r"/activit(?:y|ies)(?:/|$)",
                urlparse(page.url).path,
                re.IGNORECASE,
            ):
                return True
            await page.wait_for_timeout(200)
        return False

    async def _process_current_photo(self, page: Page, photo_id: str) -> None:
        liked = photo_id in self._liked_photo_ids
        await self.database_manager.add_photo(photo_id, page.url, liked)

        if not liked:
            self._summary["not_liked"] += 1
            logger.info("Photo {} is not present in album like activity; skipping.", photo_id)
            return

        self._summary["liked"] += 1
        logger.info("Photo {} is confirmed liked by album activity index.", photo_id)

        if await self._is_photo_downloaded(photo_id):
            self._summary["already_downloaded"] += 1
            logger.info("Photo {} was already downloaded; skipping duplicate.", photo_id)
            return

        if self.dry_run:
            logger.info("[dry-run] Photo {} is liked; download skipped.", photo_id)
            return

        current_id = await self.navigation_engine.current_photo_id()
        if current_id != photo_id:
            raise RuntimeError(
                f"Viewer changed from {photo_id} to {current_id}; download refused."
            )

        filename = await self.download_manager.download_file()
        await self.database_manager.add_download(photo_id, filename)
        self._summary["downloaded"] += 1
        logger.info("Downloaded liked photo {} to {}.", photo_id, filename)

    async def _show_album_controls(self, page: Page) -> None:
        viewport = page.viewport_size or {"width": 1440, "height": 1000}
        await page.mouse.move(viewport["width"] // 2, 100)
        await page.wait_for_timeout(150)
        await page.mouse.move(viewport["width"] - 120, 50)
        await page.wait_for_timeout(600)

    async def _first_visible(
        self,
        candidates: tuple[Locator, ...],
    ) -> Locator | None:
        for candidate in candidates:
            count = min(await candidate.count(), 20)
            for index in range(count):
                item = candidate.nth(index)
                try:
                    if await item.is_visible() and await item.is_enabled():
                        return item
                except Exception:
                    continue
        return None

    async def _visible_control_labels(self, page: Page) -> list[str]:
        labels = await page.locator("[aria-label]:visible, [title]:visible").evaluate_all(
            """elements => Array.from(new Set(elements.map(element =>
                (element.getAttribute('aria-label')
                    || element.getAttribute('title')
                    || '').replace(/\\s+/g, ' ').trim()
            ).filter(Boolean))).slice(0, 80)"""
        )
        return [str(label) for label in labels]

    async def _dismiss_menu(self, page: Page) -> None:
        try:
            if await page.locator("[role='menu']:visible").count() > 0:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(100)
        except Exception:
            logger.debug("Unable to dismiss the album menu cleanly.")

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
