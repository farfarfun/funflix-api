"""登录 / 登出 / 当前用户 / 注册 / 注册开关。

**这个模块里没有 handler**。五个端点全都来自 `funauth.contrib.fastapi`，这里只把
本仓的三样东西递进去：会话依赖、cookie 会话存放、注册开关。校验密码、消耗邀请码、
决定注册出来是什么角色都在 funauth 里。

那五个路由**全部公开**，不挂门禁 —— 否则没登录的人连登录接口都打不开。其余路由
分两层：作品检索/详情挂 `CurrentUserDep`（整站门禁），运维区挂 `AdminUserDep`。

出参用 funauth 的 `UserOut`（id / username / role），本仓不需要多返回字段；要加的
话继承它再传给 `user_out=`，不要在这里重写一份 handler。
"""

from __future__ import annotations

from funauth.contrib.fastapi import make_auth_router
from funflix.services.account import accounts

from funflix_api.deps import RegistrationEnabledDep, SessionDep, session_store

router = make_auth_router(
    accounts=accounts,
    session_dep=SessionDep,
    store=session_store,
    registration_enabled_dep=RegistrationEnabledDep,
)
