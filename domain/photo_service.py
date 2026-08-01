# domain/photo_service.py
from infrastructure.browser_manager import BrowserManager
from infrastructure.navigation_engine import NavigationEngine
from infrastructure.download_manager import DownloadManager
from infrastructure.database_manager import DatabaseManager

class PhotoService:
    def __init__(self, browser_manager: BrowserManager, navigation_engine: NavigationEngine,
                 download_manager: DownloadManager, database_manager: DatabaseManager):
        self.browser_manager = browser_manager
        self.navigation_engine = navigation_engine
        self.download_manager = download_manager
        self.database_manager = database_manager

    async def process_photos(self, shared_album_url: str):
        """Process photos in the shared album."""
        await self.browser_manager.launch_browser()
        await self.browser_manager.open_url(shared_album_url)
        await self.navigation_engine.open_first_photo()

        while not await self.navigation_engine.detect_end_of_album():
            photo_id = await self.navigation_engine.get_current_photo_id()
            if not self.database_manager.is_photo_downloaded(photo_id):
                filename = await self.download_manager.download_file()
                self.database_manager.add_download(photo_id, filename)
            await self.navigation_engine.navigate_next()

        await self.browser_manager.shutdown()
