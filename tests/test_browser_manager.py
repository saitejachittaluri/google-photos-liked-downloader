import pytest
from unittest.mock import MagicMock, patch
from infrastructure.browser_manager import BrowserManager, BrowserManagerConfig


@pytest.fixture
def mock_playwright():
    with patch("infrastructure.browser_manager.sync_playwright") as mock_sync_playwright:
        mock_playwright_instance = MagicMock()
        mock_browser = MagicMock()
        mock_page = MagicMock()

        mock_playwright_instance.start.return_value = mock_playwright_instance
        mock_playwright_instance.chromium.launch_persistent_context.return_value = mock_browser
        mock_browser.new_page.return_value = mock_page

        mock_sync_playwright.return_value = mock_playwright_instance
        yield mock_playwright_instance, mock_browser, mock_page


@pytest.fixture
def browser_manager_config():
    return BrowserManagerConfig(
        chrome_profile_path="/path/to/chrome/profile",
        shared_album_url="https://photos.google.com/shared-album",
        max_retries=3,
        retry_delay=1,
    )


def test_launch_browser(mock_playwright, browser_manager_config):
    mock_playwright_instance, mock_browser, mock_page = mock_playwright
    manager = BrowserManager(browser_manager_config)

    manager.launch_browser()

    mock_playwright_instance.chromium.launch_persistent_context.assert_called_once_with(
        user_data_dir=browser_manager_config.chrome_profile_path,
        headless=False,
    )
    assert manager._browser == mock_browser
    assert manager._page == mock_page


def test_open_shared_album(mock_playwright, browser_manager_config):
    _, _, mock_page = mock_playwright
    manager = BrowserManager(browser_manager_config)

    manager._page = mock_page
    manager.open_shared_album()

    mock_page.goto.assert_called_once_with(browser_manager_config.shared_album_url, timeout=30000)


def test_detect_and_reconnect(mock_playwright, browser_manager_config):
    _, mock_browser, mock_page = mock_playwright
    manager = BrowserManager(browser_manager_config)

    manager._browser = mock_browser
    manager._page = mock_page

    mock_page.is_closed.return_value = True
    manager.detect_and_reconnect()

    assert mock_page.is_closed.call_count > 0
    mock_browser.close.assert_called_once()
