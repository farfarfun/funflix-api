"""作品查询接口（DESIGN §7.2）。

搜索的主体是 **`Work`（一部剧）**，季挂在它下面 —— 搜「大主宰」返回一条
「大主宰（4 季 / 1496 资源）」，而不是 448 条同名行。季级详情看
`GET /media/{media_id}`（`v1/media.py`）。

列表走 `services.search` 的后端抽象：PG 上是 pg_trgm 模糊匹配，
其余方言回落 LIKE，调用方无感知。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from funflix.base.enums import CheckStatus, MediaType, Provider, Quality
from funflix.models import Media, Resource, Tag, Work, media_resource, media_tag
from funflix.models.media import UNKNOWN_YEAR
from funflix.schemas.common import Page
from funflix.schemas.media import TagOut, WorkDetail, WorkSummary
from funflix.services.search import SearchQuery, WorkSort, count_works, search_works
from pydantic import BaseModel, ConfigDict
from sqlalchemy import case, func, select
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from funflix_api.deps import CurrentUserDep, PageDep, SessionDep


class PopularTagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    name: str
    media_count: int


#: 详情页每季最多返回多少条资源。季行上的 `resource_count` 仍是真实总数。
#:
#: 上限是**按季**而不是按作品：热门剧某一季能有近千条分享，一个全局上限会让
#: 后面的季一条链接都拿不到。每季给一把，整页加载完每季都有能点的东西。
MAX_SEASON_RESOURCES = 50

router = APIRouter(prefix="/works", tags=["works"])


@router.get("", response_model=Page[WorkSummary])
async def list_works(
    session: SessionDep,
    paging: PageDep,
    _: CurrentUserDep,
    keyword: str = Query(
        default="", max_length=128, description="剧名关键词，留空则按入库时间倒序"
    ),
    media_type: Annotated[
        MediaType | None,
        Query(description="按类型筛选；非影视（book/comic/other）只有显式传了才会出现"),
    ] = None,
    year: int | None = Query(
        default=None,
        ge=UNKNOWN_YEAR,
        le=2100,
        description=f"年份；传 {UNKNOWN_YEAR} 查年份未知的作品（出参里这些作品的 year 是 null）",
    ),
    valid_only: bool = Query(default=False, description="只要至少有一条校验通过资源的作品"),
    provider: Annotated[
        Provider | None, Query(description="只要至少有一条该网盘资源的作品")
    ] = None,
    quality: Annotated[
        Quality | None, Query(description="按画质筛选（4k / 1080p / 720p / sd）")
    ] = None,
    tag: str | None = Query(default=None, max_length=64, description="按标签名（题材/地区等）筛选"),
    sort: Annotated[
        WorkSort,
        Query(
            description=(
                "排序方式：latest（入库倒序/关键词相似度）"
                " / resources（资源最多）/ valid（有效最多）"
            )
        ),
    ] = "latest",
) -> Page[WorkSummary]:
    """搜索 / 浏览作品。"""
    query = SearchQuery(
        keyword=keyword.strip(),
        media_type=media_type,
        year=year,
        valid_only=valid_only,
        provider=provider,
        quality=quality,
        tag=tag.strip() if tag else None,
        sort=sort,
        limit=paging.size,
        offset=paging.offset,
    )
    total = await count_works(session, query)
    if total == 0 or paging.offset >= total:
        return Page[WorkSummary](items=[], total=total, page=paging.page, size=paging.size)
    rows = await search_works(session, query)
    return Page[WorkSummary](
        items=[WorkSummary.model_validate(r) for r in rows],
        total=total,
        page=paging.page,
        size=paging.size,
    )


@router.get("/tags", response_model=list[PopularTagOut])
async def list_popular_tags(
    request: Request,
    session: SessionDep,
    _: CurrentUserDep,
    limit: int = Query(default=24, ge=1, le=100),
) -> list[PopularTagOut]:
    """返回热门标签，供检索页快速发现题材/地区。"""
    cache = getattr(request.app.state, "stats_cache", None)

    async def _load() -> list[PopularTagOut]:
        rows = await session.scalars(
            select(Tag)
            .where(Tag.media_count > 0)
            .order_by(Tag.media_count.desc(), Tag.name)
            .limit(limit)
        )
        return [PopularTagOut.model_validate(t) for t in rows]

    if cache is None:
        return await _load()
    return await cache.get_or_load(f"popular_tags:{limit}", _load)


@router.get("/{work_id}", response_model=WorkDetail)
async def get_work(work_id: uuid.UUID, session: SessionDep, _: CurrentUserDep) -> WorkDetail:
    """作品详情，季列表嵌套，每季带若干网盘资源，标签跨季取并集。

    关联对象一律预加载 —— 异步会话下懒加载会在序列化时抛 MissingGreenlet，
    而不是悄悄多发几条查询。

    每季的资源最多返回 `MAX_SEASON_RESOURCES` 条，且可用的排在前面。热门剧集
    会被很多频道反复分享，`media_resource` 只增不删，全量返回能到几 MB ——
    而使用者要的只是「一条能用的链接」。总数看季上的 `resource_count`。
    """
    work = await session.scalar(
        select(Work).where(Work.id == work_id).options(selectinload(Work.seasons))
    )
    if work is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="作品不存在")

    season_ids = [s.id for s in work.seasons]
    resources_by_season: dict[uuid.UUID, list[Resource]] = {sid: [] for sid in season_ids}
    if season_ids:
        # 用窗口函数单条 SQL 一次性拉回各季的 Top MAX_SEASON_RESOURCES 条资源，
        # 消除多季作品的 N+1 串行数据库往返。
        ranked = (
            select(
                media_resource.c.media_id.label("media_id"),
                Resource.id.label("resource_id"),
                func.row_number()
                .over(
                    partition_by=media_resource.c.media_id,
                    order_by=(
                        case((Resource.check_status == CheckStatus.VALID, 0), else_=1),
                        Resource.id.desc(),
                    ),
                )
                .label("rn"),
            )
            .select_from(media_resource)
            .join(Resource, Resource.id == media_resource.c.resource_id)
            .where(media_resource.c.media_id.in_(season_ids))
            .subquery()
        )
        rows = (
            await session.execute(
                select(ranked.c.media_id, Resource)
                .join(Resource, Resource.id == ranked.c.resource_id)
                .where(ranked.c.rn <= MAX_SEASON_RESOURCES)
                .order_by(ranked.c.media_id, ranked.c.rn)
            )
        ).all()
        for media_id, resource in rows:
            resources_by_season[media_id].append(resource)

    for season in work.seasons:
        # 用 set_committed_value 而不是直接赋值：直接给关系属性赋值会被 ORM 当成
        # 「这就是全部关联」，flush 时把没列进来的关联行删掉 —— 截断展示会变成截断数据。
        set_committed_value(season, "resources", resources_by_season[season.id])

    # 标签挂在季上，但题材/地区描述的是整部剧 —— 跨季取并集。
    # `Work` 上没有 tags 关系，所以单独查出来再塞进出参，不走 model_validate。
    detail = WorkDetail.model_validate(work)
    detail.tags = [
        TagOut.model_validate(t)
        for t in await session.scalars(
            select(Tag)
            .join(media_tag, media_tag.c.tag_id == Tag.id)
            .join(Media, Media.id == media_tag.c.media_id)
            .where(Media.work_id == work_id)
            .distinct()
            .order_by(Tag.kind, Tag.name)
        )
    ]
    return detail


@router.get("/{work_id}/related", response_model=list[WorkSummary])
async def list_related_works(
    work_id: uuid.UUID,
    session: SessionDep,
    _: CurrentUserDep,
    limit: int = Query(default=8, ge=1, le=20),
) -> list[WorkSummary]:
    """推荐相关作品：优先按重合标签数排序，无标签或不足时按同类型热门作品补齐。"""
    work = await session.get(Work, work_id)
    if work is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="作品不存在")

    tag_ids = list(
        await session.scalars(
            select(media_tag.c.tag_id)
            .join(Media, Media.id == media_tag.c.media_id)
            .where(Media.work_id == work_id)
            .distinct()
        )
    )

    picked: list[Work] = []
    seen_ids: set[uuid.UUID] = {work_id}

    if tag_ids:
        overlap = func.count(func.distinct(media_tag.c.tag_id))
        by_tags = list(
            await session.scalars(
                select(Work)
                .join(Media, Media.work_id == Work.id)
                .join(media_tag, media_tag.c.media_id == Media.id)
                .where(
                    Work.id != work_id,
                    Work.media_type == work.media_type,
                    media_tag.c.tag_id.in_(tag_ids),
                )
                .group_by(Work.id)
                .order_by(overlap.desc(), Work.valid_resource_count.desc(), Work.id.desc())
                .limit(limit)
            )
        )
        for item in by_tags:
            picked.append(item)
            seen_ids.add(item.id)

    if len(picked) < limit:
        remaining = limit - len(picked)
        fallback = list(
            await session.scalars(
                select(Work)
                .where(
                    Work.id.notin_(seen_ids),
                    Work.media_type == work.media_type,
                    Work.resource_count > 0,
                )
                .order_by(
                    Work.valid_resource_count.desc(),
                    Work.resource_count.desc(),
                    Work.id.desc(),
                )
                .limit(remaining)
            )
        )
        picked.extend(fallback)

    return [WorkSummary.model_validate(item) for item in picked]
