class DownloadError(Exception):
    """Raised when a download operation fails."""
    pass


class NavigationError(Exception):
    """Raised when navigation fails."""
    pass


class BrowserError(Exception):
    """Raised when browser-related operations fail."""
    pass
