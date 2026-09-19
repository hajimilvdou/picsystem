"""冒烟测试：ASGI 级别走一遍核心流程（不依赖上游服务）。

自包含：独立的库文件与数据目录，并屏蔽 .env 里的真实配置
（os.environ 优先级高于 pydantic-settings 的 .env，且必须在导入 app 前设置）。
"""
import asyncio
import os
import shutil
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./data-smoke/smoke.db"
os.environ["DATA_DIR"] = "./data-smoke"
os.environ["UPSTREAM_BASE_URL"] = "http://127.0.0.1:9"
os.environ["UPSTREAM_API_KEY"] = "smoke-dummy-key"
os.environ["JWT_SECRET"] = "smoke-test-secret-0123456789abcdef"
shutil.rmtree("./data-smoke", ignore_errors=True)

import httpx  # noqa: E402

from app.main import app  # noqa: E402,F401


async def main() -> None:
    from app.database import init_db
    await init_db()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 健康检查
        r = await client.get("/api/health")
        assert r.status_code == 200, r.text
        print("✓ /api/health")

        # 登录失败（未配置 ADMIN_PASSWORD 时生成的是随机密码，先造一个管理员）
        from app.database import SessionLocal
        from app.models import User
        from app.security import hash_password
        from sqlalchemy import select
        async with SessionLocal() as s:
            admin = (await s.execute(select(User).where(User.role == "admin"))).scalars().first()
            admin.password_hash = hash_password("admin-pass-123")
            await s.commit()

        # 登录需要 x-requested-with 头（CSRF）
        r = await client.post("/api/auth/login", json={"username": "admin", "password": "admin-pass-123"})
        assert r.status_code == 403, f"CSRF 应拦截: {r.status_code}"
        print("✓ CSRF 头校验生效")

        r = await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-pass-123"},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        assert r.status_code == 200, r.text
        cookies = r.cookies
        print("✓ 管理员登录")

        # me
        r = await client.get("/api/auth/me", cookies=cookies)
        assert r.status_code == 200 and r.json()["role"] == "admin", r.text
        print("✓ /api/auth/me:", r.json()["username"], r.json()["quotas"])

        # 创建邀请码
        r = await client.post(
            "/api/admin/invites",
            json={"max_uses": 5, "chat_quota": 100, "image_quota": 10, "search_quota": 50, "ppt_quota": 3, "note": "测试"},
            headers={"x-requested-with": "XMLHttpRequest"},
            cookies=cookies,
        )
        assert r.status_code == 200, r.text
        code = r.json()["code"]
        print("✓ 创建邀请码:", code, r.json()["quotas"])

        # 注册
        r = await client.post(
            "/api/auth/register",
            json={"username": "tester", "password": "tester-pass-1", "invite_code": code, "fp": "fp-tester"},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        assert r.status_code == 200, r.text
        user_cookies = r.cookies
        print("✓ 邀请码注册 tester")

        # 错误邀请码
        r = await client.post(
            "/api/auth/register",
            json={"username": "tester2", "password": "tester-pass-1", "invite_code": "WRONG-CODE", "fp": "fp-t2"},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        assert r.status_code == 400, r.text
        print("✓ 无效邀请码被拒绝")

        # 缺少设备指纹
        r = await client.post(
            "/api/auth/register",
            json={"invite_code": code},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        assert r.status_code == 400, r.text
        print("✓ 缺少设备指纹被拒绝")

        # 注册备注自动带入管理员备注，管理端可修改
        r = await client.post(
            "/api/auth/register",
            json={"username": "noted1", "password": "noted-pass-1", "invite_code": code, "fp": "fp-noted", "note": "约定暗号：梧桐"},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        assert r.status_code == 200, r.text
        r = await client.get("/api/admin/users?q=noted1", cookies=cookies)
        noted = r.json()["items"][0]
        assert noted["note"] == "约定暗号：梧桐" and noted["reg_note"] == "约定暗号：梧桐", noted
        r = await client.patch(
            f"/api/admin/users/{noted['id']}", json={"note": "管理员改过的备注"},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )
        assert r.status_code == 200 and r.json()["note"] == "管理员改过的备注", r.text
        print("✓ 用户备注：注册带入 + 管理端修改")

        # 协议后端强制：未同意协议访问业务端点 → 428
        r = await client.get("/api/chat/conversations", cookies=user_cookies)
        assert r.status_code == 428, r.status_code
        r = await client.post(
            "/api/auth/agreement/accept", headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies
        )
        assert r.status_code == 200, r.text
        r = await client.get("/api/chat/conversations", cookies=user_cookies)
        assert r.status_code == 200, r.status_code
        print("✓ 协议未同意时业务端点返回 428，同意后放行")

        # me / 额度
        r = await client.get("/api/auth/me", cookies=user_cookies)
        q = r.json()["quotas"]
        assert q["chat"]["total"] == 100 and q["image"]["total"] == 10, q
        print("✓ 用户额度来自邀请码:", q)

        # 用户无权访问管理端
        r = await client.get("/api/admin/overview", cookies=user_cookies)
        assert r.status_code == 403, r.status_code
        print("✓ 普通用户访问管理端被拒绝")

        # 创建 API 密钥
        r = await client.post(
            "/api/keys", json={"name": "k1"},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 200, r.text
        sk = r.json()["key"]
        print("✓ 创建用户密钥:", sk[:12], "...")

        # /v1 无上游 → 502 且为 OpenAI 错误格式
        r = await client.get("/v1/models", headers={"Authorization": f"Bearer {sk}"})
        assert r.status_code in (500, 502) and "error" in r.json(), (r.status_code, r.text)
        print("✓ /v1/models 无上游时返回 OpenAI 风格错误:", r.status_code)

        # /v1 图生图：图片字段别名都要收下（OpenAI SDK 传 image[]，脚本常传重复的 image）
        # 收到并转发后失败于上游不可达（502），而不是入参错误（400）
        for alias in ("image", "image[]", "images", "images[]"):
            r = await client.post(
                "/v1/images/edits",
                data={"prompt": "把背景换成海边"},
                files=[(alias, ("ref.png", b"ref-bytes", "image/png"))],
                headers={"Authorization": f"Bearer {sk}"},
            )
            assert r.status_code not in (400, 422), (alias, r.status_code, r.text)
        r = await client.post(
            "/v1/images/edits",
            data={"prompt": "只重绘涂抹区域"},
            files=[
                ("image[]", ("ref.png", b"ref-bytes", "image/png")),
                ("mask[]", ("mask.png", b"mask-bytes", "image/png")),
            ],
            headers={"Authorization": f"Bearer {sk}"},
        )
        assert r.status_code not in (400, 422), (r.status_code, r.text)
        print("✓ /v1 图生图接受 image / image[] / images / images[] 与 mask / mask[] 别名")

        # 管理员设置读取 + 上游测试接口（无上游 → ok=false）
        r = await client.get("/api/admin/settings", cookies=cookies)
        assert r.status_code == 200, r.text
        print("✓ 管理端设置读取")
        r = await client.post(
            "/api/admin/upstream/test", json={},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )
        assert r.status_code == 200 and r.json()["ok"] is False, r.text
        print("✓ 上游连通性测试接口:", r.json())

        # 功能开关：关闭绘图后用户请求被拦截
        r = await client.patch(
            "/api/admin/settings", json={"feature_image_enabled": False},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )
        assert r.status_code == 200 and r.json()["feature_image_enabled"] is False, r.text
        r = await client.post(
            "/api/images/generations", json={"prompt": "猫"},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 403 and "关闭" in r.json()["detail"], r.text
        print("✓ 功能开关生效（绘图已关闭）")

        # 局部编辑遮罩（mask）：上传校验发生在功能开关之前，所以这里可以直接验证；
        # 合法遮罩会一路走到 require_feature 才被 403 拦下，说明 mask 已被接受
        r = await client.post(
            "/api/images/edits",
            data={"prompt": "把背景换成海边"},
            files=[
                ("images", ("ref.png", b"ref", "image/png")),
                ("mask", ("mask.png", b"mask", "image/png")),
            ],
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 403 and "关闭" in r.json()["detail"], r.text

        r = await client.post(
            "/api/images/edits",
            data={"prompt": "x"},
            files=[
                ("images", ("ref.png", b"ref", "image/png")),
                ("mask", ("m1.png", b"m", "image/png")),
                ("mask", ("m2.png", b"m", "image/png")),
            ],
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 400 and "遮罩只能上传一张" in r.json()["detail"], r.text

        r = await client.post(
            "/api/images/edits",
            data={"prompt": "x"},
            files=[
                ("images", ("ref.png", b"ref", "image/png")),
                ("mask", ("m.txt", b"m", "text/plain")),
            ],
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 400 and "遮罩" in r.json()["detail"], r.text

        r = await client.post(
            "/api/images/edits",
            data={"prompt": "x"},
            files=[("mask", ("m.png", b"m", "image/png"))],
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 400 and "参考图" in r.json()["detail"], r.text
        for alias in ("image", "image[]", "images", "images[]"):
            r = await client.post(
                "/api/images/edits",
                data={"prompt": "x"},
                files=[(alias, ("ref.png", b"ref", "image/png"))],
                headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
            )
            assert r.status_code == 403 and "关闭" in r.json()["detail"], (alias, r.status_code, r.text)
        print("✓ 局部编辑遮罩：单张放行 + 多张/非图片/缺参考图被拦截 + 参考图字段别名")

        # 注册开关：匿名公开配置同步，关闭后注册被拒
        r = await client.get("/api/auth/public-config")
        assert r.status_code == 200 and r.json()["registration_enabled"] is True, r.text
        r = await client.patch(
            "/api/admin/settings", json={"registration_enabled": False},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )
        assert r.status_code == 200 and r.json()["registration_enabled"] is False, r.text
        r = await client.get("/api/auth/public-config")
        assert r.json()["registration_enabled"] is False, r.text
        r = await client.post(
            "/api/auth/register",
            json={"username": "blocked1", "password": "blocked-pass-1", "invite_code": code, "fp": "fp-blocked"},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        assert r.status_code == 403 and "关闭" in r.json()["detail"], r.text
        r = await client.patch(
            "/api/admin/settings", json={"registration_enabled": True},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )
        assert r.json()["registration_enabled"] is True, r.text
        print("✓ 注册开关：关闭后公开配置同步且注册被拒")

        # 设置接口下发内置默认值（拦截词库 / 免责协议），供管理端二次修改
        r = await client.get("/api/admin/settings", cookies=cookies)
        s = r.json()
        assert "法轮功" in s["content_filter_keywords_default"], s["content_filter_keywords_default"]
        assert "免责声明" in s["agreement_text_default"], s["agreement_text_default"][:80]
        print("✓ 设置接口下发内置拦截词库与免责协议默认值")

        # 对话：指到不可达上游 → SSE error 事件 + 额度退还
        r = await client.patch(
            "/api/admin/settings",
            json={"upstream_base_url": "http://127.0.0.1:9", "upstream_api_key": "dummy-key"},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )
        assert r.status_code == 200, r.text
        r = await client.get("/api/auth/me", cookies=user_cookies)
        before = r.json()["quotas"]["chat"]
        r = await client.post(
            "/api/chat/completions", json={"message": "你好"},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 200, r.text
        assert "event: error" in r.text, r.text[:300]
        r = await client.get("/api/auth/me", cookies=user_cookies)
        after = r.json()["quotas"]["chat"]
        assert after["used"] == before["used"], (before, after)
        print("✓ 对话上游失败时退还额度")

        # 管理端日志与总览
        r = await client.get("/api/admin/logs", cookies=cookies)
        assert r.status_code == 200 and r.json()["total"] >= 1, r.text
        print("✓ 管理端调用日志:", r.json()["total"], "条")
        r = await client.get("/api/admin/overview", cookies=cookies)
        assert r.status_code == 200, r.text
        print("✓ 管理端总览:", {k: r.json()[k] for k in ("total_users", "total_requests", "today_requests")})

        # ---- 风控 ----
        # 连续失败触发登录锁定
        for _ in range(5):
            r = await client.post(
                "/api/auth/login",
                json={"username": "tester", "password": "wrong-pass-1"},
                headers={"x-requested-with": "XMLHttpRequest"},
            )
            assert r.status_code == 401, r.text
        r = await client.post(
            "/api/auth/login",
            json={"username": "tester", "password": "tester-pass-1"},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        assert r.status_code == 429, r.text
        print("✓ 登录连续失败触发临时锁定")

        # 管理员解锁
        r = await client.get("/api/admin/risk/locks", cookies=cookies)
        locks = r.json()["items"]
        assert locks and locks[0]["username"] == "tester", locks
        r = await client.post(
            "/api/admin/risk/unlock", json={"username": "tester", "ip": locks[0]["ip"]},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )
        assert r.status_code == 200, r.text
        r = await client.post(
            "/api/auth/login",
            json={"username": "tester", "password": "tester-pass-1"},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        assert r.status_code == 200, r.text
        print("✓ 管理员解锁后可正常登录")

        # 用户级限流
        r = await client.patch(
            "/api/admin/settings", json={"user_rate_limit_per_minute": 1},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )
        assert r.status_code == 200, r.text
        await client.post(
            "/api/chat/completions", json={"message": "hi"},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        r = await client.post(
            "/api/chat/completions", json={"message": "hi"},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 429, r.text
        print("✓ 单用户限流生效")
        # 恢复阈值，避免影响后续用例
        await client.patch(
            "/api/admin/settings", json={"user_rate_limit_per_minute": 20},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )

        # 风控事件已记录
        r = await client.get("/api/admin/risk/events", cookies=cookies)
        kinds = {e["kind"] for e in r.json()["items"]}
        assert {"login_fail", "login_locked", "rate_limit", "register"} <= kinds, kinds
        print("✓ 风控事件记录:", sorted(kinds))

        # ---- 存储配额与自动清理 ----
        from datetime import datetime, timedelta, timezone
        from app.database import SessionLocal
        from app.models import StoredFile, UsageLog
        from app.services.cleanup import cleanup_files, cleanup_logs, cleanup_orphan_files
        from app.services.storage import StorageFullError, ensure_storage_capacity, user_storage_bytes
        from sqlalchemy import select

        async with SessionLocal() as s:
            # 设全局默认上限 1MB
            from app.services.settings_store import set_settings
            await set_settings(s, {"storage_quota_mb_default": 1})
            await s.commit()
            tester = (await s.execute(select(User).where(User.username == "tester"))).scalar_one()
            # 写入一个 2MB 的产物记录（占满配额）
            big = StoredFile(user_id=tester.id, kind="image", filename="big.png",
                             path="files/test/big.png", mime="image/png", size=2 * 1024 * 1024)
            s.add(big)
            await s.commit()
            used = await user_storage_bytes(s, tester.id)
            assert used == 2 * 1024 * 1024, used
            try:
                await ensure_storage_capacity(s, tester, 1024)
                raise AssertionError("应抛出 StorageFullError")
            except StorageFullError:
                pass
            print("✓ 存储配额超限被拦截")

            # 个人覆盖为 10MB 后放行
            tester.storage_limit_mb = 10
            await s.commit()
            await ensure_storage_capacity(s, tester, 1024)
            print("✓ 管理员个人覆盖存储上限生效")

            # 过期产物清理
            old = StoredFile(user_id=tester.id, kind="image", filename="old.png",
                             path="files/test/old.png", mime="image/png", size=10,
                             created_at=datetime.now(timezone.utc) - timedelta(hours=48))
            s.add(old)
            await s.commit()
            removed = await cleanup_files(s, 24)  # 保留 24 小时
            assert removed == 1, removed
            remaining = await user_storage_bytes(s, tester.id)
            assert remaining == 2 * 1024 * 1024, remaining
            print("✓ 24 小时前的产物被自动清理")

            # 旧日志清理
            s.add(UsageLog(user_id=tester.id, feature="chat", endpoint="/x", status="success",
                           created_at=datetime.now(timezone.utc) - timedelta(hours=200)))
            await s.commit()
            stats = await cleanup_logs(s, 168, 90)
            assert stats["usage_logs"] == 1, stats
            print("✓ 超期日志被自动清理:", stats)

            # 孤儿文件清理（宽限期 2 小时，测试文件标记为 3 小时前）
            import os
            from app.services.storage import data_root
            orphan = data_root() / "files" / "test" / "orphan.png"
            orphan.parent.mkdir(parents=True, exist_ok=True)
            orphan.write_bytes(b"x")
            old_ts = (datetime.now() - timedelta(hours=3)).timestamp()
            os.utime(orphan, (old_ts, old_ts))
            orphans = await cleanup_orphan_files(s)
            assert orphans == 1 and not orphan.exists(), orphans
            print("✓ 孤儿文件被清理")

            # 宽限期内的在途文件不被误删
            inflight = data_root() / "files" / "test" / "inflight.png"
            inflight.parent.mkdir(parents=True, exist_ok=True)
            inflight.write_bytes(b"x")
            orphans2 = await cleanup_orphan_files(s)
            assert orphans2 == 0 and inflight.exists(), orphans2
            inflight.unlink()
            print("✓ 在途文件宽限期保护生效")

        # ---- 图库标签 ----
        async with SessionLocal() as s:
            tester = (await s.execute(select(User).where(User.username == "tester"))).scalar_one()
            samples = [
                StoredFile(user_id=tester.id, kind="image", filename=f"tag{i}.png",
                           path=f"files/test/tag{i}.png", mime="image/png", size=10)
                for i in range(3)
            ]
            s.add_all(samples)
            await s.commit()
            sample_ids = [row.id for row in samples]

        # 设置标签：去重、剔除逗号等非法字符、超长截断
        r = await client.patch(
            f"/api/files/{sample_ids[0]}",
            json={"tags": ["风景", "猫", "风景", " bad,tag ", "x" * 40]},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 200, r.text
        tags = r.json()["tags"]
        assert len(tags) == 4 and tags[0] == "风景" and "猫" in tags and "badtag" in tags, tags
        assert all(len(t) <= 24 for t in tags), tags

        # 超出上限只保留前 10 个
        r = await client.patch(
            f"/api/files/{sample_ids[1]}",
            json={"tags": [f"t{i}" for i in range(20)]},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 200 and len(r.json()["tags"]) == 10, r.text

        # 列表带出标签；按标签筛选是精确匹配（前缀不命中）
        r = await client.get("/api/files", params={"kind": "image"}, cookies=user_cookies)
        rows = {item["id"]: item for item in r.json()["items"]}
        assert rows[sample_ids[0]]["tags"] == tags, rows[sample_ids[0]]
        r = await client.get("/api/files", params={"kind": "image", "tag": "猫"}, cookies=user_cookies)
        data = r.json()
        assert data["total"] == 1 and data["items"][0]["id"] == sample_ids[0], data
        r = await client.get("/api/files", params={"kind": "image", "tag": "风"}, cookies=user_cookies)
        assert r.json()["total"] == 0, r.json()

        # 标签聚合
        r = await client.get("/api/files/tags", params={"kind": "image"}, cookies=user_cookies)
        agg = {item["tag"]: item["count"] for item in r.json()["items"]}
        assert agg.get("猫") == 1 and agg.get("风景") == 1, agg

        # 越权：管理员 cookie 也改不了别人（tester）的文件
        r = await client.patch(
            f"/api/files/{sample_ids[0]}", json={"tags": []},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )
        assert r.status_code == 404, r.text

        # 清空后不再被筛出
        r = await client.patch(
            f"/api/files/{sample_ids[0]}", json={"tags": []},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 200 and r.json()["tags"] == [], r.text
        r = await client.get("/api/files", params={"kind": "image", "tag": "猫"}, cookies=user_cookies)
        assert r.json()["total"] == 0, r.json()
        print("✓ 图库标签：清洗/去重/限量、精确筛选、聚合、越权拦截、清空")

        # ---- 图片缩略图 ----
        from PIL import Image as PILImage

        from app.services.cleanup import cleanup_orphan_thumbs
        from app.services.storage import data_root as file_data_root
        from app.services.thumbnails import THUMB_EDGE, thumb_abs_path

        big_rel = "files/test/big-photo.png"
        big_abs = file_data_root() / big_rel
        big_abs.parent.mkdir(parents=True, exist_ok=True)
        PILImage.new("RGB", (1600, 900), (20, 120, 200)).save(big_abs)  # 16:9 大图
        async with SessionLocal() as s:
            tester = (await s.execute(select(User).where(User.username == "tester"))).scalar_one()
            big_row = StoredFile(user_id=tester.id, kind="image", filename="big-photo.png",
                                 path=big_rel, mime="image/png", size=big_abs.stat().st_size)
            s.add(big_row)
            await s.commit()
            big_id = big_row.id

        r = await client.get("/api/files", params={"kind": "image"}, cookies=user_cookies)
        target = next(item for item in r.json()["items"] if item["id"] == big_id)
        assert target["thumb_url"].startswith(f"/api/files/{big_id}/thumb?v="), target
        # 缓存键必须跟随文件体积：压缩替换后浏览器才会重新拉缩略图
        assert target["thumb_url"].split("?v=")[-1] == str(target["size"]), target

        # 按需生成并落盘：长边 400、JPEG、保持比例
        thumb_file = thumb_abs_path(big_rel)
        assert not thumb_file.exists(), "缩略图应当是首次请求时才生成"
        r = await client.get(f"/api/files/{big_id}/thumb", cookies=user_cookies)
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg", r.text
        assert thumb_file.exists(), thumb_file
        with PILImage.open(thumb_file) as im:
            assert max(im.size) == THUMB_EDGE and im.size == (400, 225), im.size
        # 不能落在 files/ 下，否则会被孤儿文件清理误删
        assert (file_data_root() / "files") not in thumb_file.parents, thumb_file

        # 解码像素上限守卫：把上限调到 10 像素，任何真实图片都应被拒（防解压炸弹）
        from app.services import thumbnails as thumb_module

        guard_rel = "files/test/guard.png"
        guard_abs = file_data_root() / guard_rel
        PILImage.new("RGB", (40, 40), (1, 2, 3)).save(guard_abs)
        original_limit = thumb_module.MAX_DECODE_PIXELS
        thumb_module.MAX_DECODE_PIXELS = 10
        try:
            assert thumb_module.ensure_thumbnail(guard_abs, guard_rel) is None, "像素守卫未生效"
            assert not thumb_abs_path(guard_rel).exists()
        finally:
            thumb_module.MAX_DECODE_PIXELS = original_limit
        # 恢复上限后能正常生成
        assert thumb_module.ensure_thumbnail(guard_abs, guard_rel) is not None

        # 越权访问拿不到缩略图
        r = await client.get(f"/api/files/{big_id}/thumb", cookies=cookies)
        assert r.status_code == 404, r.text

        # 删除产物时缩略图必须一起消失
        r = await client.delete(
            f"/api/files/{big_id}",
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 200, r.text
        assert not thumb_file.exists(), "删除产物后缩略图仍残留"

        # 孤儿缩略图（含生成中断的 .tmp）兜底清理
        stale = thumb_abs_path("files/test/ghost.png")
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"stale")
        (stale.parent / "half.jpg.tmp").write_bytes(b"half")
        async with SessionLocal() as s:
            removed_thumbs = await cleanup_orphan_thumbs(s)
        assert removed_thumbs >= 2 and not stale.exists(), removed_thumbs
        assert not (stale.parent / "half.jpg.tmp").exists()
        print("✓ 图片缩略图：按需生成 400px JPEG、越权拦截、随产物删除、孤儿兜底清理")

        # ---- 打包下载（ZIP） ----
        import io
        import zipfile

        async with SessionLocal() as s:
            tester = (await s.execute(select(User).where(User.username == "tester"))).scalar_one()
            archived = []
            for index in range(3):
                rel = f"files/test/arch-{index}.png"
                abs_path = file_data_root() / rel
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                abs_path.write_bytes(b"PNG-" + bytes([index]) * 8)
                archived.append(
                    StoredFile(user_id=tester.id, kind="image", filename="shot.png",  # 同名，验证 zip 内去重
                               path=rel, mime="image/png", size=abs_path.stat().st_size)
                )
            s.add_all(archived)
            await s.commit()
            arch_ids = [row.id for row in archived]

        r = await client.get(
            "/api/files/archive",
            params={"ids": ",".join(str(i) for i in arch_ids)}, cookies=user_cookies,
        )
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip", r.text
        with zipfile.ZipFile(io.BytesIO(r.content)) as bundle:
            names = bundle.namelist()
            assert len(names) == 3, names
            assert "shot.png" in names and "shot (2).png" in names, names
            assert bundle.read("shot.png") == b"PNG-" + bytes([0]) * 8
            assert bundle.read("shot (2).png") == b"PNG-" + bytes([1]) * 8

        # 非法 / 空 / 超量入参
        for bad in ("abc", "", "9" * 20, ",".join(str(i) for i in range(1, 302))):
            r = await client.get("/api/files/archive", params={"ids": bad}, cookies=user_cookies)
            assert r.status_code == 400, (bad[:20], r.status_code, r.text)

        # 越权：管理员也打不了 tester 的文件
        r = await client.get("/api/files/archive", params={"ids": str(arch_ids[0])}, cookies=cookies)
        assert r.status_code == 404, r.text

        # 体积上限兜底（把 size 改大模拟超过 1GB）
        async with SessionLocal() as s:
            row = await s.get(StoredFile, arch_ids[0])
            row.size = 2 * 1024 * 1024 * 1024
            await s.commit()
        r = await client.get(
            "/api/files/archive",
            params={"ids": ",".join(str(i) for i in arch_ids)}, cookies=user_cookies,
        )
        assert r.status_code == 413, r.text
        async with SessionLocal() as s:  # 还原，避免影响后续存储统计
            row = await s.get(StoredFile, arch_ids[0])
            row.size = 14
            await s.commit()
        print("✓ 打包下载：ZIP 内容与同名去重、非法/空/超量 400、越权 404、超限 413")

        # ---- 图片压缩（干跑 → 确认 → 落盘，替换为 WebP） ----
        import random

        noise_rel = "files/test/noise.png"
        noise_abs = file_data_root() / noise_rel
        noise_abs.parent.mkdir(parents=True, exist_ok=True)
        random.seed(7)
        noise = PILImage.new("RGB", (600, 400))
        noise.putdata([(random.randrange(256), random.randrange(256), random.randrange(256))
                       for _ in range(600 * 400)])
        noise.save(noise_abs)  # 噪声 PNG 几乎不可压缩，适合验证压缩收益
        async with SessionLocal() as s:
            tester = (await s.execute(select(User).where(User.username == "tester"))).scalar_one()
            noise_row = StoredFile(user_id=tester.id, kind="image", filename="noise.png",
                                   path=noise_rel, mime="image/png", size=noise_abs.stat().st_size)
            gif_row = StoredFile(user_id=tester.id, kind="image", filename="anim.gif",
                                 path="files/test/anim.gif", mime="image/gif", size=10)
            deck_row = StoredFile(
                user_id=tester.id, kind="ppt", filename="deck.pptx", path="files/test/deck.pptx", size=10,
                mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            )
            s.add_all([noise_row, gif_row, deck_row])
            await s.commit()
            noise_id, gif_id, deck_id = noise_row.id, gif_row.id, deck_row.id
            original_png_bytes = noise_abs.stat().st_size

        # 先摸一次缩略图，用于验证压缩后旧缩略图被清理
        assert (await client.get(f"/api/files/{noise_id}/thumb", cookies=user_cookies)).status_code == 200
        thumb_before = thumb_abs_path(noise_rel)
        assert thumb_before.exists()

        # 默认 dry_run：只预估，不动原文件
        r = await client.post(
            f"/api/files/{noise_id}/compress", json={},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 200, r.text
        preview = r.json()
        assert preview["dry_run"] is True and preview["worthwhile"] is True, preview
        assert 0 < preview["after_bytes"] < preview["before_bytes"], preview
        assert noise_abs.exists() and noise_abs.stat().st_size == original_png_bytes
        async with SessionLocal() as s:
            row = await s.get(StoredFile, noise_id)
            assert row.mime == "image/png" and row.size == original_png_bytes, row.mime

        # 确认落盘
        r = await client.post(
            f"/api/files/{noise_id}/compress", json={"dry_run": False, "quality": 80},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 200 and r.json()["ok"] is True, r.text
        done = r.json()
        assert done["mime"] == "image/webp" and done["filename"].endswith(".webp"), done
        async with SessionLocal() as s:
            row = await s.get(StoredFile, noise_id)
            assert row.mime == "image/webp" and row.path.endswith(".webp"), (row.mime, row.path)
            assert row.size == done["after_bytes"] and row.size < original_png_bytes, row.size
            compressed_rel = row.path
        assert (file_data_root() / compressed_rel).exists(), "压缩后的新文件不存在"
        assert not noise_abs.exists(), "原 PNG 未清理"
        assert not thumb_before.exists(), "旧缩略图未随原文件清理"
        r = await client.get(f"/api/files/{noise_id}/download", cookies=user_cookies)
        assert r.status_code == 200 and r.headers["content-type"] == "image/webp", r.text

        # 解码像素上限守卫：上限调到 10 像素后应直接拒绝，且不动原文件
        from app.services import image_compress as compress_module
        from app.services.image_compress import CompressError

        guard2_rel = "files/test/guard2.png"
        guard2_abs = file_data_root() / guard2_rel
        PILImage.new("RGB", (40, 40), (9, 9, 9)).save(guard2_abs)
        original_pixels = compress_module.MAX_DECODE_PIXELS
        compress_module.MAX_DECODE_PIXELS = 10
        try:
            try:
                compress_module.compress(guard2_abs, quality=80)
                raise AssertionError("压缩的像素守卫未生效")
            except CompressError:
                pass
        finally:
            compress_module.MAX_DECODE_PIXELS = original_pixels
        assert guard2_abs.exists()

        # 不支持的类型 / 越权 / 参数越界
        for target_id in (gif_id, deck_id):
            r = await client.post(
                f"/api/files/{target_id}/compress", json={"dry_run": False},
                headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
            )
            assert r.status_code == 400, r.text
        r = await client.post(
            f"/api/files/{noise_id}/compress", json={"dry_run": False},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )
        assert r.status_code == 404, r.text
        r = await client.post(
            f"/api/files/{noise_id}/compress", json={"quality": 200},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 422, r.text
        print("✓ 图片压缩：默认干跑不改文件、确认后转 WebP 并清旧缩略图、类型/越权/参数校验")

        # 管理端存储统计与手动清理接口
        r = await client.get("/api/admin/storage", cookies=cookies)
        assert r.status_code == 200 and "total_bytes" in r.json(), r.text
        ov = r.json()
        # 增强字段：磁盘 / 库体积 / 分类 / 可清理预估 / 上次清理
        assert ov["disk"]["total"] > 0 and ov["disk"]["free"] >= 0, ov["disk"]
        assert ov["db_bytes"] is None or ov["db_bytes"] >= 0, ov["db_bytes"]
        assert isinstance(ov["by_kind"], list), ov["by_kind"]
        assert ov["thumbnail_bytes"] >= 0 and ov["thumbnail_files"] >= 0, ov
        cl = ov["cleanable"]
        for key in ("expired_files", "expired_bytes", "orphan_files", "orphan_bytes",
                    "old_usage_logs", "old_risk_events", "old_audit_logs", "stale_sessions"):
            assert key in cl and cl[key] >= 0, (key, cl)
        assert "last_cleanup" in ov and "cleanup_running" in ov, ov.keys()
        r = await client.post("/api/admin/storage/cleanup", headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies)
        assert r.status_code == 200 and r.json()["ok"], r.text
        print("✓ 管理端存储统计与手动清理接口")

        # 限额调低自动裁剪：保留最新文件，删除最旧超出部分
        from app.services.storage import trim_user_storage, user_storage_bytes
        from sqlalchemy import delete as sql_delete
        async with SessionLocal() as s:
            tester = (await s.execute(select(User).where(User.username == "tester"))).scalar_one()
            await s.execute(sql_delete(StoredFile).where(StoredFile.user_id == tester.id))
            await s.commit()
            MB = 1024 * 1024
            base = datetime.now(timezone.utc)
            for i, size_mb in enumerate([1, 2, 3]):  # 旧→新：1MB / 2MB / 3MB
                s.add(StoredFile(user_id=tester.id, kind="image", filename=f"f{i}.png",
                                 path=f"files/test/f{i}.png", mime="image/png", size=size_mb * MB,
                                 created_at=base + timedelta(minutes=i)))
            await s.commit()
            # 0 = 不限，不裁剪
            removed, _ = await trim_user_storage(s, tester.id, 0)
            assert removed == 0
            # 6MB 全部 ≤ 6MB 限额，不裁剪
            removed, _ = await trim_user_storage(s, tester.id, 6)
            assert removed == 0
            # 调到 5MB：删最旧的 1MB 文件，保留 3MB + 2MB
            removed, freed = await trim_user_storage(s, tester.id, 5)
            assert removed == 1 and freed == 1 * MB, (removed, freed)
            assert await user_storage_bytes(s, tester.id) == 5 * MB
            print("✓ 限额调低自动裁剪：删除最旧文件，保留最新内容")

        # seed_defaults 幂等：不覆盖管理员已改设置
        from app.services.bootstrap import seed_defaults
        from app.services.settings_store import get_setting, set_settings
        async with SessionLocal() as s:
            await set_settings(s, {"site_name": "已修改的站名", "file_retention_hours": 48})
            await s.commit()
            await seed_defaults(s)
            await s.commit()
            assert await get_setting(s, "site_name") == "已修改的站名"
            assert await get_setting(s, "file_retention_hours") == 48
            # 迁移标记已存在：管理员显式设 0 不会被默认值迁移改写
            await set_settings(s, {"storage_quota_mb_default": 0})
            await s.commit()
            await seed_defaults(s)
            await s.commit()
            assert await get_setting(s, "storage_quota_mb_default") == 0
            print("✓ seed_defaults 不覆盖管理员设置")

        # 老库默认值迁移：无标记键且值为旧默认 0 时，一次性升级到 100/24
        from app.models import Setting
        from sqlalchemy import delete as sql_delete2
        async with SessionLocal() as s:
            await set_settings(s, {"storage_quota_mb_default": 0, "file_retention_hours": 0})
            await s.execute(sql_delete2(Setting).where(Setting.key == "_defaults_migrated_v2"))
            await s.commit()
            await seed_defaults(s)
            await s.commit()
            assert await get_setting(s, "storage_quota_mb_default") == 100
            assert await get_setting(s, "file_retention_hours") == 24
            # 迁移只跑一次
            await set_settings(s, {"storage_quota_mb_default": 0})
            await s.commit()
            await seed_defaults(s)
            await s.commit()
            assert await get_setting(s, "storage_quota_mb_default") == 0
            print("✓ 老库默认值一次性迁移生效")

        # ---- 注册升级：随机凭证 + 设备指纹风控 + 设备会话 + 协议 ----
        # 恢复注册日限额（前面风控测试改成了 1）
        await client.patch(
            "/api/admin/settings", json={"register_max_per_ip_day": 10},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )
        # 随机生成账号
        r = await client.post(
            "/api/auth/register",
            json={"invite_code": code, "fp": "fp-A"},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        assert r.status_code == 200 and r.json()["generated"], r.text
        gen_user, gen_pass = r.json()["username"], r.json()["password"]
        print("✓ 随机生成账号:", gen_user)

        # 随机账号可登录（产生设备会话）
        r = await client.post(
            "/api/auth/login",
            json={"username": gen_user, "password": gen_pass},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        assert r.status_code == 200, r.text
        gen_cookies = r.cookies
        r = await client.get("/api/auth/sessions", cookies=gen_cookies)
        items = r.json()["items"]
        assert len(items) == 2 and sum(1 for s in items if s["current"]) == 1, items  # 注册+登录共两条会话
        print("✓ 登录设备会话记录（注册+登录共", len(items), "条）")

        # 设备指纹防多注册：fp-A 已注册 1 个（上限默认 2），同一邀请码不能复用
        r = await client.post(
            "/api/auth/register",
            json={"invite_code": code, "fp": "fp-A"},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        assert r.status_code == 429, r.text
        print("✓ 同一设备复用同一邀请码被拦截")

        # 换邀请码再注册 1 个到上限，第 3 个被拦截
        r = await client.post(
            "/api/admin/invites",
            json={"max_uses": 5, "chat_quota": 1, "image_quota": 0, "search_quota": 0, "ppt_quota": 0},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )
        code2 = r.json()["code"]
        r = await client.post(
            "/api/auth/register", json={"invite_code": code2, "fp": "fp-A"},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        assert r.status_code == 200, r.text
        r = await client.post(
            "/api/auth/register", json={"invite_code": code2, "fp": "fp-A"},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        assert r.status_code == 429, r.text
        print("✓ 设备指纹注册总数上限生效")

        # 免责协议：新版本默认未同意 → accept 后生效
        r = await client.get("/api/auth/me", cookies=gen_cookies)
        me = r.json()
        assert me["agreement_required"] >= 1 and me["agreement_version"] < me["agreement_required"]
        r = await client.get("/api/auth/agreement", cookies=gen_cookies)
        assert r.json()["accepted"] is False and len(r.json()["text"]) > 100
        r = await client.post("/api/auth/agreement/accept", headers={"x-requested-with": "XMLHttpRequest"}, cookies=gen_cookies)
        assert r.status_code == 200, r.text
        r = await client.get("/api/auth/agreement", cookies=gen_cookies)
        assert r.json()["accepted"] is True
        print("✓ 免责协议阅读与同意流程")

        # 用户注销指定设备：注销另一个会话（注册会话 S0）
        r = await client.get("/api/auth/sessions", cookies=gen_cookies)
        others = [s for s in r.json()["items"] if not s["current"]]
        assert others, r.json()
        r = await client.delete(f"/api/auth/sessions/{others[0]['id']}", headers={"x-requested-with": "XMLHttpRequest"}, cookies=gen_cookies)
        assert r.status_code == 200, r.text
        r = await client.get("/api/auth/sessions", cookies=gen_cookies)
        assert all(s["current"] for s in r.json()["items"])
        print("✓ 用户注销指定设备")

        # 改密后其他设备被吊销、当前设备保留
        r = await client.post(
            "/api/auth/login",
            json={"username": gen_user, "password": gen_pass},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        second_cookies = r.cookies  # 第二个“设备”
        r = await client.post(
            "/api/auth/change-password",
            json={"old_password": gen_pass, "new_password": "newpass-12345"},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=second_cookies,
        )
        assert r.status_code == 200, r.text
        second_cookies = r.cookies  # 改密响应签发了新 token（新 token_version）
        r = await client.get("/api/auth/sessions", cookies=gen_cookies)
        assert r.status_code == 401  # 第一个会话已被吊销
        r = await client.get("/api/auth/sessions", cookies=second_cookies)
        assert r.status_code == 200  # 当前会话保留
        print("✓ 改密后其他设备会话被吊销")

        # 登出后旧 cookie 立即失效
        r = await client.post("/api/auth/logout", headers={"x-requested-with": "XMLHttpRequest"}, cookies=second_cookies)
        assert r.status_code == 200
        r = await client.get("/api/auth/sessions", cookies=second_cookies)
        assert r.status_code == 401
        print("✓ 登出后旧 cookie 立即失效")

        # 无 sid 的旧版 token 被拒绝
        from app.security import create_session_token as _mk_token
        from app.models import User as _U
        async with SessionLocal() as s:
            gen_user_row = (await s.execute(select(_U).where(_U.username == gen_user))).scalar_one()
            legacy = _mk_token(gen_user_row.id, "user", gen_user_row.token_version)
        r = await client.get("/api/auth/me", cookies={"pic_session": legacy})
        assert r.status_code == 401
        print("✓ 无 sid 的旧版 token 被拒绝")

        # 上游账号管理接口存在性（无真实上游时 5xx 而非 404）
        r = await client.get("/api/admin/upstream-accounts", cookies=cookies)
        assert r.status_code in (200, 500, 502), r.status_code
        print("✓ 上游账号管理接口已挂载:", r.status_code)

        # 导入相关端点全部挂载（无真实上游时应为 5xx/超时类，绝不能是 404/405/403）
        for method, path, body in [
            ("GET", "/api/admin/upstream-accounts/groups", None),
            ("GET", "/api/admin/upstream-accounts/cpa/pools", None),
            ("GET", "/api/admin/upstream-accounts/sub2api/servers", None),
            ("POST", "/api/admin/upstream-accounts/oauth/start", {"email_hint": ""}),
            ("POST", "/api/admin/upstream-accounts/import-cleanup", {"account_ids": ["x"]}),
        ]:
            r = await client.request(
                method, path, json=body, cookies=cookies,
                headers={"x-requested-with": "XMLHttpRequest"},
            )
            assert r.status_code not in (403, 404, 405), f"{method} {path} -> {r.status_code}"
        print("✓ 上游账号导入相关端点已挂载")

        # 导入参数校验：空负载与缺少 access_token 都必须在本地拦下，不打到上游
        for bad_body in (
            {"tokens": [], "accounts": []},
            {"accounts": [{"refresh_token": "rt-only"}]},
            {"accounts": [{"access_token": "   "}]},
        ):
            r = await client.post(
                "/api/admin/upstream-accounts", json=bad_body, cookies=cookies,
                headers={"x-requested-with": "XMLHttpRequest"},
            )
            assert r.status_code == 400, (bad_body, r.status_code, r.text)
        print("✓ 上游账号导入参数校验生效")

        # 响应白名单投影：既要保留前端依赖的字段，又不能把凭据透出去
        from app.routers.admin_upstream import _project_import_job, _project_mutation

        mutation = _project_mutation(
            {
                "progress_id": "p-123",           # 前端靠它轮询异步任务，漏了会"点了没反应"
                "target_ids": ["a1"],
                "errors": [{"id": "a1", "message": "boom", "token": "SECRET"}],
                "items": [{"id": "a1", "email": "e@x.com", "access_token": "SECRET"}],
                "secret_key": "SECRET",
            }
        )
        assert mutation["progress_id"] == "p-123" and mutation["target_ids"] == ["a1"], mutation
        assert "access_token" not in mutation["items"][0], mutation["items"]
        assert "secret_key" not in mutation, mutation
        assert "token" not in mutation["errors"][0], mutation["errors"]

        job = _project_import_job(
            {
                "job_id": "j1", "status": "running", "stage_label": "读取凭据", "terminal": False,
                "progress_total": 10, "progress_completed": 3, "result_message": "", "result_tone": "info",
                "secret_key": "SECRET", "password": "SECRET",
            }
        )
        assert job["progress_total"] == 10 and job["terminal"] is False and job["stage_label"] == "读取凭据", job
        assert "secret_key" not in job and "password" not in job, job
        print("✓ 上游响应投影：保留前端依赖字段且不下发凭据")

        # ---- 双池额度 / 签到 / 审批 / 内容钩子 / 批量 / 公告 ----
        from app.services.quota import compute_temp_expiry, consume, get_quotas, grant, remaining
        from app.services.checkin import do_checkin

        async with SessionLocal() as s:
            tester = (await s.execute(select(User).where(User.username == "tester"))).scalar_one()
            # 永久 100 + 限时 5，先扣限时
            await grant(s, tester.id, "chat", 5, pool="temporary",
                        expires_at=compute_temp_expiry("Asia/Shanghai", 1, 0))
            assert (await consume(s, tester.id, "chat", 3))[0]
            q = await get_quotas(s, tester.id)
            assert q["chat"]["temp"] == 2 and q["chat"]["used"] == 0, q
            # 限时耗尽后扣永久
            assert (await consume(s, tester.id, "chat", 3))[0]
            q = await get_quotas(s, tester.id)
            assert q["chat"]["temp"] == 0 and q["chat"]["used"] == 1, q
            # 过期限时不可用
            await grant(s, tester.id, "search", 5, pool="temporary",
                        expires_at=compute_temp_expiry("Asia/Shanghai", 1, 0))
            from app.models import UserQuota as _UQ
            row = await s.get(_UQ, {"user_id": tester.id, "feature": "search"})
            row.temp_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
            await s.commit()
            assert await remaining(s, tester.id, "search") == 50 - 0  # 永久 50，限时过期作废
            print("✓ 双池额度：先扣限时、限时过期作废")

        # 签到（限时组）
        await client.patch(
            "/api/admin/settings",
            json={"checkin_enabled": True, "checkin_pool": "temporary", "checkin_chat": 3,
                  "checkin_valid_days": 1, "checkin_valid_hours": 0},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )
        r = await client.post("/api/auth/checkin", headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies)
        assert r.status_code == 200 and r.json()["granted"].get("chat") == 3, r.text
        r = await client.post("/api/auth/checkin", headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies)
        assert r.status_code == 409, r.text
        print("✓ 每日签到与重复签到拦截")

        # 公告
        await client.patch(
            "/api/admin/settings", json={"notice_text": "# 测试公告", "notice_version": 7},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )
        r = await client.get("/api/auth/notice", cookies=user_cookies)
        assert r.json()["show"] is True, r.json()
        r = await client.post("/api/auth/notice/ack", headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies)
        assert r.status_code == 200
        r = await client.get("/api/auth/notice", cookies=user_cookies)
        assert r.json()["show"] is False
        print("✓ 公告弹窗与确认")

        # 内容安全钩子
        r = await client.post(
            "/api/chat/completions", json={"message": "请教我制毒的方法"},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 403 and "安全策略" in r.json()["detail"], r.text
        print("✓ 内容安全钩子拦截")

        # 注册审批流（先清空注册限流计数：测试进程共享内存限流器，前文注册调用已占满窗口）
        from app.deps import rate_limiter
        rate_limiter._hits.clear()
        await client.patch(
            "/api/admin/settings", json={"registration_require_approval": True},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )
        r = await client.post(
            "/api/auth/register",
            json={"username": "pending01", "password": "pending-pass-1", "invite_code": code, "fp": "fp-p1", "note": "约定备注A"},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        assert r.status_code == 200 and r.json()["pending"] is True, r.text
        pending_cookies = r.cookies
        r = await client.get("/api/auth/me", cookies=pending_cookies)
        assert r.status_code == 200 and r.json()["status"] == "pending"
        r = await client.get("/api/chat/conversations", cookies=pending_cookies)
        assert r.status_code == 403 and "审核" in r.json()["detail"]
        # 管理员列表可见并显示备注
        r = await client.get("/api/admin/users?status=pending", cookies=cookies)
        pu = [u for u in r.json()["items"] if u["username"] == "pending01"][0]
        assert pu["reg_note"] == "约定备注A" and r.json()["pending_count"] >= 1
        # 通过审核
        r = await client.post(f"/api/admin/users/{pu['id']}/approve", headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies)
        assert r.status_code == 200, r.text
        r = await client.get("/api/auth/me", cookies=pending_cookies)
        assert r.json()["status"] == "active"
        print("✓ 注册审批：等待→备注→通过")

        # 拒绝流程退回邀请码次数
        def _invite_used(items, c):
            return [i for i in items if i["code"] == c][0]["used_count"]
        before = _invite_used((await client.get("/api/admin/invites", cookies=cookies)).json()["items"], code)
        r = await client.post(
            "/api/auth/register",
            json={"username": "pending02", "password": "pending-pass-1", "invite_code": code, "fp": "fp-p2"},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        assert r.json().get("pending") is True, r.text
        r = await client.get("/api/admin/users?status=pending", cookies=cookies)
        pu2 = [u for u in r.json()["items"] if u["username"] == "pending02"][0]
        r = await client.post(f"/api/admin/users/{pu2['id']}/reject", headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies)
        assert r.status_code == 200
        after = _invite_used((await client.get("/api/admin/invites", cookies=cookies)).json()["items"], code)
        assert after == before, (before, after)
        print("✓ 审核拒绝并退回邀请码次数")
        await client.patch(
            "/api/admin/settings", json={"registration_require_approval": False},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )

        # 批量调额（统一设为 + 限时组加法）
        r = await client.post(
            "/api/admin/users/bulk-quota",
            json={"user_ids": "all", "feature": "search", "pool": "permanent", "mode": "set", "amount": 66},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )
        assert r.status_code == 200 and r.json()["affected"] >= 3, r.text
        r = await client.post(
            "/api/admin/users/bulk-quota",
            json={"user_ids": [pu["id"]], "feature": "chat", "pool": "temporary", "mode": "add",
                  "amount": 9, "valid_days": 2, "valid_hours": 3},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )
        assert r.status_code == 200
        r = await client.get(f"/api/admin/users/{pu['id']}/stats", cookies=cookies)
        assert r.status_code == 200 and r.json()["quotas"]["chat"]["temp"] == 9, r.json()
        assert r.json()["quotas"]["search"]["total"] == 66
        print("✓ 批量额度调整与用户统计")

        # 老用户兑换：独立兑换码体系（邀请码不可兑换，兑换码不可注册）
        r = await client.post(
            "/api/auth/redeem", json={"code": code},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 400, r.text  # 邀请码不是兑换码
        # 反向隔离：兑换码不能用于注册
        r0 = await client.post(
            "/api/admin/redeem-codes",
            json={"max_uses": 2, "chat_quota": 7, "image_quota": 2, "search_quota": 0, "ppt_quota": 0,
                  "pool_type": "temporary", "valid_days": 1, "valid_hours": 0},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )
        assert r0.status_code == 200, r0.text
        rcode = r0.json()["code"]
        r = await client.post(
            "/api/auth/register",
            json={"username": "ghost01", "password": "ghost-pass-1", "invite_code": rcode, "fp": "fp-ghost"},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        assert r.status_code == 400, r.text  # 兑换码不能注册
        # 正常兑换
        r = await client.post(
            "/api/auth/redeem", json={"code": rcode},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 200 and r.json()["granted"].get("chat") == 7, r.text
        r = await client.get("/api/auth/me", cookies=user_cookies)
        assert r.json()["quotas"]["chat"]["temp"] >= 7, r.json()["quotas"]
        # 同一用户重复兑换（码仍有剩余次数）→ 409 已兑换过
        r = await client.post(
            "/api/auth/redeem", json={"code": rcode},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 409, r.text
        r = await client.post(
            "/api/auth/redeem", json={"code": "NOPE-123"},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 400, r.text
        # 次数耗尽：max_uses=1 的码用完后再次兑换 → 400（先于重复检查命中耗尽）
        r1 = await client.post(
            "/api/admin/redeem-codes",
            json={"max_uses": 1, "chat_quota": 1, "image_quota": 0, "search_quota": 0, "ppt_quota": 0},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )
        rcode1 = r1.json()["code"]
        r = await client.post(
            "/api/auth/redeem", json={"code": rcode1},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 200
        r = await client.post(
            "/api/auth/redeem", json={"code": rcode1},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 400, r.text
        print("✓ 老用户兑换（邀请码隔离/反向隔离/新码成功/重复与无效拦截/次数耗尽）")

        # 固定到期时间：所有人统一在该点失效，与兑换时间无关
        future_fixed = (datetime.now(timezone.utc) + timedelta(days=2, hours=3)).replace(microsecond=0)
        r = await client.post(
            "/api/admin/redeem-codes",
            json={"max_uses": 5, "chat_quota": 4, "image_quota": 0, "search_quota": 0, "ppt_quota": 0,
                  "pool_type": "temporary", "valid_days": 1, "valid_hours": 0,
                  "fixed_expires_at": future_fixed.isoformat()},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )
        assert r.status_code == 200, r.text
        fcode = r.json()["code"]
        r = await client.post(
            "/api/auth/redeem", json={"code": fcode},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 200, r.text
        r = await client.get("/api/auth/me", cookies=user_cookies)
        exp = r.json()["quotas"]["chat"]["temp_expires_at"]
        assert exp is not None and exp.startswith(future_fixed.strftime("%Y-%m-%dT%H:%M")), exp
        print("✓ 固定到期时间生效（与兑换时间无关）")

        # 固定到期时间已过：发放即失效（有效限时额度不变）
        r = await client.get("/api/auth/me", cookies=user_cookies)
        before_temp = r.json()["quotas"]["chat"]["temp"]
        r = await client.post(
            "/api/admin/redeem-codes",
            json={"max_uses": 5, "chat_quota": 4, "image_quota": 0, "search_quota": 0, "ppt_quota": 0,
                  "pool_type": "temporary", "fixed_expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=cookies,
        )
        pcode = r.json()["code"]
        r = await client.post(
            "/api/auth/redeem", json={"code": pcode},
            headers={"x-requested-with": "XMLHttpRequest"}, cookies=user_cookies,
        )
        assert r.status_code == 200
        r = await client.get("/api/auth/me", cookies=user_cookies)
        assert r.json()["quotas"]["chat"]["temp"] == before_temp, (before_temp, r.json()["quotas"]["chat"])
        print("✓ 固定到期时间已过即失效")

        # 上游标记过滤：:::writing{...} 块（整段 / 逐字节切块 / 无标记 / 未闭合）
        from app.services.text_filter import WritingMarkerFilter, strip_writing_markers
        raw = ':::writing{variant="standard" title="t" id="1"}\n正文第一行\n第二行:::\n后续内容'
        assert strip_writing_markers(raw) == "正文第一行\n第二行后续内容", strip_writing_markers(raw)
        # 逐字节喂入（模拟 SSE 任意切chunk）结果一致
        f = WritingMarkerFilter()
        merged = "".join(f.feed(ch) for ch in raw) + f.flush()
        assert merged == "正文第一行\n第二行后续内容", merged
        # 无标记原文放行；C++ 作用域 :: 不误伤；行内 :: 不误伤
        plain = "a::b std::vector 普通文本\n:::note 不是目标标记"
        assert strip_writing_markers(plain) == plain
        # 开始标记未闭合：flush 原文放行不丢字
        unclosed = "前文:::writing{abc"
        assert strip_writing_markers(unclosed) == unclosed
        # 块结束标记缺失：只去开头，正文保留
        no_close = ":::writing{x}\n保留我"
        assert strip_writing_markers(no_close) == "保留我", strip_writing_markers(no_close)
        print("✓ 上游 :::writing 标记过滤（整段/切块/误伤/未闭合）")

    # ---- 数据库迁移框架：历史库一键升级 / 幂等 / 篡改检测 / 全新库空跑 ----
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sa_text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.migrations import load_migrations, migration_status, run_migrations

    migrations = load_migrations()
    assert migrations, "未发现任何迁移脚本"

    # 造一个「启用迁移框架之前」的老库：先把表建成当前模型结构，
    # 再删掉「由迁移负责补齐」的列（等价于老版本的表结构），
    # 并把 redemptions 换回旧结构。夹具自动跟随模型演进，不会写死。
    migration_managed_columns = {
        "users": [
            "storage_limit_mb", "reg_fp", "reg_ip", "agreement_version",
            "notice_version", "reg_note", "last_checkin_key", "max_inflight", "note",
        ],
        "user_quotas": ["temp_amount", "temp_expires_at"],
        "invite_codes": ["pool_type", "valid_days", "valid_hours", "fixed_expires_at"],
        "redemption_codes": ["fixed_expires_at"],
        "stored_files": ["tags"],
    }
    legacy_path = Path("./data-smoke/legacy.db")
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.unlink(missing_ok=True)
    legacy_engine = create_async_engine("sqlite+aiosqlite:///./data-smoke/legacy.db")
    from app.database import Base as AppBase

    async with legacy_engine.begin() as conn:
        await conn.run_sync(AppBase.metadata.create_all)
        await conn.execute(sa_text("DROP INDEX IF EXISTS ix_users_reg_fp"))
        for table, columns in migration_managed_columns.items():
            for column in columns:
                await conn.execute(sa_text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        await conn.execute(sa_text("DROP TABLE redemptions"))
        await conn.execute(
            sa_text("CREATE TABLE redemptions (id INTEGER PRIMARY KEY, invite_code_id INTEGER, user_id INTEGER)")
        )
        # 老库里放一条历史用户：NOT NULL 且无默认值的列用占位值填满，
        # 这样夹具不会因为模型新增非空列而失效
        user_columns = await conn.run_sync(lambda c: sa_inspect(c).get_columns("users"))
        row: dict[str, object] = {}
        for column in user_columns:
            name, default = column["name"], column["default"]
            if name == "id":
                row[name] = 1
            elif name == "username":
                row[name] = "legacy_user"
            elif name == "password_hash":
                row[name] = "h"
            elif column["nullable"] or default is not None:
                continue
            elif "INT" in str(column["type"]).upper():
                row[name] = 0
            else:
                row[name] = ""
        await conn.execute(
            sa_text(
                f"INSERT INTO users ({', '.join(row)}) VALUES ({', '.join(f':{k}' for k in row)})"
            ),
            row,
        )
        await conn.execute(sa_text("INSERT INTO redemptions (id, invite_code_id, user_id) VALUES (1, 7, 1)"))

    legacy_report = await run_migrations(legacy_engine)
    assert legacy_report.applied, legacy_report.summary()
    assert any(item.changed for item in legacy_report.applied), legacy_report.summary()

    async with legacy_engine.connect() as conn:
        tables = await conn.run_sync(lambda c: set(sa_inspect(c).get_table_names()))
        user_cols = await conn.run_sync(lambda c: {x["name"] for x in sa_inspect(c).get_columns("users")})
        user_indexes = await conn.run_sync(lambda c: {x["name"] for x in sa_inspect(c).get_indexes("users")})
        new_cols = await conn.run_sync(
            lambda c: {x["name"] for x in sa_inspect(c).get_columns("redemptions")}
        )
        kept_user = (await conn.execute(sa_text("SELECT username FROM users WHERE id = 1"))).scalar()
        kept_redeem = (
            await conn.execute(sa_text("SELECT invite_code_id FROM redemptions_legacy WHERE id = 1"))
        ).scalar()
    assert {"note", "reg_fp", "max_inflight", "storage_limit_mb"} <= user_cols, user_cols
    assert "ix_users_reg_fp" in user_indexes, user_indexes
    assert "redemptions_legacy" in tables and "redemption_code_id" in new_cols, (tables, new_cols)
    assert kept_user == "legacy_user" and kept_redeem == 7, (kept_user, kept_redeem)
    print("✓ 迁移：历史库自动升级（补列 / 建索引 / 重建表）且数据保留")

    assert (await run_migrations(legacy_engine)).applied == []
    print("✓ 迁移：重复执行幂等，不会重复改结构")

    # 命名不合规的迁移文件必须直接报错，而不是被静默跳过
    from app.migrations import MigrationError
    from app.migrations import versions as migrations_versions

    # 用包路径定位，避免依赖测试脚本自身位置
    bad_migration = Path(migrations_versions.__path__[0]) / "m3_bad_name.py"
    bad_migration.write_text('DESCRIPTION = "bad"\n\n\ndef apply(ctx):\n    pass\n', encoding="utf-8")
    try:
        load_migrations()
        raise AssertionError("命名不合规的迁移文件没有被拦截")
    except MigrationError as exc:
        assert "m3_bad_name" in str(exc), exc
    finally:
        bad_migration.unlink(missing_ok=True)
    assert all(m.name != "bad_name" for m in load_migrations())
    print("✓ 迁移：命名不合规的文件被拦截（不会静默漏跑）")

    async with legacy_engine.begin() as conn:
        await conn.execute(sa_text("UPDATE schema_migrations SET checksum = 'tampered' WHERE id = '0001'"))
    states = {row.id: row.state for row in await migration_status(legacy_engine)}
    assert states.get("0001") == "edited", states
    print("✓ 迁移：能发现已应用迁移被事后修改")

    fresh_path = Path("./data-smoke/fresh.db")
    fresh_path.unlink(missing_ok=True)
    fresh_engine = create_async_engine("sqlite+aiosqlite:///./data-smoke/fresh.db")
    fresh_report = await run_migrations(fresh_engine)
    assert all(item.changed == 0 for item in fresh_report.applied), fresh_report.summary()
    async with fresh_engine.connect() as conn:
        fresh_tables = await conn.run_sync(lambda c: set(sa_inspect(c).get_table_names()))
    assert fresh_tables == {"schema_migrations"}, fresh_tables
    await fresh_engine.dispose()
    print("✓ 迁移：全新库空跑，表结构交给 create_all")

    from app.database import SessionLocal as MigrationSession

    async with MigrationSession() as session:
        recorded = (await session.execute(sa_text("SELECT COUNT(*) FROM schema_migrations"))).scalar()
    assert recorded == len(migrations), (recorded, len(migrations))
    await init_db()  # 模拟容器重启：迁移不得重复执行
    async with MigrationSession() as session:
        recorded_again = (await session.execute(sa_text("SELECT COUNT(*) FROM schema_migrations"))).scalar()
    assert recorded_again == len(migrations), recorded_again

    # 关键不变量：历史库迁移后的列集合必须与全新库（create_all 建的）一致。
    # 相差即说明「迁移加了列但模型漏加」（新装缺列）或「模型加了列但没写迁移」（老库缺列）。
    from app.database import engine as app_engine

    compared_tables = ("users", "user_quotas", "invite_codes", "redemptions", "stored_files")
    fresh_columns: dict[str, set] = {}
    async with app_engine.connect() as conn:
        for table in compared_tables:
            fresh_columns[table] = await conn.run_sync(
                lambda c, t=table: {x["name"] for x in sa_inspect(c).get_columns(t)}
            )
    async with legacy_engine.connect() as conn:
        for table in compared_tables:
            legacy_columns = await conn.run_sync(
                lambda c, t=table: {x["name"] for x in sa_inspect(c).get_columns(t)}
            )
            assert fresh_columns[table] == legacy_columns, (
                table,
                fresh_columns[table] ^ legacy_columns,
            )
    print("✓ 迁移：历史库迁移后结构 == 全新库结构（模型与迁移无漂移）")

    await legacy_engine.dispose()
    print(f"✓ 迁移：启动自动执行且二次启动不重复（共 {len(migrations)} 条）")

    from app.database import engine
    await engine.dispose()  # 释放连接，保证 Windows 下能删除数据目录

    print("\n全部冒烟测试通过 ✅")

asyncio.run(main())
shutil.rmtree("./data-smoke", ignore_errors=True)
