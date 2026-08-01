"""Deprecated per-photo like detector.

Google Photos does not reliably expose activity controls on an individual photo URL.
The application now indexes liked photo IDs from the shared album's View activity feed
before traversal. This module intentionally contains no Like/Unlike/Delete-like selectors.
"""

from __future__ import annotations

from typing import Any


class LikeDetector:
    """Compatibility shell that prevents unsafe legacy detection from being reused."""

    def __init__(self, page: Any) -> None:
        self.page = page

    def is_liked(self) -> bool:
        raise RuntimeError(
            "Per-photo like detection is disabled. Use PhotoService's album activity index."
        )
