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

from fastapi import APIRouter, HTTPException, Query, status
from funflix.base.enums import CheckStatus, MediaType, Provider
from funflix.models import Resource, Work, media_resource
from funflix.models.media import UNKNOWN_YEAR
from funflix.schemas.common import Page
from funflix.schemas.media import WorkDetail, WorkSummary
from funflix.services.search import SearchQuery, count_works, search_works
from sqlalchemy import case, select
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from funflix_api.deps import PageDep, SessionDep

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
) -> Page[WorkSummary]:
    """搜索 / 浏览作品。"""
    query = SearchQuery(
        keyword=keyword.strip(),
        media_type=media_type,
        year=year,
        valid_only=valid_only,
        provider=provider,
        limit=paging.size,
        offset=paging.offset,
    )
    total = await count_works(session, query)
    rows = await search_works(session, query)
    return Page[WorkSummary](
        items=[WorkSummary.model_validate(r) for r in rows],
        total=total,
        page=paging.page,
        size=paging.size,
    )


@router.get("/{work_id}", response_model=WorkDetail)
async def get_work(work_id: uuid.UUID, session: SessionDep) -> WorkDetail:
    """作品详情，季列表嵌套，每季带若干网盘资源。

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

    for season in work.seasons:
        rows = list(
            await session.scalars(
                select(Resource)
                .join(media_resource, media_resource.c.resource_id == Resource.id)
                .where(media_resource.c.media_id == season.id)
                # 可用的排前面，其余按入库倒序
                .order_by(
                    case((Resource.check_status == CheckStatus.VALID, 0), else_=1),
                    Resource.id.desc(),
                )
                .limit(MAX_SEASON_RESOURCES)
            )
        )
        # 用 set_committed_value 而不是直接赋值：直接给关系属性赋值会被 ORM 当成
        # 「这就是全部关联」，flush 时把没列进来的关联行删掉 —— 截断展示会变成截断数据。
        set_committed_value(season, "resources", rows)
    return WorkDetail.model_validate(work)
