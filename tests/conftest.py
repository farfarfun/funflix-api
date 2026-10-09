from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from funauth import UserRole
from funflix.base.db import get_session
from funflix.models import Base, User
from funflix.security import hash_password
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from funflix_api.app import create_app

#: 测试用的登录账号。运维接口的读写都要先登录。
ADMIN_USERNAME = "tester"
ADMIN_PASSWORD = "test-password"

#: 访客账号。能进站看作品，但撞运维接口拿 403。
GUEST_USERNAME = "visitor"
GUEST_PASSWORD = "visitor-password"


@pytest_asyncio.fixture
async def engine() -> AsyncIterator:
    """每个测试一个独立的内存库。

    StaticPool 让所有连接复用同一个内存数据库 —— 否则 :memory: 每开一条连接
    就是一个全新的空库，建表和查询会落在不同的库上。
    """
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s


@pytest_asyncio.fixture
async def client(engine) -> AsyncIterator[AsyncClient]:
    app = create_app()
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    # 绕过 lifespan：真实 lifespan 会连全局引擎，测试要用的是上面的内存库
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _logged_in_client(
    engine, session, username: str, password: str, role: UserRole
) -> AsyncIterator[AsyncClient]:
    """建号 + 登录，返回带着 cookie 的客户端。

    角色必须显式给。ORM 默认是 `GUEST`（漏传时往最小权限掉），所以这里不写
    `role=UserRole.ADMIN` 的话运维接口全都会 403。
    """
    session.add(User(username=username, password_hash=hash_password(password), role=role))
    await session.commit()

    app = create_app()
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/api/v1/auth/login", json={"username": username, "password": password})
        assert resp.status_code == 200, resp.text
        yield c


@pytest_asyncio.fixture
async def admin_client(engine, session) -> AsyncIterator[AsyncClient]:
    """已登录的管理员客户端，用于运维接口。"""
    async for c in _logged_in_client(
        engine, session, ADMIN_USERNAME, ADMIN_PASSWORD, UserRole.ADMIN
    ):
        yield c


@pytest_asyncio.fixture
async def guest_client(engine, session) -> AsyncIterator[AsyncClient]:
    """已登录的访客客户端：过了站点门禁，但进不了运维区。"""
    async for c in _logged_in_client(
        engine, session, GUEST_USERNAME, GUEST_PASSWORD, UserRole.GUEST
    ):
        yield c
