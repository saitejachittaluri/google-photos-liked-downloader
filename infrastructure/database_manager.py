from databases import Database
from sqlalchemy import create_engine, MetaData, Table, Column, String, Integer, Boolean, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.exc import IntegrityError
from loguru import logger
from datetime import datetime
from infrastructure.exceptions import DatabaseError

Base = declarative_base()
metadata = MetaData()


class DatabaseManager:
    def __init__(self, db_url: str = "sqlite+aiosqlite:///app.db"):
        """
        Manages database operations for the application.

        Args:
            db_url (str): The database connection URL.
        """
        self.db_url = db_url
        self.database = Database(db_url)

    async def connect(self):
        """Connect to the database."""
        try:
            await self.database.connect()
            logger.info("Connected to the database.")
        except Exception as e:
            logger.error("Failed to connect to the database: {}", e)
            raise DatabaseError("Failed to connect to the database.") from e

    async def disconnect(self):
        """Disconnect from the database."""
        try:
            await self.database.disconnect()
            logger.info("Disconnected from the database.")
        except Exception as e:
            logger.error("Failed to disconnect from the database: {}", e)
            raise DatabaseError("Failed to disconnect from the database.") from e

    async def add_photo(self, photo_id: str, url: str, liked: bool):
        """
        Add a photo to the database.

        Args:
            photo_id (str): The unique ID of the photo.
            url (str): The URL of the photo.
            liked (bool): Whether the photo is liked.

        Raises:
            DatabaseError: If the photo cannot be added.
        """
        query = """
        INSERT INTO photos (photo_id, url, liked)
        VALUES (:photo_id, :url, :liked)
        """
        try:
            await self.database.execute(query, {"photo_id": photo_id, "url": url, "liked": liked})
            logger.info("Photo added to the database: {}", photo_id)
        except IntegrityError:
            logger.warning("Photo already exists in the database: {}", photo_id)
        except Exception as e:
            logger.error("Failed to add photo to the database: {}", e)
            raise DatabaseError("Failed to add photo to the database.") from e

    async def add_download(self, photo_id: str, filename: str):
        """
        Track a downloaded photo in the database.

        Args:
            photo_id (str): The unique ID of the photo.
            filename (str): The filename of the downloaded photo.

        Raises:
            DatabaseError: If the download cannot be tracked.
        """
        query = """
        INSERT INTO downloads (photo_id, filename, timestamp)
        VALUES (:photo_id, :filename, :timestamp)
        """
        try:
            await self.database.execute(
                query,
                {"photo_id": photo_id, "filename": filename, "timestamp": datetime.utcnow()},
            )
            logger.info("Download tracked in the database: {}", filename)
        except Exception as e:
            logger.error("Failed to track download in the database: {}", e)
            raise DatabaseError("Failed to track download in the database.") from e

    async def get_setting(self, key: str) -> str:
        """
        Retrieve a setting value from the database.

        Args:
            key (str): The key of the setting.

        Returns:
            str: The value of the setting.

        Raises:
            DatabaseError: If the setting cannot be retrieved.
        """
        query = "SELECT value FROM settings WHERE key = :key"
        try:
            row = await self.database.fetch_one(query, {"key": key})
            return row["value"] if row else None
        except Exception as e:
            logger.error("Failed to retrieve setting from the database: {}", e)
            raise DatabaseError("Failed to retrieve setting from the database.") from e

    async def set_setting(self, key: str, value: str):
        """
        Set or update a setting value in the database.

        Args:
            key (str): The key of the setting.
            value (str): The value of the setting.

        Raises:
            DatabaseError: If the setting cannot be updated.
        """
        query = """
        INSERT INTO settings (key, value)
        VALUES (:key, :value)
        ON CONFLICT(key) DO UPDATE SET value = :value
        """
        try:
            await self.database.execute(query, {"key": key, "value": value})
            logger.info("Setting updated in the database: {} = {}", key, value)
        except Exception as e:
            logger.error("Failed to update setting in the database: {}", e)
            raise DatabaseError("Failed to update setting in the database.") from e

    async def get_statistics(self) -> dict:
        """
        Generate statistics about photos and downloads.

        Returns:
            dict: A dictionary containing statistics.

        Raises:
            DatabaseError: If statistics cannot be retrieved.
        """
        try:
            total_photos = await self.database.fetch_val("SELECT COUNT(*) FROM photos")
            liked_photos = await self.database.fetch_val("SELECT COUNT(*) FROM photos WHERE liked = 1")
            total_downloads = await self.database.fetch_val("SELECT COUNT(*) FROM downloads")
            return {
                "total_photos": total_photos,
                "liked_photos": liked_photos,
                "total_downloads": total_downloads,
            }
        except Exception as e:
            logger.error("Failed to retrieve statistics from the database: {}", e)
            raise DatabaseError("Failed to retrieve statistics from the database.") from e
