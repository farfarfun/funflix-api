"""FastAPI 应用装配。"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import logging
import re
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from funflix.base.config import get_settings
from funflix.base.db import dispose_engine, get_engine
from funflix.services.verify.runner import RateLimiter
from sqlalchemy import text
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from funflix_api.config import (
    load_detail_rate_limit,
    load_session_config,
    load_stats_cache_ttl,
    read_server_config,
)
from funflix_api.deps import SessionDep
from funflix_api.v1 import api_router, sso
from funflix_api.v1.jobs import AdminJobManager
from funflix_api.v1.stats import StatsCache

logger = logging.getLogger(__name__)

_DETAIL_PATH_RE = re.compile(
    r"^/api/v1/(?:works|media)/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class _SlidingWindowLimiter:
    """轻量级进程内按 IP 滑动窗口限流器，防护详情页被脚本顺序/批量抓取。"""

    def __init__(self, limit_per_minute: int = 120, window_seconds: float = 60.0) -> None:
        self.limit = max(0, limit_per_minute)
        self.window = window_seconds
        self._buckets: dict[str, deque[float]] = {}

    def check(self, key: str) -> tuple[bool, int]:
        """返回 (是否放行, 若拒绝时的 Retry-After 秒数)。"""
        if self.limit <= 0:
            return True, 0
        now = time.monotonic()
        cutoff = now - self.window
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= 4096:
                # 先回收已过期的 IP 桶，避免误清空仍在窗口内的高频访问者计数
                stale = [k for k, dq in self._buckets.items() if not dq or dq[-1] <= cutoff]
                for k in stale:
                    self._buckets.pop(k, None)
                if len(self._buckets) >= 4096:
                    self._buckets.clear()
            bucket = deque()
            self._buckets[key] = bucket
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self.limit:
            retry_after = max(1, int(self.window - (now - bucket[0])) + 1)
            return False, retry_after
        bucket.append(now)
        return True, 0


def _package_version() -> str:
    try:
        return importlib.metadata.version("funflix-api")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0-dev"


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
        admin_jobs = getattr(app.state, "admin_jobs", None)
        if admin_jobs is not None:
            await admin_jobs.shutdown()
        await dispose_engine()


def create_app(config: Path | None = None) -> FastAPI:
    settings = get_settings()
    file_config = read_server_config(config)
    session = load_session_config(file_config)
    if session.ephemeral_secret:
        logger.warning(
            "未配置 session_secret，当前使用临时随机密钥；服务重启或多 worker 部署会导致登录态失效"
        )
    app = FastAPI(
        title=settings.app_name,
        version=_package_version(),
        description="影视资源分享文本的结构化采集、解析与网盘链接校验",
        lifespan=lifespan,
        debug=settings.debug,
    )
    app.state.stats_cache = StatsCache(ttl_seconds=load_stats_cache_ttl(file_config))
    app.state.verify_limiter = RateLimiter(rate_per_second=settings.worker_verify_rate)
    app.state.admin_jobs = AdminJobManager(on_complete=app.state.stats_cache.clear)
    app.state.detail_rate_limiter = _SlidingWindowLimiter(load_detail_rate_limit(file_config))

    @app.middleware("http")
    async def observability_and_security_headers(request: Request, call_next):
        req_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        started = time.perf_counter()

        # 详情接口单 IP 限流，防批量枚举抓取（见 TODO §2.4）
        if request.method == "GET" and _DETAIL_PATH_RE.match(request.url.path):
            forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            client_ip = forwarded or (request.client.host if request.client else "unknown")
            allowed, retry_after = request.app.state.detail_rate_limiter.check(client_ip)
            if not allowed:
                return JSONResponse(
                    {"detail": "请求过于频繁，请稍后再试"},
                    status_code=429,
                    headers={
                        "Retry-After": str(retry_after),
                        "X-Request-ID": req_id,
                    },
                )

        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers.setdefault("X-Request-ID", req_id)
        response.headers["X-Process-Time"] = f"{elapsed_ms:.1f}ms"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    app.add_middleware(GZipMiddleware, minimum_size=1024)
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
    async def healthz(session: SessionDep) -> dict[str, str]:
        await session.execute(text("SELECT 1"))
        return {"status": "ok"}

    return app


app = create_app()
