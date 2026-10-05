"""季级查询接口（DESIGN §7.2）。

搜索和浏览的主体是 `Work`（一部剧），见 `v1/works.py`。这里只剩**一季**的
详情 —— 作品详情页里某一季的资源太多、要翻页看全部时才用得上。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from funflix.base.enums import CheckStatus
from funflix.models import Media, Resource, media_resource
from funflix.schemas.media import MediaDetail
from sqlalchemy import case, select
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from funflix_api.deps import SessionDep

#: 详情页最多返回多少条资源。`resource_count` 仍是真实总数。
MAX_DETAIL_RESOURCES = 200

router = APIRouter(prefix="/media", tags=["media"])


@router.get("/{media_id}", response_model=MediaDetail)
async def get_media(media_id: uuid.UUID, session: SessionDep) -> MediaDetail:
    """一季的详情，含网盘资源与标签。

    关联对象一律预加载 —— 异步会话下懒加载会在序列化时抛 MissingGreenlet，
    而不是悄悄多发几条查询。

    资源最多返回 `MAX_DETAIL_RESOURCES` 条，且可用的排在前面。热门剧集会被
    很多频道反复分享，`media_resource` 只增不删，全量返回能到几 MB ——
    而使用者要的只是「一条能用的链接」。总数看 `resource_count`。
    """
    media = await session.scalar(
        select(Media).where(Media.id == media_id).options(selectinload(Media.tags))
    )
    if media is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="季不存在")

    rows = list(
        await session.scalars(
            select(Resource)
            .join(media_resource, media_resource.c.resource_id == Resource.id)
            .where(media_resource.c.media_id == media_id)
            # 可用的排前面，其余按入库倒序
            .order_by(
                case((Resource.check_status == CheckStatus.VALID, 0), else_=1),
                Resource.id.desc(),
            )
            .limit(MAX_DETAIL_RESOURCES)
        )
    )
    # 用 set_committed_value 而不是直接赋值：直接给关系属性赋值会被 ORM 当成
    # 「这就是全部关联」，flush 时把没列进来的关联行删掉 —— 截断展示会变成截断数据。
    set_committed_value(media, "resources", rows)
    return MediaDetail.model_validate(media)
