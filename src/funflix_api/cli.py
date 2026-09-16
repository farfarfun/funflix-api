"""funflix-api 命令行入口：API 服务的生命周期管理。

领域逻辑（采集/解析/校验/worker/数据库迁移）由 `funflix` 提供，用 `funflix`
自己的 CLI 管理；这里只管 HTTP 服务进程本身的 run/start/stop/restart/status。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from funflix.base.config import get_settings

from funflix_api import __version__

app = typer.Typer(help="funflix-api 服务生命周期", no_args_is_help=True)

#: 命令行 / 配置文件都没给端口时的兜底默认值。
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 18810
#: `stop`/`restart` 等多久优雅退出才放弃。
SERVER_STOP_TIMEOUT_SECONDS = 10


def _fail(message: str) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _ok(message: str) -> None:
    typer.secho(message, fg=typer.colors.GREEN)


def _warn(message: str) -> None:
    typer.secho(message, fg=typer.colors.YELLOW)


def _default_server_config_path() -> Path:
    """XDG 约定的默认配置文件路径：
    `${XDG_CONFIG_HOME:-~/.config}/farfarfun/funflix-api/config.toml`。

    生产环境直接把配置文件放在这个路径下即可，`funflix-api start` 不用
    带任何参数；`--config` 仍然可以显式覆盖，开发时常用来指向仓库内的文件。
    """
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg_config_home) / "farfarfun" / "funflix-api" / "config.toml"


def _server_state_dir() -> Path:
    """PID/日志统一放在配置目录下，跟 `--config` 默认路径同一棵树管理，
    不依赖仓库或当前工作目录。"""
    return _default_server_config_path().parent


def _server_pid_file() -> Path:
    return _server_state_dir() / "server.pid"


def _server_log_file() -> Path:
    return _server_state_dir() / "server.log"


def _load_server_config(config: Path | None) -> dict[str, Any]:
    """按扩展名解析 `--config` 指向的文件：`.toml` / `.json` / `.env`。

    未显式传 `--config` 时落到 XDG 默认路径；那个路径不存在就是没有配置文件、
    直接用命令行默认值，不算错误。显式传了但文件不存在才报错。
    """
    path = config or _default_server_config_path()
    if not path.exists():
        if config is not None:
            _fail(f"配置文件不存在：{path}")
        return {}

    suffix = path.suffix.lower()
    if suffix == ".toml":
        import tomllib

        data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    elif suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
    elif suffix == ".env":
        from dotenv import dotenv_values

        data = dict(dotenv_values(path))
    else:
        _fail(f"不支持的配置文件格式：{path.suffix}（仅支持 .toml / .json / .env）")
    return {str(key).lower(): value for key, value in data.items() if value is not None}


def _resolve_server_host_port(
    host: str | None, port: int | None, config: Path | None
) -> tuple[str, int]:
    file_config = _load_server_config(config)
    resolved_host = host or str(file_config.get("host") or DEFAULT_SERVER_HOST)
    resolved_port = (
        port if port is not None else int(file_config.get("port") or DEFAULT_SERVER_PORT)
    )
    return resolved_host, resolved_port


def _read_server_pid() -> int | None:
    pid_file = _server_pid_file()
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        return None
    return pid if pid > 1 else None


def _pid_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _resolve_cli_executable() -> str:
    """后台启动时重新拉起自己：优先用 PATH 上装好的 `funflix-api`，找不到就退回
    当前进程的入口脚本——两者对应同一个已安装的包。"""
    return shutil.which("funflix-api") or sys.argv[0]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def _main(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version_callback, is_eager=True, help="打印版本号后退出"
        ),
    ] = False,
) -> None:
    """funflix-api：funflix 的 HTTP API 服务进程。"""


@app.command("run")
def server_run(
    host: Annotated[str | None, typer.Option(help="监听地址，覆盖配置文件")] = None,
    port: Annotated[int | None, typer.Option(help="监听端口，覆盖配置文件")] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", help="配置文件路径（.toml/.json/.env），缺省用 XDG 默认路径"),
    ] = None,
    reload: Annotated[
        bool, typer.Option(help="代码变更自动重载（开发用，不代表托管生命周期）")
    ] = False,
) -> None:
    """前台启动 API 服务，Ctrl-C 停止。

    调试、临时跑一下用这个；要后台常驻用 `funflix-api start`——它内部
    就是拉一个子进程跑这条命令，只是重定向了输出、写了 PID 文件。
    """
    import uvicorn

    resolved_host, resolved_port = _resolve_server_host_port(host, port, config)
    uvicorn.run(
        "funflix_api.app:app",
        host=resolved_host,
        port=resolved_port,
        reload=reload,
        log_level=get_settings().log_level.lower(),
    )


@app.command("start")
def server_start(
    host: Annotated[str | None, typer.Option(help="监听地址，覆盖配置文件")] = None,
    port: Annotated[int | None, typer.Option(help="监听端口，覆盖配置文件")] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", help="配置文件路径（.toml/.json/.env），缺省用 XDG 默认路径"),
    ] = None,
) -> None:
    """后台启动 API 服务。

    拉一个子进程跑 `run`，PID 写到配置目录下的 `server.pid`，输出
    重定向到同目录的 `server.log`。停止/重启见 `stop` / `restart`。
    """
    existing = _read_server_pid()
    if existing is not None and _pid_is_live(existing):
        _fail(f"funflix-api 已在运行（pid {existing}）")

    pid_file = _server_pid_file()
    log_file = _server_log_file()
    pid_file.parent.mkdir(parents=True, exist_ok=True)

    command = [_resolve_cli_executable(), "run"]
    if host is not None:
        command += ["--host", host]
    if port is not None:
        command += ["--port", str(port)]
    if config is not None:
        command += ["--config", str(config)]

    with log_file.open("ab") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pid_file.write_text(f"{process.pid}\n")

    time.sleep(1)
    if process.poll() is not None:
        pid_file.unlink(missing_ok=True)
        _fail(f"funflix-api 启动失败，看日志：{log_file}")

    _, resolved_port = _resolve_server_host_port(host, port, config)
    _ok(f"funflix-api 已启动（pid {process.pid}，端口 {resolved_port}，日志 {log_file}）")


@app.command("stop")
def server_stop() -> None:
    """停止后台运行的 API 服务（发 SIGTERM，等它优雅退出）。"""
    pid_file = _server_pid_file()
    pid = _read_server_pid()
    if pid is None:
        pid_file.unlink(missing_ok=True)
        _warn("funflix-api 未在运行")
        return
    if not _pid_is_live(pid):
        pid_file.unlink(missing_ok=True)
        _warn("PID 文件已失效，已清理")
        return

    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + SERVER_STOP_TIMEOUT_SECONDS
    while _pid_is_live(pid):
        if time.monotonic() >= deadline:
            _fail(f"funflix-api 在 {SERVER_STOP_TIMEOUT_SECONDS}s 内未退出（pid {pid}）")
        time.sleep(0.2)

    pid_file.unlink(missing_ok=True)
    _ok("funflix-api 已停止")


@app.command("restart")
def server_restart(
    host: Annotated[str | None, typer.Option(help="监听地址，覆盖配置文件")] = None,
    port: Annotated[int | None, typer.Option(help="监听端口，覆盖配置文件")] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", help="配置文件路径（.toml/.json/.env），缺省用 XDG 默认路径"),
    ] = None,
) -> None:
    """重启：先 `stop`（没在跑也不报错），再 `start`。"""
    server_stop()
    server_start(host=host, port=port, config=config)


@app.command("status")
def server_status() -> None:
    """查看后台服务是否在跑，以及安装的版本号。"""
    pid = _read_server_pid()
    if pid is not None and _pid_is_live(pid):
        _ok(f"运行中（pid {pid}，版本 {__version__}）")
    elif _server_pid_file().exists():
        _warn(f"PID 文件失效（{_server_pid_file()}）")
    else:
        typer.secho(f"已停止（版本 {__version__}）", fg=typer.colors.BRIGHT_BLACK)
