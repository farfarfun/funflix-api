"""API 服务的文件配置。"""

from __future__ import annotations

import json
import secrets
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


def default_server_config_path() -> Path:
    return Path.home() / ".farfarfun" / "funflix" / "api" / "config.toml"


def read_server_config(config: Path | None = None) -> dict[str, Any]:
    path = config or default_server_config_path()
    if not path.exists():
        if config is not None:
            raise FileNotFoundError(f"配置文件不存在：{path}")
        return {}

    suffix = path.suffix.lower()
    if suffix == ".toml":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    elif suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
    elif suffix == ".env":
        from dotenv import dotenv_values

        data = dict(dotenv_values(path))
    else:
        raise ValueError(f"不支持的配置文件格式：{path.suffix}（仅支持 .toml / .json / .env）")

    result = {str(key).lower(): value for key, value in data.items() if value is not None}
    if (
        any(
            key in result
            for key in (
                "oidc_client_secret",
                "funflix_oidc_client_secret",
                "session_secret",
                "funflix_session_secret",
            )
        )
        and path.stat().st_mode & 0o077
    ):
        raise PermissionError(f"包含密钥的配置文件权限必须是 0600：{path}")
    return result


def config_value(config: dict[str, Any], key: str) -> Any:
    return config.get(key, config.get(f"funflix_{key}"))


class SessionConfig(BaseModel):
    session_secret: str = Field(default_factory=lambda: secrets.token_hex(32))
    session_max_age: int = Field(default=60 * 60 * 24 * 30, gt=0)
    session_cookie_secure: bool = False


def load_session_config(config: dict[str, Any]) -> SessionConfig:
    values = {}
    for key in ("session_secret", "session_max_age", "session_cookie_secure"):
        value = config_value(config, key)
        if value is not None:
            values[key] = value
    return SessionConfig.model_validate(values)
