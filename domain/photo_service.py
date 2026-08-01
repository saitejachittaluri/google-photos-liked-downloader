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
    """Download photos liked by at least one participant in a shared album.

    Google Photos exposes participant-like history through the shared album's activity
    surface. The service indexes that feed from its oldest rendered entries through its
    newest entries, then traverses the album and downloads only exact matching photo IDs.

    The activity feed can open near its bottom and is virtualized. Therefore, treating an
    initial ``atEnd`` state as complete misses older likes. This implementation first
    rewinds repeatedly until the top is stable, then scans forward to the stable bottom.

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
    const photoId = (value) => {
        if (!value) return null;
        try {
            const url = new URL(value, window.location.href);
            const match = url.pathname.match(/\/(?:photo|p)\/([^/?#]+)/);
            return match ? decodeURIComponent(match[1]) : null;
        } catch (_) {
            return null;
        }
    };

    const labelled = Array.from(document.querySelectorAll("[aria-label]"));
    const all = Array.from(document.querySelectorAll("*"));

    const headings = all.filter((element) => {
        if (!visible(element)) return false;
        const role = element.getAttribute("role");
        const tag = element.tagName.toLowerCase();
        if (!["h1", "h2", "h3", "h4"].includes(tag) && role !== "heading") return false;
        return /^(activity|comments?)$/i.test(normalize(element.textContent));
    });

    const closeControls = labelled.filter((element) => {
        if (!visible(element)) return false;
        const label = normalize(element.getAttribute("aria-label"));
        return /^(close|hide).*activity|activity.*(close|hide)$/i.test(label);
    });

    const activityRegions = Array.from(document.querySelectorAll(
        "aside,[role='dialog'],[role='complementary'],[role='region']"
    )).filter((element) => {
        if (!visible(element)) return false;
        const text = normalize(element.textContent).slice(0, 1800);
        return /\b(activity|liked by|liked (?:a|this|your) photo|comments?)\b/i.test(text);
    });

    const activityOpen = /\/activit(?:y|ies)(?:\/|$)/i.test(window.location.pathname)
        || headings.length > 0
        || closeControls.length > 0
        || activityRegions.length > 0;

    const rootCandidates = [];
    const addRoot = (source) => {
        let node = source;
        for (let depth = 0; node && depth < 12; depth += 1, node = node.parentElement) {
            if (!visible(node)) continue;
            const rect = node.getBoundingClientRect();
            if (rect.width >= 240 && rect.height >= 160) {
                rootCandidates.push(node);
                return;
            }
        }
    };
    headings.forEach(addRoot);
    closeControls.forEach(addRoot);
    activityRegions.forEach((element) => rootCandidates.push(element));
    const roots = Array.from(new Set(rootCandidates));
    const searchRoots = roots.length > 0 ? roots : (activityOpen ? [document.body] : []);

    const likeSignals = [];
    for (const root of searchRoots) {
        for (const element of root.querySelectorAll("[aria-label]")) {
            if (!visible(element)) continue;
            const label = normalize(element.getAttribute("aria-label"));
            if (/^Liked by\b/i.test(label)
                    || /\b(?:liked|likes) (?:a|this|your|the) photo(?:s)?\b/i.test(label)) {
                likeSignals.push(element);
            }
        }

        for (const element of root.querySelectorAll("div,span,p")) {
            if (!visible(element)) continue;
            const text = normalize(element.textContent);
            if (!text || text.length > 260) continue;
            if (/\b(?:liked|likes) (?:a|this|your|the) photo(?:s)?\b/i.test(text)
                    || /\bliked \d+ photo(?:s)?\b/i.test(text)
                    || /^Liked by\b/i.test(text)) {
                likeSignals.push(element);
            }
        }
    }

    const ids = new Set();
    const unresolved = [];
    const signalLabels = [];

    const idsWithin = (node) => {
        const values = [];
        if (node.matches && node.matches("a[href], [data-href], [data-url]")) {
            values.push(
                node.getAttribute("href"),
                node.getAttribute("data-href"),
                node.getAttribute("data-url")
            );
        }
        for (const item of node.querySelectorAll("a[href], [data-href], [data-url]")) {
            values.push(
                item.getAttribute("href"),
                item.getAttribute("data-href"),
                item.getAttribute("data-url")
            );
        }
        return Array.from(new Set(values.map(photoId).filter(Boolean)));
    };

    for (const signal of Array.from(new Set(likeSignals))) {
        const descriptor = normalize(
            signal.getAttribute("aria-label") || signal.textContent
        ).slice(0, 220);
        if (descriptor) signalLabels.push(descriptor);

        let mapped = [];
        let node = signal;
        for (let depth = 0; node && depth < 14; depth += 1, node = node.parentElement) {
            const found = idsWithin(node);
            if (found.length > 0) {
                // Use the smallest ancestor containing photo links. A large ancestor is
                // usually the entire feed and would falsely associate unrelated photos.
                if (found.length <= 20) mapped = found;
                break;
            }
        }

        if (mapped.length === 0) {
            unresolved.push(descriptor || "<unlabelled like activity>");
        } else {
            mapped.forEach((id) => ids.add(id));
        }
    }

    const scrollables = all
        .filter((element) => {
            if (!visible(element)) return false;
            const style = window.getComputedStyle(element);
            return /(auto|scroll)/.test(style.overflowY)
                && element.scrollHeight > element.clientHeight + 30;
        })
        .map((element) => {
            const rect = element.getBoundingClientRect();
            const text = normalize(element.textContent).slice(0, 1600);
            const containsActivity = /\b(activity|liked by|liked (?:a|this|your|the) photo)\b/i.test(text);
            const containsRoot = roots.some(
                (root) => element === root || element.contains(root) || root.contains(element)
            );
            const score = (containsRoot ? 100000 : 0)
                + (containsActivity ? 50000 : 0)
                + (rect.left > window.innerWidth * 0.30 ? 5000 : 0)
                + Math.max(0, element.scrollHeight - element.clientHeight);
            return {element, score};
        })
        .sort((a, b) => b.score - a.score);

    const scroller = scrollables.length > 0 ? scrollables[0].element : null;
    const scrollTop = scroller ? scroller.scrollTop : window.scrollY;
    const scrollHeight = scroller
        ? scroller.scrollHeight
        : document.documentElement.scrollHeight;
    const clientHeight = scroller ? scroller.clientHeight : window.innerHeight;

    return {
        open: activityOpen,
        ids: Array.from(ids).sort(),
        unresolved: Array.from(new Set(unresolved)).slice(0, 50),
        signals: Array.from(new Set(signalLabels)).slice(0, 30),
        scrollTop,
        scrollHeight,
        clientHeight,
        atTop: scrollTop <= 8,
        atEnd: scrollTop + clientHeight >= scrollHeight - 8,
        scrollerFound: Boolean(scroller),
    };
}
"""

    _SCROLL_ACTIVITY_TOP_SCRIPT = r"""
() => {
    const visible = (element) => {
        if (!(element instanceof Element)) return false;
        const style = window.getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden"
            && rect.width > 0 && rect.height > 0;
    };
    const normalize = (value) => (value || "").replace(/\s+/g, " ").trim();
    const candidates = Array.from(document.querySelectorAll("*"))
        .filter((element) => {
            if (!visible(element)) return false;
            const style = window.getComputedStyle(element);
            return /(auto|scroll)/.test(style.overflowY)
                && element.scrollHeight > element.clientHeight + 30;
        })
        .map((element) => {
            const rect = element.getBoundingClientRect();
            const text = normalize(element.textContent).slice(0, 1600);
            const activity = /\b(activity|liked by|liked (?:a|this|your|the) photo)\b/i.test(text);
            const score = (activity ? 50000 : 0)
                + (rect.left > window.innerWidth * 0.30 ? 5000 : 0)
                + Math.max(0, element.scrollHeight - element.clientHeight);
            return {element, score};
        })
        .sort((a, b) => b.score - a.score);

    if (candidates.length > 0) {
        const element = candidates[0].element;
        const before = element.scrollTop;
        const heightBefore = element.scrollHeight;
        element.scrollTop = 0;
        element.dispatchEvent(new Event("scroll", {bubbles: true}));
        return {target: "element", before, after: element.scrollTop, heightBefore};
    }

    const before = window.scrollY;
    const heightBefore = document.documentElement.scrollHeight;
    window.scrollTo(0, 0);
    return {target: "window", before, after: window.scrollY, heightBefore};
}
"""

    _SCROLL_ACTIVITY_DOWN_SCRIPT = r"""
() => {
    const visible = (element) => {
        if (!(element instanceof Element)) return false;
        const style = window.getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden"
            && rect.width > 0 && rect.height > 0;
    };
    const normalize = (value) => (value || "").replace(/\s+/g, " ").trim();
    const candidates = Array.from(document.querySelectorAll("*"))
        .filter((element) => {
            if (!visible(element)) return false;
            const style = window.getComputedStyle(element);
            return /(auto|scroll)/.test(style.overflowY)
                && element.scrollHeight > element.clientHeight + 30;
        })
        .map((element) => {
            const rect = element.getBoundingClientRect();
            const text = normalize(element.textContent).slice(0, 1600);
            const activity = /\b(activity|liked by|liked (?:a|this|your|the) photo)\b/i.test(text);
            const score = (activity ? 50000 : 0)
                + (rect.left > window.innerWidth * 0.30 ? 5000 : 0)
                + Math.max(0, element.scrollHeight - element.clientHeight);
            return {element, score};
        })
        .sort((a, b) => b.score - a.score);

    if (candidates.length > 0) {
        const element = candidates[0].element;
        const before = element.scrollTop;
        element.scrollTop = Math.min(
            element.scrollHeight,
            element.scrollTop + Math.max(280, Math.floor(element.clientHeight * 0.72))
        );
        element.dispatchEvent(new Event("scroll", {bubbles: true}));
        return {
            target: "element",
            before,
            after: element.scrollTop,
            height: element.scrollHeight,
            atEnd: element.scrollTop + element.clientHeight >= element.scrollHeight - 8,
        };
    }

    const before = window.scrollY;
    window.scrollBy(0, Math.max(350, Math.floor(window.innerHeight * 0.72)));
    return {
        target: "window",
        before,
        after: window.scrollY,
        height: document.documentElement.scrollHeight,
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
        """Index all photos liked by anyone from the complete album activity history."""
        await page.bring_to_front()
        await page.wait_for_timeout(700)
        await self._show_album_controls(page)

        if not await self._open_album_activity(page):
            controls = await self._visible_control_labels(page)
            raise RuntimeError(
                "The shared album's View activity surface could not be opened. "
                f"Visible controls were: {controls}"
            )

        liked_ids: set[str] = set()
        unresolved: set[str] = set()

        def absorb(snapshot: dict[str, Any]) -> None:
            liked_ids.update(str(value) for value in snapshot.get("ids", []))
            unresolved.update(str(value) for value in snapshot.get("unresolved", []))

        logger.info("Rewinding album activity to its oldest available entries.")
        stable_top_rounds = 0
        previous_top_signature: tuple[Any, ...] | None = None

        for round_number in range(1, 121):
            before = await page.evaluate(self._ACTIVITY_SNAPSHOT_SCRIPT)
            if not before.get("open"):
                raise RuntimeError("The album activity surface closed during rewind.")
            absorb(before)

            await page.evaluate(self._SCROLL_ACTIVITY_TOP_SCRIPT)
            await page.wait_for_timeout(600)

            after = await page.evaluate(self._ACTIVITY_SNAPSHOT_SCRIPT)
            if not after.get("open"):
                raise RuntimeError("The album activity surface closed during rewind.")
            absorb(after)

            signature = (
                bool(after.get("atTop")),
                int(after.get("scrollHeight", 0)),
                len(liked_ids),
                len(unresolved),
            )
            if bool(after.get("atTop")) and signature == previous_top_signature:
                stable_top_rounds += 1
            else:
                stable_top_rounds = 0
            previous_top_signature = signature

            logger.debug(
                "Activity rewind round {}: liked_ids={}, unresolved={}, "
                "scroll={}/{}, at_top={}.",
                round_number,
                len(liked_ids),
                len(unresolved),
                after.get("scrollTop", 0),
                after.get("scrollHeight", 0),
                after.get("atTop", False),
            )

            if stable_top_rounds >= 4:
                break
        else:
            raise RuntimeError(
                "Could not reach a stable top of the album activity feed. "
                "No downloads were attempted."
            )

        logger.info("Scanning album activity from oldest to newest entries.")
        stable_end_rounds = 0
        previous_end_signature: tuple[Any, ...] | None = None

        for round_number in range(1, 501):
            snapshot = await page.evaluate(self._ACTIVITY_SNAPSHOT_SCRIPT)
            if not snapshot.get("open"):
                raise RuntimeError("The album activity surface closed during forward scan.")
            absorb(snapshot)

            signature = (
                bool(snapshot.get("atEnd")),
                int(snapshot.get("scrollTop", 0)),
                int(snapshot.get("scrollHeight", 0)),
                len(liked_ids),
                len(unresolved),
            )
            if bool(snapshot.get("atEnd")) and signature == previous_end_signature:
                stable_end_rounds += 1
            else:
                stable_end_rounds = 0
            previous_end_signature = signature

            logger.debug(
                "Activity forward round {}: liked_ids={}, unresolved={}, "
                "signals={}, scroll={}/{}, at_end={}.",
                round_number,
                len(liked_ids),
                len(unresolved),
                snapshot.get("signals", []),
                snapshot.get("scrollTop", 0),
                snapshot.get("scrollHeight", 0),
                snapshot.get("atEnd", False),
            )

            if stable_end_rounds >= 4:
                break

            await page.evaluate(self._SCROLL_ACTIVITY_DOWN_SCRIPT)
            await page.wait_for_timeout(450)
        else:
            raise RuntimeError(
                "Album activity scanning exceeded its safety limit before reaching a "
                "stable end. No downloads were attempted."
            )

        if unresolved:
            samples = sorted(unresolved)[:10]
            raise RuntimeError(
                "Like activity was detected but could not be mapped safely to every photo. "
                "No downloads were attempted. Examples: "
                f"{samples}"
            )

        invalid = [
            value
            for value in liked_ids
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,}", value)
        ]
        if invalid:
            raise RuntimeError(
                f"Activity indexing returned invalid photo identifiers: {invalid[:5]}"
            )

        logger.info(
            "Indexed {} unique photos liked by at least one album participant.",
            len(liked_ids),
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
                items = (
                    page.locator("[role='menuitem'][aria-label='View activity']"),
                    page.locator("[role='menuitem'][aria-label^='View activity']"),
                    page.get_by_role("menuitem", name="View activity", exact=True),
                )
                activity_item = await self._first_visible(items)
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
