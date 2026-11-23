"""API 维度优化的回归测试：
- GET /works/{id} 消除多季 N+1 查询（窗口函数单条 SQL）
- GET /works sort 排序参数与 valid_only + provider 联合过滤
- GET /stats TTL 缓存 + ?refresh=true 穿透 + work_total/work_by_type + GET /stats/providers
- 异步运维任务 POST /jobs/... (202) 与 GET /jobs 轮询
"""

from __future__ import annotations

import asyncio

import pytest
from funflix.base.enums import CheckStatus, MediaType, Provider, Quality
from funflix.models import Media, Resource, Work, utcnow
from funflix.services.counters import refresh_media_counters, refresh_work_counters
from sqlalchemy import event

from funflix_api.app import create_app


def _resource(share_id: str, *, provider=Provider.QUARK, status=CheckStatus.VALID) -> Resource:
    now = utcnow()
    return Resource(
        provider=provider,
        share_id=share_id,
        url=f"https://pan.quark.cn/s/{share_id}",
        quality=Quality.FHD_1080P,
        check_status=status,
        first_seen_at=now,
        last_seen_at=now,
    )


@pytest.mark.asyncio
class TestApiOptimizations:
    async def test_app_version_not_hardcoded_010(self) -> None:
        app = create_app()
        assert app.version != "0.1.0"

    async def test_get_work_multi_season_uses_constant_queries(
        self, guest_client, session, engine
    ) -> None:
        work = Work(
            title="长篇多季剧",
            norm_key="长篇多季剧",
            media_type=MediaType.TV,
            year=2024,
            aliases=[],
        )
        session.add(work)
        await session.flush()

        medias = []
        for s in range(1, 9):
            m = Media(
                work_id=work.id,
                season=s,
                title=f"长篇多季剧 第{s}季",
                norm_key="长篇多季剧",
                media_type=MediaType.TV,
                year=2024,
                aliases=[],
            )
            m.resources = [_resource(f"ms{s}r1"), _resource(f"ms{s}r2", status=CheckStatus.INVALID)]
            medias.append(m)
        session.add_all(medias)
        await session.commit()
        await refresh_media_counters(session, [m.id for m in medias])
        await session.commit()
        await refresh_work_counters(session, [work.id])
        await session.commit()

        queries: list[str] = []

        def _record(conn, cursor, statement, params, context, executemany) -> None:
            queries.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", _record)
        try:
            resp = await guest_client.get(f"/api/v1/works/{work.id}")
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _record)

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["seasons"]) == 8
        assert all(len(s["resources"]) == 2 for s in body["seasons"])
        # 鉴权 user + work + selectin seasons + 单条窗口函数 resources + tags，共 5 条左右，
        # 绝不会随 8 个季膨胀到 12+ 条。
        assert len(queries) <= 6, f"多季作品详情查询过多（{len(queries)} 条）：{queries}"

    async def test_stats_ttl_cache_and_provider_endpoint(self, admin_client, session) -> None:
        r1 = _resource("st_a")
        session.add(r1)
        await session.commit()

        first = (await admin_client.get("/api/v1/stats")).json()
        assert first["resource_total"] == 1
        assert "work_total" in first
        assert "work_by_type" in first

        # 新增一条资源：TTL 缓存期内直接读缓存，传 refresh=true 立即看到最新值
        session.add(_resource("st_b", provider=Provider.ALIPAN, status=CheckStatus.INVALID))
        await session.commit()

        cached = (await admin_client.get("/api/v1/stats")).json()
        assert cached["resource_total"] == 1

        fresh = (await admin_client.get("/api/v1/stats", params={"refresh": "true"})).json()
        assert fresh["resource_total"] == 2

        prov = (
            await admin_client.get("/api/v1/stats/providers", params={"refresh": "true"})
        ).json()
        assert prov["quark"]["valid"] == 1
        assert prov["alipan"]["invalid"] == 1

    async def test_async_job_endpoint_returns_202_and_completes(
        self, admin_client, monkeypatch
    ) -> None:
        from contextlib import asynccontextmanager

        from funflix.worker.tasks import BatchReport

        @asynccontextmanager
        async def _fake_scope():
            yield None

        async def _fake_verify(_session, **kwargs):
            return BatchReport(claimed=3, succeeded=3, failed=0)

        monkeypatch.setattr("funflix_api.v1.jobs.session_scope", _fake_scope)
        monkeypatch.setattr("funflix_api.v1.jobs.run_verify_once", _fake_verify)

        resp = await admin_client.post("/api/v1/jobs/providers/quark/verify")
        assert resp.status_code == 202
        job = resp.json()
        assert job["kind"] == "verify"
        assert job["target"] == "quark"

        for _ in range(20):
            polled = (await admin_client.get(f"/api/v1/jobs/{job['id']}")).json()
            if polled["status"] in ("succeeded", "failed"):
                break
            await asyncio.sleep(0.02)

        assert polled["status"] == "succeeded"
        assert "校验 3 条" in polled["summary"]

    async def test_observability_and_security_headers(self, guest_client) -> None:
        resp = await guest_client.get("/healthz")
        assert resp.status_code == 200
        assert resp.headers.get("X-Request-ID")
        assert resp.headers.get("X-Process-Time", "").endswith("ms")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"

    async def test_resource_detail_includes_check_history_and_recheck(
        self, admin_client, session, monkeypatch
    ) -> None:
        from funflix.models import LinkCheck
        from funflix.services.verify.base import CheckOutcome

        res = _resource("hist123", status=CheckStatus.UNCHECKED)
        session.add(res)
        session.add(
            LinkCheck(
                provider=Provider.QUARK,
                share_id="hist123",
                url=res.url,
                checked_at=utcnow(),
                status=CheckStatus.VALID,
                http_code=200,
                probe="quark-test",
                latency_ms=42,
            )
        )
        await session.commit()

        detail = (await admin_client.get(f"/api/v1/resources/{res.id}")).json()
        assert len(detail["recent_checks"]) == 1
        assert detail["recent_checks"][0]["probe"] == "quark-test"

        async def _fake_check(sess, resource, **kwargs):
            from funflix.services.verify.runner import persist_check_outcome

            return await persist_check_outcome(
                sess,
                resource,
                CheckOutcome(status=CheckStatus.INVALID, http_code=404, detail="已失效"),
                probe_name="stub-recheck",
            )

        monkeypatch.setattr("funflix_api.v1.resources.check_resource", _fake_check)
        updated = (await admin_client.post(f"/api/v1/resources/{res.id}/recheck")).json()
        assert updated["check_status"] == "invalid"
        assert len(updated["recent_checks"]) == 2

    async def test_raw_reparse_and_source_name_search(self, admin_client) -> None:
        created = (
            await admin_client.post(
                "/api/v1/raw",
                json={
                    "content": "流浪地球2 2023 4K\nhttps://pan.quark.cn/s/abc123def",
                    "source_type": "manual",
                    "source_name": "测试频道Alpha",
                },
            )
        ).json()

        listed = (await admin_client.get("/api/v1/raw", params={"source_name": "Alpha"})).json()
        assert listed["total"] == 1

        reparsed = (await admin_client.post(f"/api/v1/raw/{created['id']}/reparse")).json()
        assert reparsed["parse_status"] in ("done", "skipped")

    async def test_works_quality_tag_filters_and_popular_tags(self, guest_client, session) -> None:
        from funflix.models import Tag, TagKind, media_tag

        w4k = Work(
            title="四K悬疑片",
            norm_key="四k悬疑片",
            media_type=MediaType.MOVIE,
            year=2025,
            aliases=[],
        )
        w720 = Work(
            title="标清喜剧片",
            norm_key="标清喜剧片",
            media_type=MediaType.MOVIE,
            year=2025,
            aliases=[],
        )
        t_suspense = Tag(kind=TagKind.GENRE, name="悬疑", norm_key="悬疑", media_count=1)
        session.add_all([w4k, w720, t_suspense])
        await session.flush()

        m4k = Media(
            work_id=w4k.id,
            season=0,
            title="四K悬疑片",
            norm_key="四k悬疑片",
            media_type=MediaType.MOVIE,
            year=2025,
            aliases=[],
        )
        r4k = _resource("q4k1")
        r4k.quality = Quality.UHD_4K
        m4k.resources = [r4k]

        m720 = Media(
            work_id=w720.id,
            season=0,
            title="标清喜剧片",
            norm_key="标清喜剧片",
            media_type=MediaType.MOVIE,
            year=2025,
            aliases=[],
        )
        r720 = _resource("q7201")
        r720.quality = Quality.HD_720P
        m720.resources = [r720]

        session.add_all([m4k, m720])
        await session.flush()
        await session.execute(
            media_tag.insert().values(media_id=m4k.id, tag_id=t_suspense.id, created_at=utcnow())
        )
        await session.commit()

        by_4k = (await guest_client.get("/api/v1/works", params={"quality": "4k"})).json()
        assert [i["title"] for i in by_4k["items"]] == ["四K悬疑片"]

        by_tag = (await guest_client.get("/api/v1/works", params={"tag": "悬疑"})).json()
        assert [i["title"] for i in by_tag["items"]] == ["四K悬疑片"]

        pop = (await guest_client.get("/api/v1/works/tags")).json()
        assert [t["name"] for t in pop] == ["悬疑"]

    async def test_detail_rate_limiter_returns_429(self, guest_client) -> None:
        from funflix_api.app import _SlidingWindowLimiter

        # 直接在当前 app 上将阈值压到 2 次/分，验证超过后返回 429 + Retry-After
        app = guest_client._transport.app
        app.state.detail_rate_limiter = _SlidingWindowLimiter(limit_per_minute=2)
        missing = "/api/v1/works/00000000-0000-0000-0000-000000000000"
        assert (await guest_client.get(missing)).status_code == 404
        assert (await guest_client.get(missing)).status_code == 404
        blocked = await guest_client.get(missing)
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) >= 1

    async def test_resources_search_quality_and_orphan_filter(self, admin_client, session) -> None:
        work = Work(
            title="关联测试剧",
            norm_key="关联测试剧",
            media_type=MediaType.TV,
            year=2025,
            aliases=[],
        )
        session.add(work)
        await session.flush()
        media = Media(
            work_id=work.id,
            season=1,
            title="关联测试剧 第1季",
            norm_key="关联测试剧",
            media_type=MediaType.TV,
            year=2025,
            aliases=[],
        )
        attached = _resource("att4k")
        attached.quality = Quality.UHD_4K
        attached.title_raw = "关联剧4K合集"
        media.resources = [attached]

        orphan = _resource("orph720")
        orphan.quality = Quality.HD_720P
        orphan.title_raw = "未归属冷门资源"
        session.add_all([media, orphan])
        await session.commit()

        by_q = (await admin_client.get("/api/v1/resources", params={"q": "冷门"})).json()
        assert [r["share_id"] if "share_id" in r else r["url"] for r in by_q["items"]] == [
            orphan.url
        ]

        by_quality = (await admin_client.get("/api/v1/resources", params={"quality": "4k"})).json()
        assert [r["url"] for r in by_quality["items"]] == [attached.url]

        by_orphan = (
            await admin_client.get("/api/v1/resources", params={"orphan_only": "true"})
        ).json()
        assert [r["url"] for r in by_orphan["items"]] == [orphan.url]

    async def test_admin_jobs_clear_finished_and_invalidate_callback(self, admin_client) -> None:
        from funflix_api.v1.jobs import AdminJobManager

        invalidated = 0

        def _on_done() -> None:
            nonlocal invalidated
            invalidated += 1

        mgr = AdminJobManager(on_complete=_on_done)
        admin_client._transport.app.state.admin_jobs = mgr

        async def _ok() -> str:
            return "done"

        job = mgr.submit(kind="verify", target="quark", target_label="夸克", runner=_ok)
        await asyncio.sleep(0.05)
        assert job.status == "succeeded"
        assert job.target_label == "夸克"
        assert invalidated == 1

        cleared = (await admin_client.delete("/api/v1/jobs")).json()
        assert cleared == {"cleared": 1}
        assert (await admin_client.get("/api/v1/jobs")).json() == []

    async def test_related_works_and_provider_limiter_status(self, admin_client, session) -> None:
        from funflix.models import Tag, TagKind, media_tag

        w1 = Work(
            title="主剧",
            norm_key="主剧",
            media_type=MediaType.TV,
            year=2025,
            aliases=[],
            resource_count=2,
            valid_resource_count=2,
        )
        w2 = Work(
            title="同标签剧",
            norm_key="同标签剧",
            media_type=MediaType.TV,
            year=2025,
            aliases=[],
            resource_count=1,
            valid_resource_count=1,
        )
        w3 = Work(
            title="同类型兜底剧",
            norm_key="同类型兜底剧",
            media_type=MediaType.TV,
            year=2024,
            aliases=[],
            resource_count=5,
            valid_resource_count=3,
        )
        t_sci = Tag(kind=TagKind.GENRE, name="科幻", norm_key="科幻", media_count=2)
        session.add_all([w1, w2, w3, t_sci])
        await session.flush()

        m1 = Media(
            work_id=w1.id,
            season=1,
            title="主剧 第1季",
            norm_key="主剧",
            media_type=MediaType.TV,
            year=2025,
            aliases=[],
        )
        m2 = Media(
            work_id=w2.id,
            season=1,
            title="同标签剧 第1季",
            norm_key="同标签剧",
            media_type=MediaType.TV,
            year=2025,
            aliases=[],
        )
        session.add_all([m1, m2])
        await session.flush()
        for mid in (m1.id, m2.id):
            await session.execute(
                media_tag.insert().values(media_id=mid, tag_id=t_sci.id, created_at=utcnow())
            )
        await session.commit()

        related = (await admin_client.get(f"/api/v1/works/{w1.id}/related")).json()
        assert [item["title"] for item in related] == ["同标签剧", "同类型兜底剧"]

        status_resp = (await admin_client.get("/api/v1/resources/providers/status")).json()
        assert "quark" in status_resp
        assert status_resp["quark"]["factor"] >= 1.0
        assert status_resp["quark"]["circuit_remaining"] >= 0.0

    async def test_gzip_middleware_compresses_large_json(self, admin_client) -> None:
        resp = await admin_client.get(
            "/openapi.json",
            headers={"Accept-Encoding": "gzip"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("content-encoding") == "gzip"
