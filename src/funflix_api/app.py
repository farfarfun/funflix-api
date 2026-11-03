"""FastAPI 应用装配。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from funflix.base.config import get_settings
from funflix.base.db import dispose_engine, get_engine
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from funflix_api.config import load_session_config, read_server_config
from funflix_api.v1 import api_router, sso

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # 启动即探一次库，让配置错误在启动时暴露，而不是等第一个请求打进来才 500
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("funflix 启动完成，数据库=%s", settings.database_url.split("://", 1)[0])

    task: asyncio.Task[None] | None = None
    stop: asyncio.Event | None = None
    if settings.worker_enabled:
        from funflix.worker import spawn

        task, stop = spawn(settings)
        logger.info("进程内后台 worker 已启动")
    else:
        logger.info("进程内后台 worker 未启用（FUNFLIX_WORKER_ENABLED=true 开启）")

    try:
        yield
    finally:
        if task is not None and stop is not None:
            stop.set()
            # 给它一轮的时间收尾。超时就取消 —— 卡住的多半是某次外部调用，
            # 等下去没有意义，任务本身有租约兜底，重启后会被重新领取。
            try:
                await asyncio.wait_for(task, timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await dispose_engine()


def create_app(config: Path | None = None) -> FastAPI:
    settings = get_settings()
    file_config = read_server_config(config)
    session = load_session_config(file_config)
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="影视资源分享文本的结构化采集、解析与网盘链接校验",
        lifespan=lifespan,
        debug=settings.debug,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=session.session_secret,
        max_age=session.session_max_age,
        same_site="lax",
        https_only=session.session_cookie_secure,
    )
    if sso.configure(app, file_config):

        @app.middleware("http")
        async def disable_local_login(request: Request, call_next):
            if request.method == "POST" and request.url.path in {
                f"{settings.api_prefix}/auth/login",
                f"{settings.api_prefix}/auth/register",
            }:
                return JSONResponse({"detail": "请使用单点登录"}, status_code=404)
            return await call_next(request)

    app.include_router(api_router, prefix=settings.api_prefix)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok"}

    return app


app = create_app()
