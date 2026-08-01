"""Application configuration loading and validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class ConfigurationError(ValueError):
    """Raised when the application configuration is missing or invalid."""


class AppConfig(BaseModel):
    """Validated runtime configuration for the downloader."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    chrome_profile_path: Path = Field(alias="browser_profile_directory")
    shared_album_url: str = Field(alias="album_url")
    download_dir: Path = Field(alias="download_directory")
    database_url: str = "sqlite+aiosqlite:///state/downloader.db"
    headless: bool = False
    photo_load_timeout_seconds: int = Field(default=20, ge=1, le=300)
    download_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    navigation_delay_ms: int = Field(default=750, ge=0, le=30000)
    maximum_consecutive_failures: int = Field(default=10, ge=1, le=1000)

    @field_validator("shared_album_url")
    @classmethod
    def validate_album_url(cls, value: str) -> str:
        """Require an HTTPS Google Photos album URL."""
        parsed = urlparse(value)
        allowed_hosts = {
            "photos.app.goo.gl",
            "photos.google.com",
            "www.photos.google.com",
        }
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
            raise ValueError(
                "shared_album_url must be an HTTPS Google Photos URL "
                "from photos.app.goo.gl or photos.google.com"
            )
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        """Restrict v1 to the supported local SQLite database backend."""
        if not value.startswith(("sqlite:///", "sqlite+aiosqlite:///")):
            raise ValueError(
                "database_url must use sqlite:/// or sqlite+aiosqlite:///"
            )
        return value

    def prepare_directories(self) -> None:
        """Create local runtime directories if they do not already exist."""
        self.chrome_profile_path.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)

        database_path = _sqlite_database_path(self.database_url)
        if database_path is not None and database_path.parent != Path("."):
            database_path.parent.mkdir(parents=True, exist_ok=True)


def validate_config(config_path: str | Path) -> AppConfig:
    """Load, validate and prepare a JSON configuration file.

    Both the original internal field names and the user-facing aliases are
    accepted. For example, ``shared_album_url`` and ``album_url`` are both
    valid. Unknown fields are rejected to catch spelling mistakes early.
    """
    path = Path(config_path).expanduser()
    if not path.is_file():
        raise ConfigurationError(f"Configuration file not found: {path}")

    try:
        with path.open("r", encoding="utf-8") as config_file:
            raw_config: Any = json.load(config_file)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"Configuration file contains invalid JSON at line "
            f"{exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    except OSError as exc:
        raise ConfigurationError(f"Unable to read configuration file: {path}") from exc

    if not isinstance(raw_config, dict):
        raise ConfigurationError("Configuration root must be a JSON object")

    try:
        config = AppConfig.model_validate(raw_config)
    except ValidationError as exc:
        messages = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"])
            messages.append(f"{location}: {error['msg']}")
        raise ConfigurationError(
            "Invalid configuration:\n- " + "\n- ".join(messages)
        ) from exc

    config.prepare_directories()
    return config


def _sqlite_database_path(database_url: str) -> Path | None:
    """Extract the local file path from a supported SQLite URL."""
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if database_url.startswith(prefix):
            value = database_url[len(prefix) :]
            if value == ":memory:":
                return None
            return Path(value).expanduser()
    return None
