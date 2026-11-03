"""Casdoor OIDC 登录，成功后沿用 Funflix 的本地会话和角色。"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from funauth import UserRole
from funflix.models import User
from joserfc.errors import JoseError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.applications import Starlette

from funflix_api.deps import SessionDep, session_store

router = APIRouter(prefix="/auth/oidc", tags=["auth"])


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    admin_subject: str


def load_config() -> OidcConfig | None:
    keys = ("issuer", "client_id", "client_secret", "redirect_uri", "admin_subject")
    values = {key: os.getenv(f"FUNFLIX_OIDC_{key.upper()}", "").strip() for key in keys}
    if not any(values.values()):
        return None
    if not all(values.values()):
        raise RuntimeError(
            "FUNFLIX_OIDC_ISSUER/CLIENT_ID/CLIENT_SECRET/REDIRECT_URI/ADMIN_SUBJECT 必须全部配置"
        )

    issuer = values["issuer"].rstrip("/")
    redirect = urlsplit(values["redirect_uri"])
    issuer_url = urlsplit(issuer)
    if (
        issuer_url.scheme != "https"
        or not issuer_url.hostname
        or issuer_url.path
        or issuer_url.query
        or issuer_url.fragment
        or len(issuer) > 255
    ):
        raise RuntimeError("FUNFLIX_OIDC_ISSUER 必须是 HTTPS 地址")
    if redirect.scheme != "https" or not redirect.hostname or redirect.query or redirect.fragment:
        raise RuntimeError("FUNFLIX_OIDC_REDIRECT_URI 必须是无片段的 HTTPS 地址")
    return OidcConfig(
        issuer=issuer,
        client_id=values["client_id"],
        client_secret=values["client_secret"],
        redirect_uri=values["redirect_uri"],
        admin_subject=values["admin_subject"],
    )


def configure(app: Starlette) -> bool:
    config = load_config()
    app.state.oidc_config = config
    if config is None:
        return False
    oauth = OAuth()
    app.state.oidc_client = oauth.register(
        name="casdoor",
        client_id=config.client_id,
        client_secret=config.client_secret,
        server_metadata_url=f"{config.issuer}/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile email", "code_challenge_method": "S256"},
    )
    return True


def _next_path(value: str) -> str:
    return (
        value
        if value.startswith("/web/") and not value.startswith("//") and "\\" not in value
        else "/web/"
    )


def _username(issuer: str, subject: str, preferred: str) -> str:
    digest = hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest()[:16]
    base = "".join(c if c.isalnum() or c in "_-" else "_" for c in preferred).strip("_")[:39]
    return f"sso_{base or 'user'}_{digest}"


@router.get("/config")
async def oidc_config(request: Request) -> dict[str, bool]:
    return {"enabled": request.app.state.oidc_config is not None}


@router.get("/login")
async def oidc_login(request: Request, next: str = "/web/") -> RedirectResponse:
    config: OidcConfig | None = request.app.state.oidc_config
    if config is None:
        raise HTTPException(404, "未配置单点登录")
    request.session["sso_next"] = _next_path(next)
    return await request.app.state.oidc_client.authorize_redirect(request, config.redirect_uri)


@router.get("/callback")
async def oidc_callback(request: Request, session: SessionDep) -> RedirectResponse:
    config: OidcConfig | None = request.app.state.oidc_config
    if config is None:
        raise HTTPException(404, "未配置单点登录")
    try:
        token = await request.app.state.oidc_client.authorize_access_token(request)
    except (OAuthError, JoseError) as err:
        raise HTTPException(400, "单点登录校验失败") from err
    claims = token.get("userinfo")
    if (
        not token.get("id_token")
        or not claims
        or claims.get("iss") != config.issuer
        or not claims.get("sub")
    ):
        raise HTTPException(400, "单点登录身份无效")

    subject = str(claims["sub"])
    if len(subject) > 255:
        raise HTTPException(400, "单点登录身份过长")
    query = select(User).where(User.oidc_issuer == config.issuer, User.oidc_subject == subject)
    user = await session.scalar(query)
    role = UserRole.ADMIN if subject == config.admin_subject else UserRole.GUEST
    if user is None:
        preferred = str(claims.get("preferred_username") or subject.rsplit("/", 1)[-1])
        user = User(
            username=_username(config.issuer, subject, preferred),
            password_hash=None,
            role=role,
            oidc_issuer=config.issuer,
            oidc_subject=subject,
        )
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            user = await session.scalar(query)
            if user is None:
                raise HTTPException(409, "单点登录账号创建冲突") from None
    if not user.is_active:
        raise HTTPException(403, "账号已停用")
    if user.role != role:
        user.role = role
        await session.commit()
    session_store.login(request, str(user.id))
    next_path = _next_path(request.session.pop("sso_next", "/web/"))
    if next_path == "/web/" and user.role == UserRole.ADMIN:
        next_path = "/web/dashboard"
    return RedirectResponse(next_path, status_code=303)
