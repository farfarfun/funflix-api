"""异步运维任务通道（202 Accepted + 轮询查询）。

解决重型手动触发（采集 / 解析 / 网盘校验）同步阻塞 HTTP 连接、刷新页面丢失进度的问题。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from funflix.base.config import get_settings
from funflix.base.db import session_scope
from funflix.base.enums import CHECKABLE_PROVIDERS, ParseStatus, Provider
from funflix.models import RawDocument, Source, utcnow
from funflix.services.collect.runner import collect_source
from funflix.services.verify.runner import RateLimiter
from funflix.worker.tasks import run_parse_once, run_verify_once
from pydantic import BaseModel
from sqlalchemy import func, select

from funflix_api.deps import AdminUserDep, SessionDep

router = APIRouter(prefix="/jobs", tags=["jobs"])

JobKind = Literal["collect", "parse", "verify"]
JobStatus = Literal["queued", "running", "succeeded", "failed"]

_MAX_HISTORY = 100
_JOB_CONCURRENCY = 4
_PARSE_JOB_LIMIT = 500
_VERIFY_JOB_LIMIT = 500


class AdminJobOut(BaseModel):
    id: str
    kind: JobKind
    target: str
    target_label: str | None = None
    status: JobStatus
    summary: str | None = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class AdminJobManager:
    """进程内异步运维任务调度器：限制并发、同目标去重、保留最近执行记录。"""

    def __init__(
        self,
        *,
        max_concurrency: int = _JOB_CONCURRENCY,
        on_complete: Callable[[], None] | None = None,
    ) -> None:
        self._sem = asyncio.Semaphore(max_concurrency)
        self._jobs: OrderedDict[str, AdminJobOut] = OrderedDict()
        self._active_keys: dict[str, str] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._on_complete = on_complete

    def list_jobs(self) -> list[AdminJobOut]:
        return list(reversed(self._jobs.values()))

    def get_job(self, job_id: str) -> AdminJobOut | None:
        return self._jobs.get(job_id)

    def clear_finished(self) -> int:
        """清除已完成/失败的历史任务，保留排队中与执行中的记录。"""
        to_remove = [
            job_id for job_id, job in self._jobs.items() if job.status in ("succeeded", "failed")
        ]
        for job_id in to_remove:
            self._jobs.pop(job_id, None)
        return len(to_remove)

    async def shutdown(self) -> None:
        """服务关闭时取消仍在运行的后台运维任务，避免进程退出告警。"""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

    def submit(
        self,
        *,
        kind: JobKind,
        target: str,
        target_label: str | None = None,
        runner: Callable[[], Awaitable[str]],
    ) -> AdminJobOut:
        dedupe_key = f"{kind}:{target}"
        existing_id = self._active_keys.get(dedupe_key)
        if existing_id and existing_id in self._jobs:
            return self._jobs[existing_id]

        job = AdminJobOut(
            id=uuid.uuid4().hex,
            kind=kind,
            target=target,
            target_label=target_label,
            status="queued",
            created_at=utcnow(),
        )
        self._jobs[job.id] = job
        while len(self._jobs) > _MAX_HISTORY:
            oldest_id, _ = self._jobs.popitem(last=False)
            self._active_keys = {k: v for k, v in self._active_keys.items() if v != oldest_id}
        self._active_keys[dedupe_key] = job.id

        async def _run() -> None:
            async with self._sem:
                job.status = "running"
                job.started_at = utcnow()
                try:
                    job.summary = await runner()
                    job.status = "succeeded"
                    if self._on_complete is not None:
                        self._on_complete()
                except Exception as exc:
                    job.error = str(exc) or type(exc).__name__
                    job.status = "failed"
                finally:
                    job.finished_at = utcnow()
                    if self._active_keys.get(dedupe_key) == job.id:
                        self._active_keys.pop(dedupe_key, None)

        task = asyncio.create_task(_run(), name=f"funflix-job-{kind}-{job.id[:8]}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job


def _get_manager(request: Request) -> AdminJobManager:
    mgr = getattr(request.app.state, "admin_jobs", None)
    if mgr is None:
        mgr = AdminJobManager()
        request.app.state.admin_jobs = mgr
    return mgr


@router.get("", response_model=list[AdminJobOut])
async def list_jobs(request: Request, _: AdminUserDep) -> list[AdminJobOut]:
    """列出最近的异步运维任务。"""
    return _get_manager(request).list_jobs()


@router.delete("", response_model=dict[str, int])
async def clear_finished_jobs(request: Request, _: AdminUserDep) -> dict[str, int]:
    """清空已完成/失败的任务历史记录。"""
    return {"cleared": _get_manager(request).clear_finished()}


@router.get("/{job_id}", response_model=AdminJobOut)
async def get_job(job_id: str, request: Request, _: AdminUserDep) -> AdminJobOut:
    job = _get_manager(request).get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    return job


@router.post(
    "/sources/{source_id}/collect",
    response_model=AdminJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_collect_job(
    source_id: uuid.UUID, request: Request, session: SessionDep, _: AdminUserDep
) -> AdminJobOut:
    """异步触发单个采集源采集，立即返回 202 与任务 ID。"""
    source = await session.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="采集源不存在")

    async def _runner() -> str:
        async with session_scope() as bg_session:
            src = await bg_session.get(Source, source_id)
            if src is None:
                raise RuntimeError("采集源已被删除")
            report = await collect_source(bg_session, src)
            await bg_session.commit()
            if not report.ok:
                raise RuntimeError(report.error or "采集失败")
            suffix = "，仍有内容待采" if report.truncated else ""
            return (
                f"拉取 {report.fetched} 条，新增 {report.created}，"
                f"去重 {report.duplicated}，无正文跳过 {report.skipped_empty}{suffix}"
            )

    return _get_manager(request).submit(
        kind="collect",
        target=str(source_id),
        target_label=source.title or source.identifier,
        runner=_runner,
    )


@router.post(
    "/sources/{source_id}/parse",
    response_model=AdminJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_parse_job(
    source_id: uuid.UUID, request: Request, session: SessionDep, _: AdminUserDep
) -> AdminJobOut:
    """异步触发单个采集源解析，立即返回 202 与任务 ID。"""
    source = await session.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="采集源不存在")

    async def _runner() -> str:
        async with session_scope() as bg_session:
            report = await run_parse_once(bg_session, source_id=source_id, limit=_PARSE_JOB_LIMIT)
            remaining = (
                await bg_session.scalar(
                    select(func.count())
                    .select_from(RawDocument)
                    .where(
                        RawDocument.source_id == source_id,
                        RawDocument.parse_status == ParseStatus.PENDING,
                    )
                )
                or 0
            )
            if report.claimed == 0:
                return "没有待解析文本"
            tail = f"，剩余 {remaining} 条" if remaining > 0 else ""
            return f"解析 {report.claimed} 条，成功 {report.succeeded}，失败 {report.failed}{tail}"

    return _get_manager(request).submit(
        kind="parse",
        target=str(source_id),
        target_label=source.title or source.identifier,
        runner=_runner,
    )


@router.post(
    "/providers/{provider}/verify",
    response_model=AdminJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_verify_job(provider: Provider, request: Request, _: AdminUserDep) -> AdminJobOut:
    """异步触发指定网盘校验，立即返回 202 与任务 ID。"""
    if provider not in CHECKABLE_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{provider.value} 暂不支持校验",
        )
    limiter = getattr(request.app.state, "verify_limiter", None)
    if limiter is None:
        limiter = RateLimiter(rate_per_second=get_settings().worker_verify_rate)
        request.app.state.verify_limiter = limiter

    async def _runner() -> str:
        async with session_scope() as bg_session:
            report = await run_verify_once(
                bg_session,
                provider=provider,
                limit=_VERIFY_JOB_LIMIT,
                limiter=limiter,
            )
            if report.claimed == 0:
                return "没有可校验资源"
            return f"校验 {report.claimed} 条，成功 {report.succeeded}，失败 {report.failed}"

    return _get_manager(request).submit(kind="verify", target=provider.value, runner=_runner)
