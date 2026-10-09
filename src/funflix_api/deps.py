"""FastAPI 依赖。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Query, status
from funauth.contrib.fastapi import CookieSessionStore, make_user_deps
from funflix.base.config import Settings, get_settings
from funflix.base.db import get_session
from funflix.schemas.common import MAX_PAGE_NUMBER, MAX_PAGE_SIZE
from funflix.services.account import accounts
from sqlalchemy.ext.asyncio import AsyncSession

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def registration_enabled(settings: SettingsDep) -> bool:
    """注册开关，包成一个依赖传给 funauth 的路由工厂。

    不直接把 `settings.registration_enabled` 取出来当 bool 传过去：那是一次性读
    值，测试里 `dependency_overrides[get_settings]` 就再也影响不到它了。
    """
    return settings.registration_enabled


RegistrationEnabledDep = Annotated[bool, Depends(registration_enabled)]


@dataclass(slots=True)
class PageParams:
    """翻页入参。

    四个列表接口曾各写一遍 page/size 的声明与 `(page - 1) * size`，
    上限已经先漂了一次（两处 100、两处 200）。收敛到这里，
    翻页语义只有一个定义点。
    """

    page: int
    size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


def page_params(
    page: Annotated[int, Query(ge=1, le=MAX_PAGE_NUMBER, description="页码，从 1 开始")] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE, description="每页条数")] = 20,
) -> PageParams:
    return PageParams(page=page, size=size)


PageDep = Annotated[PageParams, Depends(page_params)]


async def get_or_404(session: AsyncSession, model: type[Any], pk: Any, detail: str) -> Any:
    """按主键取，取不到就 404。"""
    row = await session.get(model, pk)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    return row


#: 登录态存哪儿。Starlette 的签名 cookie，`main.py` 里装的 `SessionMiddleware`
#: 决定 secret_key / 有效期 / `https_only`。`v1/auth.py` 共用这一个实例。
session_store = CookieSessionStore()

#: 两道门，实现在 funauth 里。
#:
#: - `CurrentUserDep` 是**站点门禁**：已登录且启用就放过，不看角色。作品检索/详情
#:   挂的是它（整站要口令才能进）。
#: - `AdminUserDep` 在它之上再要求 `ADMIN`，运维区挂它，guest 撞上拿 403。
#:
#: 门禁必须落在这里。`funflix-web/server/` 只是静态文件 + 反向代理，没有任何鉴权，
#: 光靠前端路由守卫挡不住直接 curl 接口。
#:
#: 两个注解的用户类型是 `Any` 而不是 `User` —— funauth 不认识本仓的模型类。现在
#: 所有调用点都写成 `_: AdminUserDep`（只要门禁、不碰用户对象），不影响什么。
_guards = make_user_deps(accounts=accounts, session_dep=SessionDep, store=session_store)

CurrentUserDep = _guards.current_user
AdminUserDep = _guards.admin_user
