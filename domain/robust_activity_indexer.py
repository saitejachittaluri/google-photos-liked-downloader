"""Resilient read-only indexing of participant likes in Google Photos."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from loguru import logger
from playwright.async_api import Page

from domain.activity_indexer import AlbumActivityIndexer


class RobustAlbumActivityIndexer(AlbumActivityIndexer):
    """Wait for lazy activity rendering and support overflow-hidden virtual lists.

    Google Photos renders the Activity shell before it renders the virtual list. The base
    indexer intentionally uses strict overflow semantics. This adapter keeps its structural
    activity-root checks, but also accepts a structurally related element whose scrollHeight
    exceeds its clientHeight even when Google marks it ``overflow: hidden``.

    ``Liked by <person>`` is weak accessibility metadata that may be repeated on an avatar or
    badge without containing the photo link. It is still used when it maps to a photo, but an
    unmapped copy is not treated as a missing activity event. Strong activity text such as
    ``<person> liked a photo`` must still map safely or the run fails closed.
    """

    _STRICT_SCROLLABLE = (
        "return /(auto|scroll)/.test(s.overflowY) "
        "&& e.scrollHeight > e.clientHeight + 24;"
    )
    _VIRTUAL_SCROLLABLE = (
        "if ((e === document.body || e === document.documentElement) "
        "&& !routeActivity) return false; "
        "return e.scrollHeight > e.clientHeight + 24;"
    )
    _SIGNAL_COLLECTION_DECLARATION = (
        "const ids = new Set(), unresolved = [], labels = [];"
    )
    _SAFE_SIGNAL_COLLECTION_DECLARATION = (
        "const ids = new Set(), unresolved = [], weakUnmapped = [], labels = [];"
    )
    _UNMAPPED_SIGNAL_BLOCK = (
        "if (mapped.length) mapped.forEach(id => ids.add(id));\n"
        "    else unresolved.push(label || '<unlabelled like activity>');"
    )
    _SAFE_UNMAPPED_SIGNAL_BLOCK = (
        "if (mapped.length) mapped.forEach(id => ids.add(id));\n"
        "    else if (/^Liked by\\b/i.test(label)) weakUnmapped.push(label);\n"
        "    else unresolved.push(label || '<unlabelled like activity>');"
    )
    _UNRESOLVED_RETURN = (
        "unresolved:Array.from(new Set(unresolved)).slice(0, 50),\n"
        "    signals:Array.from(new Set(labels)).slice(0, 30),"
    )
    _SAFE_UNRESOLVED_RETURN = (
        "unresolved:Array.from(new Set(unresolved)).slice(0, 50),\n"
        "    weakUnmapped:Array.from(new Set(weakUnmapped)).slice(0, 50),\n"
        "    signals:Array.from(new Set(labels)).slice(0, 30),"
    )

    for expected in (
        _STRICT_SCROLLABLE,
        _SIGNAL_COLLECTION_DECLARATION,
        _UNMAPPED_SIGNAL_BLOCK,
        _UNRESOLVED_RETURN,
    ):
        if expected not in AlbumActivityIndexer.ACTIVITY_SCRIPT:
            raise RuntimeError(
                "AlbumActivityIndexer JavaScript changed unexpectedly; "
                f"missing fragment: {expected!r}"
            )

    ACTIVITY_SCRIPT = (
        AlbumActivityIndexer.ACTIVITY_SCRIPT
        .replace(_STRICT_SCROLLABLE, _VIRTUAL_SCROLLABLE)
        .replace(
            _SIGNAL_COLLECTION_DECLARATION,
            _SAFE_SIGNAL_COLLECTION_DECLARATION,
        )
        .replace(_UNMAPPED_SIGNAL_BLOCK, _SAFE_UNMAPPED_SIGNAL_BLOCK)
        .replace(_UNRESOLVED_RETURN, _SAFE_UNRESOLVED_RETURN)
    )

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
        weak_unmapped: set[str] = set()

        def absorb(state: dict[str, Any]) -> bool:
            before = len(liked)
            liked.update(str(value) for value in state.get("ids", []))
            unresolved.update(
                str(value) for value in state.get("unresolved", [])
            )
            weak_unmapped.update(
                str(value) for value in state.get("weakUnmapped", [])
            )
            return len(liked) != before

        first = await self._wait_for_activity_content(page, absorb)
        logger.info("Activity scroller selected: {}", first.get("scroller"))
        await self._rewind(page, liked, unresolved, absorb)
        await self._scan_forward(page, liked, unresolved, absorb)

        if unresolved:
            raise RuntimeError(
                "Strong like activity could not be mapped safely to every photo. "
                "No downloads were attempted. Examples: "
                f"{sorted(unresolved)[:10]}"
            )

        invalid = [
            value
            for value in liked
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,}", value)
        ]
        if invalid:
            raise RuntimeError(
                f"Activity indexing returned invalid photo IDs: {invalid[:5]}"
            )

        if weak_unmapped:
            logger.info(
                "Ignored {} unmapped duplicate 'Liked by' accessibility labels; "
                "all strong like events were mapped.",
                len(weak_unmapped),
            )

        logger.info(
            "Indexed {} unique photos liked by at least one album participant.",
            len(liked),
        )
        return liked

    async def _wait_for_activity_content(
        self,
        page: Page,
        absorb: Any,
    ) -> dict[str, Any]:
        """Wait for the Activity shell's lazy virtual list to materialize."""
        deadline = asyncio.get_running_loop().time() + 20
        last_state: dict[str, Any] = {}
        sample = 0

        while asyncio.get_running_loop().time() < deadline:
            sample += 1
            state = await self._state(page)
            last_state = state
            if not state.get("open"):
                raise RuntimeError(
                    "The album activity surface closed while waiting for its content."
                )

            absorb(state)
            if state.get("scrollerFound"):
                if self._safe_scroller(page, state):
                    return state
                state["scrollerFound"] = False

            if sample == 1 or sample % 10 == 0:
                logger.debug(
                    "Waiting for activity content: sample={}, scroller={}, "
                    "signals={}, ids={}.",
                    sample,
                    state.get("scroller"),
                    state.get("signals", []),
                    len(state.get("ids", [])),
                )
            await page.wait_for_timeout(300)

        labels = await self._visible_control_labels(page)
        raise RuntimeError(
            "Activity opened, but its lazy content did not produce a safe scroll "
            "container within 20 seconds. "
            f"Last state={last_state}; visible controls={labels}"
        )

    @staticmethod
    def _safe_scroller(page: Page, state: dict[str, Any]) -> bool:
        descriptor = state.get("scroller") or {}
        tag = str(descriptor.get("tag") or "").lower()
        source = str(descriptor.get("source") or "")
        width = int(descriptor.get("width") or 0)
        viewport_width = int(
            (page.viewport_size or {"width": 1440})["width"]
        )

        if source == "activity-route-document":
            return True

        if tag in {"body", "html"} and width >= int(viewport_width * 0.80):
            return False

        return source in {"activity-descendant", "activity-ancestor"}
