from pathlib import Path

from funflix_api.cli import _default_server_config_path


def test_default_server_config_path() -> None:
    assert _default_server_config_path() == (
        Path.home() / "farfarfun" / "funflix" / "api" / "config.toml"
    )
