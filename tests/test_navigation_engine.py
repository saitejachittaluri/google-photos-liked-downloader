import pytest
from unittest.mock import MagicMock
from infrastructure.navigation_engine import NavigationEngine


@pytest.fixture
def mock_page():
    return MagicMock()


@pytest.fixture
def navigation_engine(mock_page):
    return NavigationEngine(page=mock_page, max_retries=3, retry_delay=1)


def test_open_first_photo(mock_page, navigation_engine):
    mock_page.wait_for_selector.return_value = None
    navigation_engine.open_first_photo()
    mock_page.click.assert_called_once_with("css=[data-testid='thumbnail']")


def test_navigate_next_success(mock_page, navigation_engine):
    mock_page.url = "https://photos.google.com/photo/12345"
    mock_page.keyboard.press.return_value = None
    photo_id = navigation_engine.navigate_next()
    assert photo_id == "12345"
    mock_page.keyboard.press.assert_called_once_with("ArrowRight")


def test_navigate_next_retries(mock_page, navigation_engine):
    mock_page.url = ""
    mock_page.keyboard.press.side_effect = [Exception("Navigation failed"), None]
    mock_page.url = "https://photos.google.com/photo/12345"
    photo_id = navigation_engine.navigate_next()
    assert photo_id == "12345"
    assert mock_page.keyboard.press.call_count == 2


def test_detect_end_of_album(mock_page, navigation_engine):
    mock_page.query_selector.return_value = True
    assert navigation_engine.detect_end_of_album() is True
    mock_page.query_selector.assert_called_once_with("css=[data-testid='end-of-album']")


def test_get_current_photo_id(mock_page, navigation_engine):
    mock_page.url = "https://photos.google.com/photo/12345"
    photo_id = navigation_engine.get_current_photo_id()
    assert photo_id == "12345"
