"""Casdoor 回调接入 Funflix 账号和会话。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.responses import RedirectResponse
from funflix.base.db import get_session
from funflix.models import User
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from funflix_api.app import create_app
from funflix_api.config import read_server_config
from funflix_api.v1.sso import _username, load_config


class FakeOidcClient:
    claims: dict[str, str] = {
        "iss": "https://id.example.com",
        "sub": "farfarfun/alice",
        "preferred_username": "alice",
    }

    async def authorize_redirect(self, request, redirect_uri: str) -> RedirectResponse:
        assert redirect_uri == "https://flix.example.com/api/v1/auth/oidc/callback"
        return RedirectResponse("https://id.example.com/authorize", status_code=302)

    async def authorize_access_token(self, request) -> dict:
        return {"id_token": "validated", "userinfo": self.claims}


@pytest.fixture
def sso_config(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        'oidc_issuer = "https://id.example.com"\n'
        'oidc_client_id = "funflix"\n'
        'oidc_client_secret = "secret"\n'
        'oidc_admin_subject = "built-in/admin-id"\n'
        'oidc_redirect_uri = "https://flix.example.com/api/v1/auth/oidc/callback"\n'
        "oidc_timeout_seconds = 45\n"
        'session_secret = "fixed-test-secret"\n'
        "session_max_age = 120\n"
        "session_cookie_secure = true\n"
    )
    config.chmod(0o600)
    return config


@pytest.mark.parametrize("timeout", [0, -1, "nan", "bad"])
def test_oidc_timeout_must_be_positive(timeout) -> None:
    config = {
        "oidc_issuer": "https://id.example.com",
        "oidc_client_id": "funflix",
        "oidc_client_secret": "secret",
        "oidc_redirect_uri": "https://flix.example.com/api/v1/auth/oidc/callback",
        "oidc_admin_subject": "built-in/admin-id",
        "oidc_timeout_seconds": timeout,
    }
    with pytest.raises(RuntimeError, match="oidc_timeout_seconds"):
        load_config(config)


def test_oidc_timeout_defaults_to_120(sso_config) -> None:
    config = read_server_config(sso_config)
    config.pop("oidc_timeout_seconds")
    assert load_config(config).timeout_seconds == 120


@pytest.mark.asyncio
async def test_oidc_start_uses_pkce_and_nonce(sso_config, monkeypatch) -> None:
    monkeypatch.setenv("FUNFLIX_OIDC_ISSUER", "https://wrong.example.com")
    monkeypatch.setenv("FUNFLIX_SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("FUNFLIX_SESSION_MAX_AGE", "999")
    app = create_app(sso_config)
    assert app.state.oidc_client.client_kwargs["timeout"] == 45
    async with app.state.oidc_client._get_oauth_client() as oidc_http_client:
        assert oidc_http_client.timeout.read == 45
    app.state.oidc_client.load_server_metadata = AsyncMock(
        return_value={"authorization_endpoint": "https://id.example.com/login/oauth/authorize"}
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://flix.example.com"
    ) as client:
        response = await client.get("/api/v1/auth/oidc/login")
    assert response.status_code == 302
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"][0]
    assert params["nonce"][0]
    assert params["state"][0]
    assert "secure" in response.headers["set-cookie"].lower()
    assert "Max-Age=120" in response.headers["set-cookie"]


@pytest.mark.asyncio
async def test_oidc_login_creates_guest_reuses_identity_and_blocks_disabled_user(
    engine, sso_config, monkeypatch
) -> None:
    monkeypatch.setenv("FUNFLIX_OIDC_ISSUER", "https://wrong.example.com")
    monkeypatch.setenv("FUNFLIX_OIDC_ADMIN_SUBJECT", "wrong")
    app = create_app(sso_config)
    assert app.state.oidc_client.client_kwargs["code_challenge_method"] == "S256"
    fake = FakeOidcClient()
    app.state.oidc_client = fake
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def db_session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = db_session
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://flix.example.com"
    ) as client:
        config = await client.get("/api/v1/auth/oidc/config")
        assert config.json() == {"enabled": True}
        assert (await client.post("/api/v1/auth/login", json={})).status_code == 404

        start = await client.get("/api/v1/auth/oidc/login?next=/web/media/42")
        assert start.status_code == 302
        callback = await client.get("/api/v1/auth/oidc/callback")
        assert callback.status_code == 303
        assert callback.headers["location"] == "/web/media/42"
        me = (await client.get("/api/v1/auth/me")).json()
        assert me["role"] == "guest"
        assert me["username"].startswith("sso_alice_")

        await client.get("/api/v1/auth/oidc/login?next=https://evil.example.com/")
        assert (await client.get("/api/v1/auth/oidc/callback")).headers["location"] == "/web/"
        async with maker() as session:
            users = (await session.scalars(select(User))).all()
            assert len(users) == 1
            assert users[0].password_hash is None
            users[0].is_active = False
            await session.commit()

        await client.get("/api/v1/auth/oidc/login")
        assert (await client.get("/api/v1/auth/oidc/callback")).status_code == 403

        fake.claims = {
            "iss": "https://id.example.com",
            "sub": "built-in/admin-id",
            "preferred_username": "admin",
        }
        await client.get("/api/v1/auth/oidc/login")
        assert (await client.get("/api/v1/auth/oidc/callback")).status_code == 303
        assert (await client.get("/api/v1/auth/me")).json()["role"] == "admin"
        assert (await client.get("/api/v1/stats")).status_code == 200

        fake.claims = {
            "iss": "https://id.example.com",
            "sub": "farfarfun/another-user",
            "preferred_username": "admin",
        }
        await client.get("/api/v1/auth/oidc/login")
        assert (await client.get("/api/v1/auth/oidc/callback")).status_code == 303
        assert (await client.get("/api/v1/auth/me")).json()["role"] == "guest"
        assert (await client.get("/api/v1/stats")).status_code == 403

        fake.claims = {
            "iss": "https://id.example.com",
            "sub": "farfarfun/different-identity",
            "preferred_username": "taken",
        }
        async with maker() as session:
            session.add(
                User(
                    username=_username(
                        "https://id.example.com", "farfarfun/different-identity", "taken"
                    ),
                    password_hash="local-hash",
                )
            )
            await session.commit()
        await client.get("/api/v1/auth/oidc/login")
        with pytest.raises(IntegrityError):
            await client.get("/api/v1/auth/oidc/callback")


@pytest.mark.asyncio
async def test_oidc_callback_rejects_wrong_issuer(engine, sso_config) -> None:
    app = create_app(sso_config)
    fake = FakeOidcClient()
    fake.claims = {"iss": "https://other.example.com", "sub": "alice"}
    app.state.oidc_client = fake
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def db_session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = db_session
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://flix.example.com"
    ) as client:
        assert (await client.get("/api/v1/auth/oidc/callback")).status_code == 400
