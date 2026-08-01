import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
from infrastructure.download_manager import DownloadManager


@pytest.fixture
def mock_page():
    return MagicMock()


@pytest.fixture
def download_manager(mock_page, tmp_path):
    return DownloadManager(page=mock_page, download_dir=str(tmp_path), max_retries=3, retry_delay=1)


def test_download_file_success(mock_page, download_manager, tmp_path):
    # Mock the download process
    mock_page.wait_for_selector.return_value = None
    mock_page.click.return_value = None

    # Simulate a .crdownload file that disappears
    crdownload_file = tmp_path / "testfile.crdownload"
    final_file = tmp_path / "testfile"
    crdownload_file.touch()  # Create the .crdownload file
    with patch("time.sleep", side_effect=lambda _: crdownload_file.unlink()):  # Simulate download completion
        final_file.touch()  # Create the final file

        downloaded_file = download_manager.download_file()
        assert downloaded_file == str(final_file)
        assert final_file.exists()


def test_download_file_retries(mock_page, download_manager, tmp_path):
    # Mock the download process
    mock_page.wait_for_selector.side_effect = [Exception("Click failed"), None]
    mock_page.click.return_value = None

    # Simulate a .crdownload file that disappears
    crdownload_file = tmp_path / "testfile.crdownload"
    final_file = tmp_path / "testfile"
    crdownload_file.touch()  # Create the .crdownload file
    with patch("time.sleep", side_effect=lambda _: crdownload_file.unlink()):  # Simulate download completion
        final_file.touch()  # Create the final file

        downloaded_file = download_manager.download_file()
        assert downloaded_file == str(final_file)
        assert final_file.exists()
        assert mock_page.wait_for_selector.call_count == 2  # Retries once


def test_download_file_failure(mock_page, download_manager):
    # Mock the download process to always fail
    mock_page.wait_for_selector.side_effect = Exception("Click failed")

    with pytest.raises(RuntimeError, match="DownloadManager failed to download the file."):
        download_manager.download_file()
