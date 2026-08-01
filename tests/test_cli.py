import pytest
from click.testing import CliRunner
from entrypoints.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


def test_start(runner):
    result = runner.invoke(cli, ["start"])
    assert result.exit_code == 0
    assert "Download process started." in result.output


def test_resume_no_resume_point(runner, mocker):
    mock_db_manager = mocker.patch("entrypoints.cli.DatabaseManager")
    mock_db_manager.return_value.get_setting.return_value = None

    result = runner.invoke(cli, ["resume"])
    assert result.exit_code == 0
    assert "No resume point found" in result.output


def test_resume_with_resume_point(runner, mocker):
    mock_db_manager = mocker.patch("entrypoints.cli.DatabaseManager")
    mock_db_manager.return_value.get_setting.return_value = "photo123"

    result = runner.invoke(cli, ["resume"])
    assert result.exit_code == 0
    assert "Resuming from photo ID: photo123" in result.output


def test_status(runner, mocker):
    mock_db_manager = mocker.patch("entrypoints.cli.DatabaseManager")
    mock_db_manager.return_value.get_setting.return_value = "photo123"
    mock_db_manager.return_value.get_statistics.return_value = {"total_downloads": 10}

    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "Last processed photo ID: photo123" in result.output
    assert "Total downloads: 10" in result.output


def test_stats(runner, mocker):
    mock_db_manager = mocker.patch("entrypoints.cli.DatabaseManager")
    mock_db_manager.return_value.get_statistics.return_value = {
        "total_photos": 100,
        "liked_photos": 50,
        "total_downloads": 75,
    }

    result = runner.invoke(cli, ["stats"])
    assert result.exit_code == 0
    assert "Total photos: 100" in result.output
    assert "Liked photos: 50" in result.output
    assert "Total downloads: 75" in result.output


def test_doctor_success(runner, mocker):
    mock_db_manager = mocker.patch("entrypoints.cli.DatabaseManager")
    mock_db_manager.return_value.get_statistics.return_value = {}

    result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 0
    assert "Database connection: OK" in result.output


def test_doctor_failure(runner, mocker):
    mock_db_manager = mocker.patch("entrypoints.cli.DatabaseManager")
    mock_db_manager.return_value.get_statistics.side_effect = Exception("Database error")

    result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 0
    assert "Database connection: FAILED (Database error)" in result.output


def test_validate_config(runner):
    result = runner.invoke(cli, ["validate-config"])
    assert result.exit_code == 0
    assert "Configuration is valid." in result.output
