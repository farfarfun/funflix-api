"""流水线统计接口 —— `funflix status` 的 HTTP 版本。

要求登录：虽然不含链接、文本或凭据，但属于「运维」视角的数据，跟其余
运维接口一起统一收进登录态之后。

内置进程级短 TTL 缓存 + 单飞锁（single-flight），避免大盘轮询或多管理员同时
刷新压垮数据库；传 `?refresh=true` 可强制穿透缓存。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Query, Request
from funflix.schemas.stats import PipelineStatsOut
from funflix.services.stats import collect_provider_stats, collect_stats

from funflix_api.deps import AdminUserDep, SessionDep

router = APIRouter(prefix="/stats", tags=["stats"])


class StatsCache:
    """进程内短 TTL + 单飞统计缓存，按 key 隔离 full / providers 两类视图。"""

    def __init__(self, ttl_seconds: float = 15.0) -> None:
        self.ttl_seconds = max(0.0, ttl_seconds)
        self._entries: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get_or_load(
        self, key: str, loader: Callable[[], Awaitable[Any]], *, refresh: bool = False
    ) -> Any:
        if self.ttl_seconds <= 0:
            return await loader()
        now = time.monotonic()
        if not refresh and key in self._entries:
            expires_at, value = self._entries[key]
            if now < expires_at:
                return value
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            if not refresh and key in self._entries:
                expires_at, value = self._entries[key]
                if now < expires_at:
                    return value
            value = await loader()
            self._entries[key] = (time.monotonic() + self.ttl_seconds, value)
            return value

    def clear(self) -> None:
        self._entries.clear()


@router.get("", response_model=PipelineStatsOut)
async def get_stats(
    request: Request,
    session: SessionDep,
    _: AdminUserDep,
    refresh: bool = Query(default=False, description="强制穿透缓存重新聚合"),
) -> PipelineStatsOut:
    """采集 → 抽取 → 作品/资源 → 校验，各环节的记录数与分布。"""
    cache: StatsCache | None = getattr(request.app.state, "stats_cache", None)

    async def _load() -> PipelineStatsOut:
        return PipelineStatsOut.model_validate(await collect_stats(session))

    if cache is None:
        return await _load()
    return await cache.get_or_load("full", _load, refresh=refresh)


@router.get("/providers", response_model=dict[str, dict[str, int]])
async def get_provider_stats(
    request: Request,
    session: SessionDep,
    _: AdminUserDep,
    refresh: bool = Query(default=False, description="强制穿透缓存重新聚合"),
) -> dict[str, dict[str, int]]:
    """仅返回各网盘的校验状态分布（单条 SQL 聚合），供网盘管理页轻量化读取。"""
    cache: StatsCache | None = getattr(request.app.state, "stats_cache", None)

    async def _load() -> dict[str, dict[str, int]]:
        return await collect_provider_stats(session)

    if cache is None:
        return await _load()
    return await cache.get_or_load("providers", _load, refresh=refresh)
