from pathlib import Path
from unittest.mock import Mock

from typer.testing import CliRunner

from funflix_api import cli

runner = CliRunner()


def test_default_server_config_path() -> None:
    assert cli._default_server_config_path() == (
        Path.home() / ".farfarfun" / "funflix" / "api" / "config.toml"
    )


def test_cli_command_tree() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in ("server", "upgrade", "rollback", "uninstall"):
        assert command in result.stdout

    result = runner.invoke(cli.app, ["server", "--help"])
    assert result.exit_code == 0
    for command in ("run", "start", "stop", "restart", "status"):
        assert command in result.stdout


def test_package_management_commands(monkeypatch) -> None:
    run_uv_tool = Mock()
    monkeypatch.setattr(cli, "_run_uv_tool", run_uv_tool)
    monkeypatch.setattr(cli, "server_stop", Mock())

    assert runner.invoke(cli.app, ["upgrade"]).exit_code == 0
    assert runner.invoke(cli.app, ["upgrade", "1.0.28"]).exit_code == 0
    assert runner.invoke(cli.app, ["rollback", "1.0.27"]).exit_code == 0
    assert runner.invoke(cli.app, ["uninstall"]).exit_code == 0

    assert run_uv_tool.call_args_list == [
        ((["install", "--upgrade", "funflix-api"],), {}),
        ((["install", "--upgrade", "funflix-api==1.0.28"],), {}),
        ((["install", "--force", "funflix-api==1.0.27"],), {}),
        ((["uninstall", "funflix-api"],), {}),
    ]
    cli.server_stop.assert_called_once_with()
