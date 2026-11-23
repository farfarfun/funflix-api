"""网盘资源查询接口。

与 `/media` 的区别是视角：这里按链接本身筛（哪个网盘、校验成不成功），
用于运维排查「夸克最近是不是大面积失效了」这类问题。

**这个模块整体要求登录**：它按 `provider` / `check_status` 成页吐出
整库的链接与提取码，是运维视角而非产品视角。面向使用者的接口是
`/media` 与 `/media/{id}`，保持开放。

> ⚠️ 这道锁防的是**成批导出的便利**，不是保密。链接本身来自公开频道，
> 而且 `/media/{id}` 无凭据就会返回同一份 url 与 passcode ——
> `/resources` 这层锁挡不住已经拿到某个具体 media id 的人。
> 把这里当成机密边界会得到虚假的安全感。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, Request, status
from funflix.base.config import get_settings
from funflix.base.enums import CHECKABLE_PROVIDERS, CheckStatus, Provider, Quality
from funflix.models import LinkCheck, Resource, media_resource
from funflix.schemas.common import Page
from funflix.schemas.media import (
    LinkCheckOut,
    ProviderVerifyReportOut,
    ResourceDetail,
    ResourceOut,
)
from funflix.services.verify.runner import RateLimiter, check_resource
from funflix.worker.tasks import run_verify_once
from sqlalchemy import exists, func, or_, select

from funflix_api.deps import AdminUserDep, PageDep, SessionDep, get_or_404

router = APIRouter(prefix="/resources", tags=["resources"])

_VERIFY_TRIGGER_LIMIT = 500
_RECENT_CHECKS_LIMIT = 20


async def _build_resource_detail(session: SessionDep, resource: Resource) -> ResourceDetail:
    """组装资源详情并附带最近若干次校验历史（按 (provider, share_id) 查询）。"""
    checks = list(
        await session.scalars(
            select(LinkCheck)
            .where(
                LinkCheck.provider == resource.provider,
                LinkCheck.share_id == resource.share_id,
            )
            .order_by(LinkCheck.checked_at.desc(), LinkCheck.id.desc())
            .limit(_RECENT_CHECKS_LIMIT)
        )
    )
    detail = ResourceDetail.model_validate(resource)
    detail.recent_checks = [LinkCheckOut.model_validate(c) for c in checks]
    return detail


@router.get("", response_model=Page[ResourceOut])
async def list_resources(
    session: SessionDep,
    paging: PageDep,
    _: AdminUserDep,
    provider: Provider | None = None,
    check_status: CheckStatus | None = None,
    quality: Quality | None = None,
    q: str | None = Query(default=None, max_length=128, description="按链接或原始标题模糊搜索"),
    orphan_only: bool = Query(default=False, description="仅返回尚未归属任何作品的资源"),
) -> Page[ResourceOut]:
    """按网盘 / 校验状态 / 画质 / 关键词翻页。

    排序用 `id` 而不是 `last_seen_at`：后者会被 ingest 在每次同一条链接被
    重新分享时改写，翻页途中行会在页与页之间来回移动 —— 客户端翻到第 2 页
    时会重复看到第 1 页的行，被挤下去的那条则永远看不到。`id` 不可变，
    且与「入库先后」同序，翻页结果稳定。
    """
    conditions = []
    if provider is not None:
        conditions.append(Resource.provider == provider)
    if check_status is not None:
        conditions.append(Resource.check_status == check_status)
    if quality is not None:
        conditions.append(Resource.quality == quality)
    if q is not None and q.strip():
        term = q.strip()
        conditions.append(
            or_(
                Resource.url.icontains(term, autoescape=True),
                Resource.title_raw.icontains(term, autoescape=True),
            )
        )
    if orphan_only:
        conditions.append(~exists(select(1).where(media_resource.c.resource_id == Resource.id)))

    total = await session.scalar(select(func.count()).select_from(Resource).where(*conditions)) or 0
    if total == 0 or paging.offset >= total:
        return Page[ResourceOut](items=[], total=total, page=paging.page, size=paging.size)
    rows = await session.scalars(
        select(Resource)
        .where(*conditions)
        .order_by(Resource.id.desc())
        .offset(paging.offset)
        .limit(paging.size)
    )
    return Page[ResourceOut](
        items=[ResourceOut.model_validate(r) for r in rows],
        total=total,
        page=paging.page,
        size=paging.size,
    )


@router.get("/providers/checkable", response_model=list[Provider])
async def list_checkable_providers(_: AdminUserDep) -> list[Provider]:
    """返回当前真正实现了探针的网盘，供界面禁用其余校验按钮。"""
    return sorted(CHECKABLE_PROVIDERS, key=lambda provider: provider.value)


@router.get("/providers/status", response_model=dict[str, dict[str, float]])
async def get_providers_limiter_status(
    request: Request, _: AdminUserDep
) -> dict[str, dict[str, float]]:
    """返回各可校验网盘当前的自适应限流系数与熔断冷却剩余秒数。"""
    limiter = getattr(request.app.state, "verify_limiter", None)
    if limiter is None:
        limiter = RateLimiter(rate_per_second=get_settings().worker_verify_rate)
        request.app.state.verify_limiter = limiter
    return limiter.intervals.status_snapshot(sorted(CHECKABLE_PROVIDERS, key=lambda p: p.value))


@router.post("/providers/{provider}/verify", response_model=ProviderVerifyReportOut)
async def verify_provider(
    provider: Provider, request: Request, session: SessionDep, _: AdminUserDep
) -> ProviderVerifyReportOut:
    """手动强制复查指定网盘最久未校验的一批资源。"""
    if provider not in CHECKABLE_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{provider.value} 暂不支持校验",
        )
    limiter = getattr(request.app.state, "verify_limiter", None)
    if limiter is None:
        limiter = RateLimiter(rate_per_second=get_settings().worker_verify_rate)
        request.app.state.verify_limiter = limiter
    report = await run_verify_once(
        session,
        provider=provider,
        limit=_VERIFY_TRIGGER_LIMIT,
        limiter=limiter,
    )
    return ProviderVerifyReportOut(
        provider=provider,
        claimed=report.claimed,
        succeeded=report.succeeded,
        failed=report.failed,
        reclaimed=report.reclaimed,
        abandoned=report.abandoned,
    )


@router.get("/{resource_id}", response_model=ResourceDetail)
async def get_resource(
    resource_id: uuid.UUID, session: SessionDep, _: AdminUserDep
) -> ResourceDetail:
    """单条资源详情，附带最近校验历史供运维排查。"""
    resource = await get_or_404(session, Resource, resource_id, "资源不存在")
    return await _build_resource_detail(session, resource)


@router.post("/{resource_id}/recheck", response_model=ResourceDetail)
async def recheck_resource(
    resource_id: uuid.UUID, request: Request, session: SessionDep, _: AdminUserDep
) -> ResourceDetail:
    """立即对单条网盘资源执行一次校验，并返回最新状态与校验历史。"""
    resource = await get_or_404(session, Resource, resource_id, "资源不存在")
    if resource.provider not in CHECKABLE_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{resource.provider.value} 暂不支持校验",
        )
    limiter = getattr(request.app.state, "verify_limiter", None)
    if limiter is None:
        limiter = RateLimiter(rate_per_second=get_settings().worker_verify_rate)
        request.app.state.verify_limiter = limiter
    await check_resource(session, resource, limiter=limiter)
    await session.commit()
    await session.refresh(resource)
    return await _build_resource_detail(session, resource)
