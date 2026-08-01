from pydantic import BaseModel, ValidationError


class AppConfig(BaseModel):
    chrome_profile_path: str
    shared_album_url: str
    download_dir: str

@cli.command()
def validate_config():
    """Validate the application's configuration file."""
    logger.info("Validating configuration...")
    config = {
        "chrome_profile_path": "/path/to/profile",
        "shared_album_url": "https://photos.google.com/shared-album",
        "download_dir": "/path/to/downloads",
    }
    try:
        validate_config(config)
        click.echo("Configuration is valid.")
    except ValueError as e:
        click.echo(f"Configuration validation failed: {e}")
