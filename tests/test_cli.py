from pathlib import Path
from unittest.mock import Mock

import pytest
import typer
from typer.testing import CliRunner

from funflix_api import cli

runner = CliRunner()


def test_default_server_config_path() -> None:
    assert cli._default_server_config_path() == (
        Path.home() / ".farfarfun" / "funflix" / "api" / "config.toml"
    )


def test_sso_config_requires_private_file_and_reaches_runtime(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config.toml"
    config.write_text('oidc_issuer = "https://id.example.com"\noidc_client_secret = "secret"\n')
    with pytest.raises(typer.Exit):
        cli._resolve_server_host_port(None, None, config)

    config.chmod(0o600)
    monkeypatch.setattr(cli.os, "environ", {})
    assert cli._resolve_server_host_port(None, None, config) == ("127.0.0.1", 18810)
    assert cli.os.environ["FUNFLIX_OIDC_ISSUER"] == "https://id.example.com"
    assert cli.os.environ["FUNFLIX_OIDC_CLIENT_SECRET"] == "secret"


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
