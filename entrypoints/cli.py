import asyncio
import click
from loguru import logger
from infrastructure.browser.browser_manager import BrowserManager
from infrastructure.navigation.navigation_engine import NavigationEngine
from infrastructure.download.download_manager import DownloadManager
from infrastructure.persistence.database_manager import DatabaseManager
from domain.photo_service import PhotoService
from infrastructure.config_validator import validate_config


@click.group()
def cli():
    """Google Photos Downloader CLI."""
    pass


@cli.command()
@click.option("--config", default="config.json", help="Path to the configuration file.")
def validate_config_command(config: str):
    """Validate the application's configuration file."""
    logger.info("Validating configuration file: {}", config)
    try:
        validate_config(config)
        click.echo("Configuration is valid.")
    except Exception as e:
        logger.error("Configuration validation failed: {}", e)
        click.echo(f"Configuration validation failed: {e}")


@cli.command()
@click.option("--config", default="config.json", help="Path to the configuration file.")
def start(config: str):
    """Start the photo download process."""
    logger.info("Starting the download process with config: {}", config)
    try:
        asyncio.run(_start_process(config))
    except Exception as e:
        logger.error("Failed to start the download process: {}", e)
        click.echo(f"Error: {e}")


@cli.command()
@click.option("--config", default="config.json", help="Path to the configuration file.")
def resume(config: str):
    """Resume the photo download process."""
    logger.info("Resuming the download process with config: {}", config)
    try:
        asyncio.run(_resume_process(config))
    except Exception as e:
        logger.error("Failed to resume the download process: {}", e)
        click.echo(f"Error: {e}")


@cli.command()
@click.option("--config", default="config.json", help="Path to the configuration file.")
def status(config: str):
    """Display the current status of the application."""
    logger.info("Fetching application status with config: {}", config)
    try:
        asyncio.run(_show_status(config))
    except Exception as e:
        logger.error("Failed to fetch application status: {}", e)
        click.echo(f"Error: {e}")


@cli.command()
@click.option("--config", default="config.json", help="Path to the configuration file.")
def stats(config: str):
    """Show statistics about photos and downloads."""
    logger.info("Fetching statistics with config: {}", config)
    try:
        asyncio.run(_show_stats(config))
    except Exception as e:
        logger.error("Failed to fetch statistics: {}", e)
        click.echo(f"Error: {e}")


@cli.command()
@click.option("--config", default="config.json", help="Path to the configuration file.")
def doctor(config: str):
    """Check the system for potential issues."""
    logger.info("Running system checks with config: {}", config)
    try:
        asyncio.run(_run_diagnostics(config))
    except Exception as e:
        logger.error("System checks failed: {}", e)
        click.echo(f"Error: {e}")


# ----------------------------
# Internal Command Handlers
# ----------------------------

async def _start_process(config_path: str):
    """Start the photo download process."""
    config = validate_config(config_path)
    browser_manager = BrowserManager(config.chrome_profile_path)
    database_manager = DatabaseManager(config.database_url)
    navigation_engine = NavigationEngine(None)  # Page will be injected later
    download_manager = DownloadManager(None, config.download_dir)  # Page will be injected later
    photo_service = PhotoService(browser_manager, navigation_engine, download_manager, database_manager)

    await database_manager.connect()
    try:
        await photo_service.process_photos(config.shared_album_url)
    finally:
        await database_manager.disconnect()


async def _resume_process(config_path: str):
    """Resume the photo download process."""
    config = validate_config(config_path)
    browser_manager = BrowserManager(config.chrome_profile_path)
    database_manager = DatabaseManager(config.database_url)
    navigation_engine = NavigationEngine(None)  # Page will be injected later
    download_manager = DownloadManager(None, config.download_dir)  # Page will be injected later
    photo_service = PhotoService(browser_manager, navigation_engine, download_manager, database_manager)

    await database_manager.connect()
    try:
        last_photo_id = await database_manager.get_setting("last_photo_id")
        if not last_photo_id:
            click.echo("No resume point found. Use 'start' to begin the process.")
            return
        await photo_service.resume_photos(config.shared_album_url, last_photo_id)
    finally:
        await database_manager.disconnect()


async def _show_status(config_path: str):
    """Show the current status of the application."""
    config = validate_config(config_path)
    database_manager = DatabaseManager(config.database_url)

    await database_manager.connect()
    try:
        last_photo_id = await database_manager.get_setting("last_photo_id")
        click.echo(f"Last processed photo ID: {last_photo_id or 'None'}")
    finally:
        await database_manager.disconnect()


async def _show_stats(config_path: str):
    """Show statistics about photos and downloads."""
    config = validate_config(config_path)
    database_manager = DatabaseManager(config.database_url)

    await database_manager.connect()
    try:
        stats = await database_manager.get_statistics()
        click.echo(f"Total photos: {stats['total_photos']}")
        click.echo(f"Liked photos: {stats['liked_photos']}")
        click.echo(f"Total downloads: {stats['total_downloads']}")
    finally:
        await database_manager.disconnect()


async def _run_diagnostics(config_path: str):
    """Run system diagnostics."""
    config = validate_config(config_path)
    database_manager = DatabaseManager(config.database_url)

    await database_manager.connect()
    try:
        # Check database connection
        await database_manager.get_statistics()
        click.echo("Database connection: OK")
    except Exception as e:
        click.echo(f"Database connection: FAILED ({e})")
    finally:
        await database_manager.disconnect()
