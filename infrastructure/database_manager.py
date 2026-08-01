"""Asynchronous SQLite persistence for downloader state and history."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from databases import Database
from loguru import logger

from infrastructure.exceptions import DatabaseError


class DatabaseManager:
    """Manage the local SQLite database used for resume and deduplication.

    The schema is created automatically during :meth:`connect`, so users do not
    need to install or run SQLite separately or execute migrations for v1.
    """

    def __init__(self, db_url: str = "sqlite+aiosqlite:///state/downloader.db") -> None:
        self.db_url = db_url
        self.database = Database(db_url)
        self._connected = False

    async def connect(self) -> None:
        """Connect to SQLite and create all required tables and indexes."""
        if self._connected:
            return

        try:
            await self.database.connect()
            self._connected = True
            await self.initialize_schema()
            logger.info("Connected to database and verified schema.")
        except Exception as exc:
            if self._connected:
                await self.database.disconnect()
                self._connected = False
            logger.exception("Failed to initialize database.")
            raise DatabaseError("Failed to connect to or initialize the database.") from exc

    async def disconnect(self) -> None:
        """Close the database connection safely."""
        if not self._connected:
            return

        try:
            await self.database.disconnect()
        except Exception as exc:
            logger.exception("Failed to disconnect from database.")
            raise DatabaseError("Failed to disconnect from the database.") from exc
        finally:
            self._connected = False

    async def initialize_schema(self) -> None:
        """Create the v1 schema idempotently."""
        statements = (
            """
            CREATE TABLE IF NOT EXISTS photos (
                photo_id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                liked INTEGER NOT NULL CHECK (liked IN (0, 1)),
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS downloads (
                photo_id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                downloaded_at TEXT NOT NULL,
                FOREIGN KEY (photo_id) REFERENCES photos(photo_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_photos_liked ON photos(liked)",
            "CREATE INDEX IF NOT EXISTS idx_downloads_downloaded_at ON downloads(downloaded_at)",
        )

        try:
            await self.database.execute("PRAGMA foreign_keys = ON")
            for statement in statements:
                await self.database.execute(statement)
        except Exception as exc:
            logger.exception("Failed to create database schema.")
            raise DatabaseError("Failed to create the database schema.") from exc

    async def add_photo(self, photo_id: str, url: str, liked: bool) -> None:
        """Insert or refresh a photo observation without losing history."""
        now = _utc_now()
        query = """
            INSERT INTO photos (photo_id, url, liked, first_seen_at, last_seen_at)
            VALUES (:photo_id, :url, :liked, :now, :now)
            ON CONFLICT(photo_id) DO UPDATE SET
                url = excluded.url,
                liked = excluded.liked,
                last_seen_at = excluded.last_seen_at
        """
        try:
            await self.database.execute(
                query,
                {
                    "photo_id": photo_id,
                    "url": url,
                    "liked": 1 if liked else 0,
                    "now": now,
                },
            )
        except Exception as exc:
            logger.exception("Failed to save photo {}.", photo_id)
            raise DatabaseError(f"Failed to save photo '{photo_id}'.") from exc

    async def add_download(self, photo_id: str, filename: str) -> None:
        """Record a completed download idempotently."""
        query = """
            INSERT INTO downloads (photo_id, filename, downloaded_at)
            VALUES (:photo_id, :filename, :downloaded_at)
            ON CONFLICT(photo_id) DO UPDATE SET
                filename = excluded.filename,
                downloaded_at = excluded.downloaded_at
        """
        try:
            await self.database.execute(
                query,
                {
                    "photo_id": photo_id,
                    "filename": filename,
                    "downloaded_at": _utc_now(),
                },
            )
        except Exception as exc:
            logger.exception("Failed to record download for {}.", photo_id)
            raise DatabaseError(f"Failed to record download for '{photo_id}'.") from exc

    async def is_photo_downloaded(self, photo_id: str) -> bool:
        """Return whether a completed download is already recorded."""
        try:
            value = await self.database.fetch_val(
                "SELECT 1 FROM downloads WHERE photo_id = :photo_id LIMIT 1",
                {"photo_id": photo_id},
            )
            return value is not None
        except Exception as exc:
            logger.exception("Failed to check download status for {}.", photo_id)
            raise DatabaseError(f"Failed to check download status for '{photo_id}'.") from exc

    async def get_setting(self, key: str) -> str | None:
        """Read a persisted application setting."""
        try:
            row = await self.database.fetch_one(
                "SELECT value FROM settings WHERE key = :key",
                {"key": key},
            )
            return str(row["value"]) if row is not None else None
        except Exception as exc:
            logger.exception("Failed to read setting {}.", key)
            raise DatabaseError(f"Failed to read setting '{key}'.") from exc

    async def set_setting(self, key: str, value: str) -> None:
        """Create or replace a persisted application setting."""
        query = """
            INSERT INTO settings (key, value, updated_at)
            VALUES (:key, :value, :updated_at)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
        """
        try:
            await self.database.execute(
                query,
                {"key": key, "value": value, "updated_at": _utc_now()},
            )
        except Exception as exc:
            logger.exception("Failed to write setting {}.", key)
            raise DatabaseError(f"Failed to write setting '{key}'.") from exc

    async def get_statistics(self) -> dict[str, int]:
        """Return processing and download counts."""
        try:
            total_photos = int(
                await self.database.fetch_val("SELECT COUNT(*) FROM photos") or 0
            )
            liked_photos = int(
                await self.database.fetch_val(
                    "SELECT COUNT(*) FROM photos WHERE liked = 1"
                )
                or 0
            )
            total_downloads = int(
                await self.database.fetch_val("SELECT COUNT(*) FROM downloads") or 0
            )
            return {
                "total_photos": total_photos,
                "liked_photos": liked_photos,
                "total_downloads": total_downloads,
            }
        except Exception as exc:
            logger.exception("Failed to read database statistics.")
            raise DatabaseError("Failed to retrieve database statistics.") from exc

    async def health_check(self) -> dict[str, Any]:
        """Verify connectivity and expose basic diagnostic information."""
        try:
            result = await self.database.fetch_val("SELECT sqlite_version()")
            return {
                "connected": self._connected,
                "sqlite_version": str(result),
                "database_url": self.db_url,
            }
        except Exception as exc:
            raise DatabaseError("Database health check failed.") from exc


def _utc_now() -> str:
    """Return an ISO-8601 UTC timestamp suitable for SQLite text storage."""
    return datetime.now(timezone.utc).isoformat()
