import pytest
from unittest.mock import MagicMock
from infrastructure.like_detector import LikeDetector


@pytest.fixture
def mock_page():
    return MagicMock()


@pytest.fixture
def like_detector(mock_page):
    return LikeDetector(page=mock_page)


def test_is_liked_true(mock_page, like_detector):
    mock_page.evaluate.return_value = True
    assert like_detector.is_liked() is True
    mock_page.evaluate.assert_called_once_with(
        """() => !!document.querySelector('[aria-label="Delete like"]')"""
    )


def test_is_liked_false(mock_page, like_detector):
    mock_page.evaluate.return_value = False
    assert like_detector.is_liked() is False
    mock_page.evaluate.assert_called_once_with(
        """() => !!document.querySelector('[aria-label="Delete like"]')"""
    )


def test_is_liked_exception(mock_page, like_detector):
    mock_page.evaluate.side_effect = Exception("JavaScript error")
    with pytest.raises(Exception, match="JavaScript error"):
        like_detector.is_liked()
