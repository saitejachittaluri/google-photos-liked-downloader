"""Application-specific exception hierarchy."""


class GooglePhotosDownloaderError(Exception):
    """Base exception for all downloader-specific failures."""


class ConfigurationError(GooglePhotosDownloaderError):
    """Raised when application configuration is missing or invalid."""


class BrowserError(GooglePhotosDownloaderError):
    """Raised when browser startup, navigation, or lifecycle operations fail."""


class NavigationError(GooglePhotosDownloaderError):
    """Raised when album or photo navigation fails."""


class DownloadError(GooglePhotosDownloaderError):
    """Raised when a photo download cannot be completed safely."""


class DatabaseError(GooglePhotosDownloaderError):
    """Raised when SQLite initialization or persistence operations fail."""
