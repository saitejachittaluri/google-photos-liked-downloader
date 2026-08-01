"""Command-line interface for the Google Photos liked-photo downloader."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import click
from loguru import logger

from domain.photo_service import PhotoService
from infrastructure.browser_manager import BrowserManager
from infrastructure.config_validator import AppConfig, ConfigurationError, validate_config
from infrastructure.database_manager import DatabaseManager
from infrastructure.download_manager import DownloadManager
from infrastructure.navigation_engine import NavigationEngine

T = TypeVar("T")


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="google-photos-liked-downloader")
def cli() -> None:
    """Download photos liked by any participant in a shared Google Photos album."""


@cli.command("validate-config")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=Path("config.json"),
    show_default=True,
    help="Path to the JSON configuration file.",
)
def validate_config_command(config_path: Path) -> None:
    """Validate configuration and create required local directories."""
    config = _load_config(config_path)
    click.echo("Configuration is valid.")
    click.echo(f"Album: {config.shared_album_url}")
    click.echo(f"Download directory: {config.download_dir.resolve()}")
    click.echo(f"Browser profile: {config.chrome_profile_path.resolve()}")


@cli.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=Path("config.json"),
    show_default=True,
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Inspect photos and record progress without downloading files.",
)
@click.option(
    "--max-photos",
    type=click.IntRange(min=1),
    default=None,
    help="Stop after processing this many photos; useful for safe testing.",
)
def start(config_path: Path, dry_run: bool, max_photos: int | None) -> None:
    """Process the album from its first photo."""
    _execute(
        lambda: _run_download(
            config_path=config_path,
            resume=False,
            dry_run=dry_run,
            max_photos=max_photos,
        )
    )


@cli.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=Path("config.json"),
    show_default=True,
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Resume inspection without downloading files.",
)
@click.option(
    "--max-photos",
    type=click.IntRange(min=1),
    default=None,
    help="Stop after processing this many additional photos.",
)
def resume(config_path: Path, dry_run: bool, max_photos: int | None) -> None:
    """Continue after the last successfully processed photo."""
    _execute(
        lambda: _run_download(
            config_path=config_path,
            resume=True,
            dry_run=dry_run,
            max_photos=max_photos,
        )
    )


@cli.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=Path("config.json"),
    show_default=True,
)
def status(config_path: Path) -> None:
    """Display the saved resume position."""
    _execute(lambda: _show_status(config_path))


@cli.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=Path("config.json"),
    show_default=True,
)
def stats(config_path: Path) -> None:
    """Display persisted photo and download counts."""
    _execute(lambda: _show_stats(config_path))


@cli.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=Path("config.json"),
    show_default=True,
)
def doctor(config_path: Path) -> None:
    """Check configuration, directories, SQLite connectivity, and Playwright import."""
    _execute(lambda: _run_diagnostics(config_path))


async def _run_download(
    *,
    config_path: Path,
    resume: bool,
    dry_run: bool,
    max_photos: int | None,
) -> None:
    config = _load_config(config_path)
    database_manager = DatabaseManager(config.database_url)
    browser_manager = BrowserManager(config.chrome_profile_path)

    # PhotoService injects the active Playwright page after BrowserManager launches.
    navigation_engine = NavigationEngine(
        None,  # type: ignore[arg-type]
        retry_delay=max(1, config.navigation_delay_ms // 1000),
    )
    download_manager = DownloadManager(
        None,  # type: ignore[arg-type]
        str(config.download_dir),
    )
    service = PhotoService(
        browser_manager=browser_manager,
        navigation_engine=navigation_engine,
        download_manager=download_manager,
        database_manager=database_manager,
        dry_run=dry_run,
        max_photos=max_photos,
    )

    await database_manager.connect()
    try:
        if resume:
            last_photo_id = await database_manager.get_setting("last_photo_id")
            if not last_photo_id:
                raise click.ClickException(
                    "No resume position exists. Run the 'start' command first."
                )
            await service.resume_photos(config.shared_album_url, last_photo_id)
        else:
            await service.process_photos(config.shared_album_url)
    finally:
        await database_manager.disconnect()


async def _show_status(config_path: Path) -> None:
    config = _load_config(config_path)
    database_manager = DatabaseManager(config.database_url)
    await database_manager.connect()
    try:
        last_photo_id = await database_manager.get_setting("last_photo_id")
        click.echo(f"Last processed photo ID: {last_photo_id or 'None'}")
    finally:
        await database_manager.disconnect()


async def _show_stats(config_path: Path) -> None:
    config = _load_config(config_path)
    database_manager = DatabaseManager(config.database_url)
    await database_manager.connect()
    try:
        values = await database_manager.get_statistics()
        click.echo(f"Total photos inspected: {values['total_photos']}")
        click.echo(f"Liked photos: {values['liked_photos']}")
        click.echo(f"Downloaded photos: {values['total_downloads']}")
    finally:
        await database_manager.disconnect()


async def _run_diagnostics(config_path: Path) -> None:
    config = _load_config(config_path)
    click.echo("Configuration: OK")
    click.echo(f"Download directory: OK ({config.download_dir.resolve()})")
    click.echo(f"Browser profile directory: OK ({config.chrome_profile_path.resolve()})")

    try:
        import playwright  # noqa: F401
    except ImportError as exc:
        raise click.ClickException(
            "Playwright is not installed. Run: python -m pip install -e ."
        ) from exc
    click.echo("Playwright package: OK")

    database_manager = DatabaseManager(config.database_url)
    await database_manager.connect()
    try:
        # The database manager will be enhanced separately to initialize tables.
        await database_manager.get_statistics()
    except Exception as exc:
        raise click.ClickException(
            "SQLite connection opened, but the application schema is unavailable. "
            "Database initialization still needs to be completed."
        ) from exc
    finally:
        await database_manager.disconnect()
    click.echo("SQLite database and schema: OK")


def _load_config(config_path: Path) -> AppConfig:
    try:
        return validate_config(config_path)
    except ConfigurationError as exc:
        raise click.ClickException(str(exc)) from exc


def _execute(factory: Callable[[], Awaitable[T]]) -> T:
    """Run one async CLI operation with consistent user-facing error handling."""
    try:
        return asyncio.run(factory())
    except click.ClickException:
        raise
    except KeyboardInterrupt as exc:
        raise click.Abort() from exc
    except Exception as exc:
        logger.exception("Command failed")
        raise click.ClickException(str(exc) or exc.__class__.__name__) from exc


if __name__ == "__main__":
    cli()
