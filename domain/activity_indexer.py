"""Read-only indexing of participant likes from a Google Photos album activity feed."""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlparse

from loguru import logger
from playwright.async_api import Locator, Page


class AlbumActivityIndexer:
    """Build an exact set of photo IDs liked by at least one album participant."""

    VIEW_ACTIVITY_SELECTORS = (
        "[aria-label='View activity']",
        "button[title='View activity']",
        "[role='button'][title='View activity']",
    )
    MORE_OPTIONS_SELECTORS = (
        "[aria-label='More options']",
        "button[title='More options']",
        "[role='button'][title='More options']",
    )

    # snapshot/top/down deliberately share one scroller-selection algorithm. The activity
    # container must be structurally connected to the Activity heading/region. A tall album
    # grid behind the pane is never selected merely because its scrollHeight is larger.
    ACTIVITY_SCRIPT = r"""
(action) => {
  const visible = e => {
    if (!(e instanceof Element)) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden'
      && Number.parseFloat(s.opacity || '1') > .01
      && r.width > 0 && r.height > 0 && r.right > 0 && r.bottom > 0
      && r.left < innerWidth && r.top < innerHeight;
  };
  const norm = v => (v || '').replace(/\s+/g, ' ').trim();
  const scrollable = e => {
    if (!(e instanceof Element) || !visible(e)) return false;
    const s = getComputedStyle(e);
    return /(auto|scroll)/.test(s.overflowY) && e.scrollHeight > e.clientHeight + 24;
  };
  const photoId = value => {
    if (!value) return null;
    try {
      const u = new URL(value, location.href);
      const m = u.pathname.match(/\/(?:photo|p)\/([^/?#]+)/);
      return m ? decodeURIComponent(m[1]) : null;
    } catch (_) { return null; }
  };

  const all = Array.from(document.querySelectorAll('*'));
  const labelled = Array.from(document.querySelectorAll('[aria-label]'));
  const headings = all.filter(e => {
    if (!visible(e)) return false;
    const tag = e.tagName.toLowerCase(), role = e.getAttribute('role');
    return (['h1','h2','h3','h4'].includes(tag) || role === 'heading')
      && /^(activity|comments?)$/i.test(norm(e.textContent));
  });
  const closes = labelled.filter(e => visible(e)
    && /^(close|hide).*activity|activity.*(close|hide)$/i.test(norm(e.getAttribute('aria-label'))));
  const regions = Array.from(document.querySelectorAll(
    "aside,[role='dialog'],[role='complementary'],[role='region']"
  )).filter(e => visible(e)
    && /\b(activity|liked by|liked (?:a|this|your|the) photo|comments?)\b/i
      .test(norm(e.textContent).slice(0, 1800)));
  const routeActivity = /\/activit(?:y|ies)(?:\/|$)/i.test(location.pathname);
  const open = routeActivity || headings.length > 0 || closes.length > 0 || regions.length > 0;

  const roots = [];
  const addRoot = source => {
    let n = source;
    for (let d = 0; n && d < 12; d += 1, n = n.parentElement) {
      if (!visible(n)) continue;
      const r = n.getBoundingClientRect();
      if (r.width >= 220 && r.height >= 140) { roots.push(n); return; }
    }
  };
  headings.forEach(addRoot); closes.forEach(addRoot); regions.forEach(e => roots.push(e));
  const uniqueRoots = Array.from(new Set(roots));

  const candidates = new Map();
  const addScroller = (e, priority, distance, source) => {
    if (!scrollable(e)) return;
    const r = e.getBoundingClientRect();
    const score = priority * 1000000 - distance * 10000
      + (r.left >= innerWidth * .30 ? 500 : 0)
      + Math.min(e.scrollHeight - e.clientHeight, 5000);
    const old = candidates.get(e);
    if (!old || score > old.score) candidates.set(e, {element:e, score, source, distance});
  };
  for (const root of uniqueRoots) {
    let n = root;
    for (let d = 0; n && d < 14; d += 1, n = n.parentElement) {
      addScroller(n, 4, d, 'activity-ancestor');
    }
    let d = 0;
    for (const child of root.querySelectorAll('*')) {
      if (scrollable(child)) addScroller(child, 5, d++, 'activity-descendant');
    }
  }
  if (routeActivity) {
    const e = document.scrollingElement || document.documentElement;
    if (e.scrollHeight > innerHeight + 24) addScroller(e, 1, 0, 'activity-route-document');
  }
  const ranked = Array.from(candidates.values()).sort((a,b) => b.score - a.score);
  const meta = ranked[0] || null, scroller = meta ? meta.element : null;

  const searchRoots = uniqueRoots.length ? uniqueRoots : (open ? [document.body] : []);
  const signals = [];
  for (const root of searchRoots) {
    const aria = [];
    if (root.matches && root.matches('[aria-label]')) aria.push(root);
    aria.push(...root.querySelectorAll('[aria-label]'));
    for (const e of aria) {
      if (!visible(e)) continue;
      const label = norm(e.getAttribute('aria-label'));
      if (/^Liked by\b/i.test(label)
          || /\b(?:liked|likes) (?:a|this|your|the) photo(?:s)?\b/i.test(label)) signals.push(e);
    }
    for (const e of root.querySelectorAll('div,span,p')) {
      if (!visible(e)) continue;
      const text = norm(e.textContent);
      if (!text || text.length > 260) continue;
      if (/^Liked by\b/i.test(text)
          || /\b(?:liked|likes) (?:a|this|your|the) photo(?:s)?\b/i.test(text)
          || /\bliked \d+ photo(?:s)?\b/i.test(text)) signals.push(e);
    }
  }

  const idsWithin = node => {
    const values = [];
    const selector = 'a[href],[data-href],[data-url]';
    if (node.matches && node.matches(selector)) {
      values.push(node.getAttribute('href'), node.getAttribute('data-href'), node.getAttribute('data-url'));
    }
    for (const e of node.querySelectorAll(selector)) {
      values.push(e.getAttribute('href'), e.getAttribute('data-href'), e.getAttribute('data-url'));
    }
    return Array.from(new Set(values.map(photoId).filter(Boolean)));
  };

  const ids = new Set(), unresolved = [], labels = [];
  for (const signal of Array.from(new Set(signals))) {
    const label = norm(signal.getAttribute('aria-label') || signal.textContent).slice(0, 220);
    if (label) labels.push(label);
    let mapped = [], n = signal;
    for (let d = 0; n && d < 14; d += 1, n = n.parentElement) {
      const found = idsWithin(n);
      if (found.length) { if (found.length <= 20) mapped = found; break; }
    }
    if (mapped.length) mapped.forEach(id => ids.add(id));
    else unresolved.push(label || '<unlabelled like activity>');
  }

  const isDocument = scroller && scroller === document.scrollingElement;
  const top = scroller ? (isDocument ? scrollY : scroller.scrollTop) : 0;
  const height = scroller ? (isDocument ? document.documentElement.scrollHeight : scroller.scrollHeight) : innerHeight;
  const client = scroller ? (isDocument ? innerHeight : scroller.clientHeight) : innerHeight;

  if (action === 'top' && scroller) {
    if (isDocument) scrollTo(0, 0);
    else { scroller.scrollTop = 0; scroller.dispatchEvent(new Event('scroll', {bubbles:true})); }
  }
  if (action === 'down' && scroller) {
    const step = Math.max(420, Math.floor(client * .82));
    if (isDocument) scrollBy(0, step);
    else {
      scroller.scrollTop = Math.min(height, top + step);
      scroller.dispatchEvent(new Event('scroll', {bubbles:true}));
    }
  }
  const after = scroller ? (isDocument ? scrollY : scroller.scrollTop) : 0;
  const descriptor = scroller ? {
    tag: scroller.tagName.toLowerCase(), role: scroller.getAttribute('role') || '',
    ariaLabel: scroller.getAttribute('aria-label') || '', source: meta.source,
    distance: meta.distance, left: Math.round(scroller.getBoundingClientRect().left),
    width: Math.round(scroller.getBoundingClientRect().width),
    clientHeight: client, scrollHeight: height
  } : null;

  return {
    open, ids:Array.from(ids).sort(),
    unresolved:Array.from(new Set(unresolved)).slice(0, 50),
    signals:Array.from(new Set(labels)).slice(0, 30),
    scrollerFound:Boolean(scroller), scroller:descriptor,
    scrollTop:top, scrollTopAfterAction:after, scrollHeight:height, clientHeight:client,
    atTop:top <= 8, atEnd:top + client >= height - 8
  };
}
"""

    async def index(self, page: Page) -> set[str]:
        await page.bring_to_front()
        await page.wait_for_timeout(700)
        await self._show_controls(page)
        if not await self._open(page):
            labels = await self._visible_control_labels(page)
            raise RuntimeError(
                "The shared album's View activity surface could not be opened. "
                f"Visible controls were: {labels}"
            )

        liked: set[str] = set()
        unresolved: set[str] = set()

        def absorb(state: dict[str, Any]) -> bool:
            before = len(liked)
            liked.update(str(v) for v in state.get("ids", []))
            unresolved.update(str(v) for v in state.get("unresolved", []))
            return len(liked) != before

        first = await self._state(page)
        if not first.get("scrollerFound"):
            raise RuntimeError("Activity opened, but its scroll container could not be identified safely.")
        absorb(first)
        logger.info("Activity scroller selected: {}", first.get("scroller"))

        await self._rewind(page, liked, unresolved, absorb)
        await self._scan_forward(page, liked, unresolved, absorb)

        if unresolved:
            raise RuntimeError(
                "Like activity could not be mapped safely to every photo. No downloads were attempted. "
                f"Examples: {sorted(unresolved)[:10]}"
            )
        invalid = [v for v in liked if not re.fullmatch(r"[A-Za-z0-9_-]{8,}", v)]
        if invalid:
            raise RuntimeError(f"Activity indexing returned invalid photo IDs: {invalid[:5]}")
        logger.info("Indexed {} unique photos liked by at least one album participant.", len(liked))
        return liked

    async def _rewind(self, page: Page, liked: set[str], unresolved: set[str], absorb: Any) -> None:
        logger.info("Rewinding album activity to a stable oldest position.")
        stable, previous, round_no = 0, None, 0
        deadline = asyncio.get_running_loop().time() + 180
        while stable < 4:
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError("Timed out while rewinding album activity. No downloads were attempted.")
            round_no += 1
            await self._action(page, "top")
            await page.wait_for_timeout(650)
            state = await self._state(page)
            if not state.get("open"):
                raise RuntimeError("Album activity closed during rewind.")
            absorb(state)
            sig = (bool(state.get("atTop")), int(state.get("scrollHeight", 0)),
                   len(liked), len(unresolved))
            stable = stable + 1 if bool(state.get("atTop")) and sig == previous else 0
            previous = sig
            if round_no == 1 or round_no % 10 == 0 or stable:
                logger.debug("Activity rewind {}: liked={}, scroll={}/{}, stable={}.",
                             round_no, len(liked), state.get("scrollTop"),
                             state.get("scrollHeight"), stable)

    async def _scan_forward(self, page: Page, liked: set[str], unresolved: set[str], absorb: Any) -> None:
        logger.info("Scanning participant-like activity from oldest to newest.")
        stable, stalled, previous, round_no = 0, 0, None, 0
        deadline = asyncio.get_running_loop().time() + 1200
        while stable < 4:
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError("Timed out before album activity reached a stable end.")
            round_no += 1
            state = await self._state(page)
            if not state.get("open"):
                raise RuntimeError("Album activity closed during scanning.")
            found_new = absorb(state)
            sig = (bool(state.get("atEnd")), int(state.get("scrollTop", 0)),
                   int(state.get("scrollHeight", 0)), len(liked), len(unresolved))
            stable = stable + 1 if bool(state.get("atEnd")) and sig == previous else 0
            previous = sig
            if round_no == 1 or round_no % 25 == 0 or found_new or stable:
                logger.debug("Activity scan {}: liked={}, scroll={}/{}, at_end={}, stable={}, signals={}.",
                             round_no, len(liked), state.get("scrollTop"),
                             state.get("scrollHeight"), state.get("atEnd"), stable,
                             state.get("signals", []))
            if stable >= 4:
                break
            before = int(state.get("scrollTop", 0))
            moved = await self._action(page, "down")
            await page.wait_for_timeout(275)
            after = int(moved.get("scrollTopAfterAction", before))
            stalled = stalled + 1 if after <= before + 1 and not state.get("atEnd") else 0
            if stalled:
                await page.wait_for_timeout(500)
            if stalled >= 12:
                raise RuntimeError(
                    "Activity list stopped moving before its end. Selected scroller: "
                    f"{state.get('scroller')}"
                )

    async def _state(self, page: Page) -> dict[str, Any]:
        return await self._action(page, "snapshot")

    async def _action(self, page: Page, action: str) -> dict[str, Any]:
        return dict(await page.evaluate(self.ACTIVITY_SCRIPT, action) or {})

    async def _open(self, page: Page) -> bool:
        baseline = page.url
        direct = await self._first_visible(tuple(page.locator(s) for s in self.VIEW_ACTIVITY_SELECTORS))
        if direct is not None:
            try:
                await direct.click(timeout=8_000)
                if await self._wait_open(page, baseline):
                    return True
            except Exception:
                pass
        more = await self._first_visible(tuple(page.locator(s) for s in self.MORE_OPTIONS_SELECTORS))
        if more is not None:
            try:
                await more.click(timeout=8_000)
                await page.locator("[role='menu']:visible").first.wait_for(state="visible", timeout=4_000)
                item = await self._first_visible((
                    page.locator("[role='menuitem'][aria-label='View activity']"),
                    page.locator("[role='menuitem'][aria-label^='View activity']"),
                    page.get_by_role("menuitem", name="View activity", exact=True),
                ))
                if item is not None:
                    await item.click(timeout=8_000)
                    if await self._wait_open(page, baseline):
                        return True
            except Exception:
                await self._dismiss_menu(page)
        return False

    async def _wait_open(self, page: Page, baseline: str) -> bool:
        for _ in range(50):
            state = await self._state(page)
            if state.get("open"):
                return True
            if page.url != baseline and re.search(r"/activit(?:y|ies)(?:/|$)", urlparse(page.url).path, re.I):
                return True
            await page.wait_for_timeout(200)
        return False

    async def _show_controls(self, page: Page) -> None:
        viewport = page.viewport_size or {"width": 1440, "height": 1000}
        await page.mouse.move(viewport["width"] // 2, 100)
        await page.wait_for_timeout(150)
        await page.mouse.move(viewport["width"] - 120, 50)
        await page.wait_for_timeout(600)

    async def _first_visible(self, candidates: tuple[Locator, ...]) -> Locator | None:
        for candidate in candidates:
            for index in range(min(await candidate.count(), 20)):
                item = candidate.nth(index)
                try:
                    if await item.is_visible() and await item.is_enabled():
                        return item
                except Exception:
                    continue
        return None

    async def _visible_control_labels(self, page: Page) -> list[str]:
        labels = await page.locator("[aria-label]:visible,[title]:visible").evaluate_all(
            """els => Array.from(new Set(els.map(e =>
            (e.getAttribute('aria-label') || e.getAttribute('title') || '')
            .replace(/\\s+/g,' ').trim()).filter(Boolean))).slice(0,80)"""
        )
        return [str(v) for v in labels]

    async def _dismiss_menu(self, page: Page) -> None:
        try:
            if await page.locator("[role='menu']:visible").count() > 0:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(100)
        except Exception:
            logger.debug("Unable to dismiss the album menu cleanly.")
