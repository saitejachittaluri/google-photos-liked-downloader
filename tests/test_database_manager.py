import pytest
from infrastructure.database_manager import DatabaseManager


@pytest.fixture
def db_manager():
    return DatabaseManager("sqlite:///:memory:")


def test_add_photo(db_manager):
    db_manager.add_photo("photo1", "http://example.com/photo1", liked=True)
    stats = db_manager.get_statistics()
    assert stats["total_photos"] == 1
    assert stats["liked_photos"] == 1


def test_add_download(db_manager):
    db_manager.add_download("photo1", "photo1.jpg")
    stats = db_manager.get_statistics()
    assert stats["total_downloads"] == 1


def test_settings(db_manager):
    db_manager.set_setting("last_photo_id", "photo1")
    assert db_manager.get_setting("last_photo_id") == "photo1"
