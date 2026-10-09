"""两道门禁 + 登录 / 注册的 HTTP 行为。

站点从「半开放」变成「整站要口令」之后，门有两层，这批测试钉的就是这两层：

- **站点门禁**：没登录打任何业务接口（含作品检索/详情）都是 401；`/healthz` 和
  `/api/v1/auth/*` 保持匿名可访问，否则谁都进不来。
- **运维区**：访客登录了也只能看作品，撞 sources / raw / resources / stats 一律
  **403**（不是 401 —— 他已经登录了，只是权限不够）。

登录与注册这组端点的实现在 funauth（`funauth.contrib.fastapi`），本仓只递了三样
东西进去。所以这里测的是「接得对不对」：会话 cookie 在不在、注册开关的
`dependency_overrides` 有没有穿过去、邀请码那条路通不通。funauth 自己的单测覆盖
的是另一面 —— 并发、消息不泄露、状态码映射。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from funauth import UserRole
from funflix.base.config import Settings, get_settings
from funflix.base.db import get_session
from funflix.base.enums import ParseStatus
from funflix.models import Extraction, RawDocument, Source, User, utcnow
from funflix.security import hash_password
from funflix.services.account import accounts
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from funflix_api.app import create_app

USERNAME = "tester"
PASSWORD = "test-password"
GUEST_USERNAME = "visitor"

#: 运维区的代表性端点，四个模块各取一个。访客撞上去应该一律 403。
OPS_PATHS = ["/api/v1/sources", "/api/v1/raw", "/api/v1/resources", "/api/v1/stats"]


def _client_with(engine, settings: Settings) -> AsyncClient:
    app = create_app()
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            try:
                yield s
            except Exception:
                # 注册撞用户名时要靠这里把扣掉的邀请码名额退回去
                await s.rollback()
                raise

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_settings] = lambda: settings
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@asynccontextmanager
async def _login_as(
    engine, session, username: str, role: UserRole, settings: Settings | None = None
) -> AsyncIterator[AsyncClient]:
    """建号并走一遍真实登录接口，交出带着会话 cookie 的客户端。

    角色必须显式给 —— ORM 默认是 `GUEST`（漏传时往最小权限掉）。
    """
    session.add(User(username=username, password_hash=hash_password(PASSWORD), role=role))
    await session.commit()
    async with _client_with(engine, settings or Settings()) as client:
        resp = await client.post(
            "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
        )
        assert resp.status_code == 200, resp.text
        yield client


@pytest_asyncio.fixture
async def anon(engine) -> AsyncIterator[AsyncClient]:
    """没登录的客户端。"""
    async with _client_with(engine, Settings()) as c:
        yield c


@pytest_asyncio.fixture
async def authed(engine, session) -> AsyncIterator[AsyncClient]:
    """已登录的**管理员**客户端。两道门都过。"""
    async with _login_as(engine, session, USERNAME, UserRole.ADMIN) as c:
        yield c


@pytest_asyncio.fixture
async def guest(engine, session) -> AsyncIterator[AsyncClient]:
    """已登录的**访客**客户端：过了站点门禁，进不了运维区。"""
    async with _login_as(engine, session, GUEST_USERNAME, UserRole.GUEST) as c:
        yield c


PAYLOAD = {"url": "https://t.me/s/demo_channel"}


@pytest.mark.asyncio
class TestOpsEndpointsRequireLogin:
    async def test_create_source_without_login_is_rejected(self, anon) -> None:
        assert (await anon.post("/api/v1/sources", json=PAYLOAD)).status_code == 401

    async def test_create_source_after_login_succeeds(self, authed) -> None:
        assert (await authed.post("/api/v1/sources", json=PAYLOAD)).status_code == 201

    async def test_list_sources_requires_login(self, anon) -> None:
        assert (await anon.get("/api/v1/sources")).status_code == 401

    async def test_stats_requires_login(self, anon) -> None:
        """`/stats` 曾经完全匿名开放，换成登录态后一并收进来。"""
        assert (await anon.get("/api/v1/stats")).status_code == 401

    async def test_resources_requires_login(self, anon) -> None:
        assert (await anon.get("/api/v1/resources")).status_code == 401

    async def test_raw_requires_login(self, anon) -> None:
        assert (await anon.get("/api/v1/raw")).status_code == 401
        assert (await anon.post("/api/v1/raw", json={"content": "x"})).status_code == 401

    async def test_delete_requires_login(self, authed) -> None:
        """删源会丢掉水位游标，绝不能匿名。"""
        created = await authed.post("/api/v1/sources", json=PAYLOAD)
        source_id = created.json()["id"]
        assert (await authed.delete(f"/api/v1/sources/{source_id}")).status_code == 204

    async def test_reset_source_cursor(self, authed, session) -> None:
        created = await authed.post("/api/v1/sources", json=PAYLOAD)
        source = await session.get(Source, uuid.UUID(created.json()["id"]))
        assert source is not None
        source.cursor_message_id = "123"
        source.backfill_cursor_id = "100"
        source.backfill_done = True
        source.extra = {"revision": 9}
        await session.commit()

        response = await authed.post(f"/api/v1/sources/{source.id}/reset-cursor")
        await session.refresh(source)

        assert response.status_code == 204
        assert source.cursor_message_id is None
        assert source.backfill_cursor_id is None
        assert source.backfill_done is False
        assert source.extra == {}

    async def test_reset_source_parse(self, authed, session) -> None:
        created = await authed.post("/api/v1/sources", json=PAYLOAD)
        source = await session.get(Source, uuid.UUID(created.json()["id"]))
        assert source is not None
        now = utcnow()
        done = RawDocument(
            content="done",
            content_hash="1" * 64,
            source=source,
            source_type=source.source_type,
            collected_at=now,
            parse_status=ParseStatus.DONE,
            parse_attempts=2,
            last_parsed_at=now,
        )
        skipped = RawDocument(
            content="skipped",
            content_hash="2" * 64,
            source=source,
            source_type=source.source_type,
            collected_at=now,
            parse_status=ParseStatus.SKIPPED,
            parse_attempts=1,
            parse_error="old error",
            lease_until=now,
            next_parse_at=now,
            last_parsed_at=now,
        )
        extraction = Extraction(
            raw_document=done,
            model="test",
            prompt_version="v1",
            output={},
            stats={},
        )
        session.add_all([done, skipped, extraction])
        await session.commit()
        extraction_id = extraction.id

        before = await authed.get(f"/api/v1/sources/{source.id}")
        response = await authed.post(f"/api/v1/sources/{source.id}/reset-parse")
        await session.refresh(done)
        await session.refresh(skipped)
        session.expunge(extraction)

        assert before.json()["raw_parsed"] == 2
        assert response.status_code == 200
        assert response.json() == 2
        for document in (done, skipped):
            assert document.parse_status == ParseStatus.PENDING
            assert document.parse_attempts == 0
            assert document.parse_error is None
            assert document.lease_until is None
            assert document.next_parse_at is None
            assert document.last_parsed_at is None
        assert await session.get(Extraction, extraction_id) is None

    async def test_logout_revokes_access(self, authed) -> None:
        assert (await authed.post("/api/v1/auth/logout")).status_code == 204
        assert (await authed.get("/api/v1/sources")).status_code == 401


@pytest.mark.asyncio
class TestLogin:
    async def test_wrong_password_is_rejected(self, engine, session) -> None:
        session.add(User(username=USERNAME, password_hash=hash_password(PASSWORD)))
        await session.commit()
        async with _client_with(engine, Settings()) as c:
            resp = await c.post(
                "/api/v1/auth/login", json={"username": USERNAME, "password": "wrong"}
            )
            assert resp.status_code == 401

    async def test_unknown_username_is_rejected(self, engine) -> None:
        async with _client_with(engine, Settings()) as c:
            resp = await c.post("/api/v1/auth/login", json={"username": "nobody", "password": "x"})
            assert resp.status_code == 401

    async def test_me_reflects_session(self, authed) -> None:
        resp = await authed.get("/api/v1/auth/me")
        assert resp.status_code == 200
        assert resp.json()["username"] == USERNAME

    async def test_me_is_null_when_anonymous(self, anon) -> None:
        resp = await anon.get("/api/v1/auth/me")
        assert resp.status_code == 200
        assert resp.json() is None


@pytest.mark.asyncio
class TestOpsEndpointsRejectGuests:
    """访客登录了也进不了运维区。

    403 而不是 401：他已经登录了，只是权限不够。前端靠 `/auth/me` 里的 `role`
    不渲染运维入口，但那只是省掉一次无意义的点击 —— 真正的拦截在这里。
    """

    @pytest.mark.parametrize("path", OPS_PATHS)
    async def test_guest_gets_403(self, guest, path) -> None:
        assert (await guest.get(path)).status_code == 403

    async def test_guest_cannot_create_source(self, guest) -> None:
        assert (await guest.post("/api/v1/sources", json=PAYLOAD)).status_code == 403

    @pytest.mark.parametrize("path", OPS_PATHS)
    async def test_admin_gets_200(self, authed, path) -> None:
        assert (await authed.get(path)).status_code == 200

    async def test_demoting_takes_effect_without_relogin(self, authed, session) -> None:
        """降级立刻生效 —— 会话里只存 user_id，角色每个请求重新查库。"""
        assert (await authed.get("/api/v1/stats")).status_code == 200

        user = await accounts.get_by_username(session, USERNAME)
        assert user is not None
        user.role = UserRole.GUEST
        await session.commit()

        assert (await authed.get("/api/v1/stats")).status_code == 403


@pytest.mark.asyncio
class TestSiteGate:
    """整站门禁：作品检索/详情也要登录，`/healthz` 和 auth 那组保持公开。"""

    @pytest.mark.parametrize("path", ["/api/v1/works", "/api/v1/works/" + str(uuid.uuid4())])
    async def test_works_require_login(self, anon, path) -> None:
        assert (await anon.get(path)).status_code == 401

    async def test_media_requires_login(self, anon) -> None:
        resp = await anon.get(f"/api/v1/media/{uuid.uuid4()}")
        # 401 而不是 404：门禁在取数据之前，不存在的 id 也不该泄露出去
        assert resp.status_code == 401

    async def test_guest_can_browse_works(self, guest) -> None:
        assert (await guest.get("/api/v1/works")).status_code == 200

    @pytest.mark.parametrize("path", ["/healthz", "/api/v1/auth/me", "/api/v1/auth/config"])
    async def test_stays_public(self, anon, path) -> None:
        """这几条不能挂门禁，否则没登录的人连登录页都渲染不出来。"""
        assert (await anon.get(path)).status_code == 200


@pytest.mark.asyncio
class TestRegistration:
    async def test_switch_off_blocks_register(self, engine, session) -> None:
        """开关是个依赖，`dependency_overrides[get_settings]` 必须能穿到它。

        funauth 侧收的是 `registration_enabled_dep` 而不是一个 bool，就是为了这个
        —— 捕获值会绕过覆盖，于是这条测试会变成假绿。
        """
        code = await accounts.issue_invite(session)
        async with _client_with(engine, Settings(registration_enabled=False)) as c:
            assert (await c.get("/api/v1/auth/config")).json() == {"registration_enabled": False}
            resp = await c.post(
                "/api/v1/auth/register",
                json={"username": "new", "password": "abcdef", "invite_code": code.code},
            )
            assert resp.status_code == 403

    async def test_valid_code_creates_guest_and_logs_in(self, engine, session) -> None:
        code = await accounts.issue_invite(session)
        async with _client_with(engine, Settings(registration_enabled=True)) as c:
            resp = await c.post(
                "/api/v1/auth/register",
                json={"username": "new", "password": "abcdef", "invite_code": code.code},
            )
            assert resp.status_code == 201
            # 自助注册出来的恒为 guest，接口无权指定角色
            assert resp.json()["role"] == "guest"
            # 注册完直接进站，不用再登一次
            assert (await c.get("/api/v1/works")).status_code == 200
            # 但进不了运维区
            assert (await c.get("/api/v1/stats")).status_code == 403

    async def test_no_code_is_422(self, engine) -> None:
        """邀请码必填 —— 少传一个字段不能变成「无码注册」。"""
        async with _client_with(engine, Settings(registration_enabled=True)) as c:
            resp = await c.post(
                "/api/v1/auth/register", json={"username": "new", "password": "abcdef"}
            )
            assert resp.status_code == 422

    async def test_used_and_unknown_codes_are_indistinguishable(self, engine, session) -> None:
        """用完的码和不存在的码必须返回同一句话。

        拆开这个接口就成了「这个码存不存在」的探测器。
        """
        code = await accounts.issue_invite(session, max_uses=1)
        async with _client_with(engine, Settings(registration_enabled=True)) as c:
            first = await c.post(
                "/api/v1/auth/register",
                json={"username": "first", "password": "abcdef", "invite_code": code.code},
            )
            assert first.status_code == 201

            exhausted = await c.post(
                "/api/v1/auth/register",
                json={"username": "second", "password": "abcdef", "invite_code": code.code},
            )
            unknown = await c.post(
                "/api/v1/auth/register",
                json={"username": "third", "password": "abcdef", "invite_code": "NOSUCHCD"},
            )

        assert {r.status_code for r in (exhausted, unknown)} == {400}
        assert len({r.json()["detail"] for r in (exhausted, unknown)}) == 1

    async def test_duplicate_username_is_409_and_refunds_the_slot(self, engine, session) -> None:
        """撞用户名 409，而且扣掉的名额要退回来。

        退回靠 `funflix.base.db.get_session` 在异常时 rollback —— handler 抛的
        `HTTPException` 会一路传到那个 `except` 里。
        """
        code = await accounts.issue_invite(session, max_uses=1)
        async with _client_with(engine, Settings(registration_enabled=True)) as c:
            await c.post(
                "/api/v1/auth/register",
                json={"username": "dup", "password": "abcdef", "invite_code": code.code},
            )
            # 第一次把这张 max_uses=1 的码用掉了，所以换一张再撞名字
            second = await accounts.issue_invite(session, max_uses=1)
            resp = await c.post(
                "/api/v1/auth/register",
                json={"username": "dup", "password": "abcdef", "invite_code": second.code},
            )
            assert resp.status_code == 409

            ok = await c.post(
                "/api/v1/auth/register",
                json={"username": "fresh", "password": "abcdef", "invite_code": second.code},
            )
            assert ok.status_code == 201
