"""2026-11-14 加固改动的回归测试。

每个用例对应「待优化清单」里的一条，防止以后改回去：

1. 接口鉴权（P0）：配了 API_TOKEN 必须拦住无令牌请求；
2. 飞书回调校验（P0）：token 不匹配、缺 token、生产环境未配置 —— 三种都必须拒收；
3. 发送失败不推进状态（P0）：「说发了、其实没发」是最贵的脏数据；
4. 入站幂等（P1）：同一 Message-ID 只处理一次；
5. 排期静默（P0-bug）：点名的人不能排期时必须说清原因；
6. 咨询锁（P1）：多副本只有一个实例能跑；
7. 运营视图（P2）：stage_snapshot 与 daily 口径各自自洽。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from client.mailbox import InboundEmail
from core.locks import LOCK_DAILY_CYCLE, advisory_lock
from core.paths import resolve_resume_path
from model.candidate import Candidate, CandidateStatus
from model.position import Position
from schema.message import InboundMessage
from service.reporting import ReportingService
from tests.conftest import feishu_text_event, resume_text

NOW = datetime(2026, 11, 20, 6, 0, 0)


# ------------------------------------------------------------------ 1. 接口鉴权


async def test_api_requires_token_when_configured(app, container, settings) -> None:
    settings.api_token = "unit-test-token"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        anonymous = await client.get("/api/v1/recruitment/positions")
        assert anonymous.status_code == 401

        wrong = await client.get(
            "/api/v1/recruitment/positions", headers={"X-API-Token": "nope"}
        )
        assert wrong.status_code == 401

        ok = await client.get(
            "/api/v1/recruitment/positions", headers={"X-API-Token": "unit-test-token"}
        )
        assert ok.status_code == 200

        bearer = await client.get(
            "/api/v1/recruitment/positions",
            headers={"Authorization": "Bearer unit-test-token"},
        )
        assert bearer.status_code == 200

        # 健康检查始终公开（容器健康检查依赖它），但不能因此泄露令牌
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["components"]["auth"] == "enabled"
        assert "unit-test-token" not in health.text


async def test_api_is_open_when_token_not_configured(client, settings) -> None:
    settings.api_token = ""
    response = await client.get("/api/v1/recruitment/positions")
    assert response.status_code == 200


# ------------------------------------------------------------------ 2. 飞书回调校验


def _payload(token: str = "") -> dict:
    payload = feishu_text_event(open_id="ou_supervisor", text="你好")
    if token:
        payload["header"]["token"] = token
    return payload


async def test_feishu_webhook_rejects_wrong_token(client, settings) -> None:
    settings.feishu_verification_token = "expected-token"
    response = await client.post("/api/v1/feishu/webhook", json=_payload("wrong-token"))
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "configuration_error"

    ok = await client.post("/api/v1/feishu/webhook", json=_payload("expected-token"))
    assert ok.status_code == 200


async def test_feishu_webhook_rejects_missing_token(client, settings) -> None:
    """老实现只在「两边都有值」时比对，缺 token 就能过——这条防的就是那个洞。"""
    settings.feishu_verification_token = "expected-token"
    response = await client.post("/api/v1/feishu/webhook", json=_payload(""))
    assert response.status_code == 500


async def test_feishu_webhook_requires_config_in_prod(client, settings) -> None:
    settings.feishu_verification_token = ""
    settings.feishu_encrypt_key = ""
    settings.feishu_verify_mode = "auto"
    settings.environment = "dev"
    dev_ok = await client.post("/api/v1/feishu/webhook", json=_payload(""))
    assert dev_ok.status_code == 200, "dev 环境未配置 token 时应放行并告警"

    settings.environment = "prod"
    prod_reject = await client.post("/api/v1/feishu/webhook", json=_payload(""))
    assert prod_reject.status_code == 500


async def test_feishu_webhook_signature_verified(client, settings) -> None:
    import hashlib

    settings.feishu_encrypt_key = "test-encrypt-key"
    settings.feishu_verification_token = ""
    body = '{"header":{"event_type":"im.message.receive_v1","token":""}}'
    timestamp, nonce = "1700000000", "abc123"
    good = hashlib.sha256(
        (timestamp + nonce + "test-encrypt-key").encode() + body.encode()
    ).hexdigest()

    bad_resp = await client.post(
        "/api/v1/feishu/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Lark-Signature": "deadbeef",
            "X-Lark-Request-Timestamp": timestamp,
            "X-Lark-Request-Nonce": nonce,
        },
    )
    assert bad_resp.status_code == 500

    ok_resp = await client.post(
        "/api/v1/feishu/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Lark-Signature": good,
            "X-Lark-Request-Timestamp": timestamp,
            "X-Lark-Request-Nonce": nonce,
        },
    )
    assert ok_resp.status_code == 200


# ---------------------------------------------------------- 3. 发送失败不推进状态


class _FailingMailbox:
    """把 send() 固定返回失败，模拟 SMTP 挂了。"""

    def __init__(self, real) -> None:
        self._real = real

    def __getattr__(self, name):  # 其余方法透传
        return getattr(self._real, name)

    async def send(self, **kwargs):
        from client.mailbox import SendResult

        return SendResult(
            ok=False,
            mode="live",
            error="SMTP 连接超时（测试注入）",
            message_id=kwargs.get("message_id", ""),
        )


async def test_failed_notice_keeps_status(services, session) -> None:
    position = (await session.execute(select(Position).where(Position.code == "backend_senior"))).scalars().one()
    candidate = Candidate(
        name="发送失败候选人",
        email="fail@example.com",
        phone="13900003333",
        position_id=position.id,
        position_code=position.code,
        status=CandidateStatus.SUPERVISOR_FEEDBACK,
        score=80.0,
    )
    session.add(candidate)
    # 必须 commit：services 夹具用的是另一个连接，flush 出来的行它看不见，
    # 会先撞 email_threads 的外键约束，测不到我们真正想测的「发送失败」。
    await session.commit()

    services.notification.mailbox = _FailingMailbox(services.container.mailbox)
    result = await services.notification.send_interview_notice(
        candidate, position, interview_time=NOW
    )

    assert result.ok is False
    assert candidate.status == CandidateStatus.SUPERVISOR_FEEDBACK, "发送失败不得标记为已通知"
    assert candidate.interview_time == NOW, "面试时间仍要落库，补发时才用得上"


async def test_failed_dispatch_keeps_screened_passed(services, session, settings) -> None:
    position = (await session.execute(select(Position).where(Position.code == "backend_senior"))).scalars().one()
    candidate = Candidate(
        name="分发失败候选人",
        email="dispatchfail@example.com",
        position_id=position.id,
        position_code=position.code,
        status=CandidateStatus.SCREENED_PASSED,
        cycle_id=202611140900,
        score=70.0,
    )
    session.add(candidate)
    await session.commit()

    # 三个渠道全部失败
    services.notification.feishu.push_text = _async_fail
    services.notification.feishu.push_file = _async_fail
    services.notification.mailbox = _FailingMailbox(services.container.mailbox)

    results = await services.notification.dispatch_cycle(202611140900)
    assert results and results[0].ok is False
    await session.refresh(candidate)
    assert candidate.status == CandidateStatus.SCREENED_PASSED, "没送达就不能算已发主管"


async def _async_fail(*args, **kwargs):
    from client.feishu import FeishuSendResult

    return FeishuSendResult(ok=False, mode="live", error="飞书不可达（测试注入）")


# ------------------------------------------------------------------ 4. 入站幂等


async def test_same_email_message_id_processed_once(services) -> None:
    message_id = f"<dup-{uuid.uuid4().hex[:8]}@example.com>"
    email = InboundEmail(
        message_id=message_id,
        subject="应聘高级后端工程师",
        from_address="dup@example.com",
        to_addresses=["hr@xingye-test.com"],
        body_text=resume_text(),
        received_at=datetime(2026, 11, 14, 1, 0, 0),
    )
    first = await services.inbound.handle_email(email)
    await services.session.commit()
    kind = first.kind
    assert kind in {"resume", "candidate_reply", "supervisor_feedback", "unmatched", "duplicate"}

    second = await services.inbound.handle_email(email)
    assert second.kind == "duplicate"
    assert second.handled is False


# -------------------------------------------------------------- 5. 排期静默修复


async def test_unschedulable_name_is_reported(services, session, settings) -> None:
    """点名的人处于 screened_passed（还没发主管）时，必须明确回一句，不能静默。"""
    position = (await session.execute(select(Position).where(Position.code == "backend_senior"))).scalars().one()
    candidate = Candidate(
        name="李四静默",
        email="silent@example.com",
        position_id=position.id,
        position_code=position.code,
        status=CandidateStatus.SCREENED_PASSED,
        score=66.0,
    )
    session.add(candidate)
    await session.commit()

    inbound = InboundMessage(
        channel="feishu",
        sender_id=position.supervisor_open_id,
        sender_name=position.supervisor_name,
        text="请通知李四静默，下周三下午 14:00 来面试",
        message_id=f"om_{uuid.uuid4().hex[:8]}",
        thread_id="oc_test",
        received_at=datetime(2026, 11, 14, 1, 0, 0),
    )
    # 关掉 Agent 路由（llm off），强制走确定性解析，确保测的是 _handle_schedule 本身
    outcome = await services.feedback.handle(inbound)
    await services.session.commit()

    assert outcome.handled is False
    assert "李四静默" in outcome.reply_text
    assert "筛选通过但还没发给主管" in outcome.reply_text


# ------------------------------------------------------------------ 6. 咨询锁


async def test_advisory_lock_is_exclusive(container) -> None:
    async with advisory_lock(container.engine, LOCK_DAILY_CYCLE, name="t1") as first:
        assert first is True
        async with advisory_lock(container.engine, LOCK_DAILY_CYCLE, name="t2") as second:
            assert second is False, "同一把锁不能被两个会话同时拿到"

    async with advisory_lock(container.engine, LOCK_DAILY_CYCLE, name="t3") as third:
        assert third is True, "释放后应能再次获取"


# ------------------------------------------------------------------ 7. 运营视图


async def test_reporting_snapshot_and_daily(services, session) -> None:
    position = (await session.execute(select(Position).where(Position.code == "backend_senior"))).scalars().one()
    base = dict(
        position_id=position.id,
        position_code=position.code,
        created_at=datetime(2026, 11, 14, 2, 0, 0),
        updated_at=datetime(2026, 11, 14, 2, 0, 0),
    )
    session.add_all(
        [
            Candidate(name="快照通过", email="snap1@example.com", status=CandidateStatus.SCREENED_PASSED, **base),
            Candidate(name="快照淘汰", email="snap2@example.com", status=CandidateStatus.SCREENED_REJECTED, **base),
            Candidate(
                name="快照重复",
                email="snap3@example.com",
                status=CandidateStatus.SCREENED_REJECTED,
                is_duplicate=True,
                **base,
            ),
        ]
    )
    await session.commit()

    reporting = ReportingService(services.session)
    snapshot = await reporting.funnel(days=30)
    assert snapshot.stage_snapshot.get("screened_passed") == 1
    assert snapshot.stage_snapshot.get("screened_rejected") == 1, "重复投递不应计入阶段分布"
    assert snapshot.duplicate_count == 1

    daily = await reporting.daily_counts(days=30)
    assert daily, "应至少有一天的流水"
    row = next(item for item in daily if item["day"] == "2026-11-14")
    assert row["received"] == 2
    assert row["passed"] == 1
    assert row["rejected"] == 1


# ------------------------------------------------------------ 8. 原件路径回落


def test_resume_path_falls_back_to_repo_var() -> None:
    """历史绝对路径在项目搬家后失效，但同名文件还在 var/resumes 时应当能打开。"""
    from config.settings import BASE_DIR

    target = BASE_DIR / "var" / "resumes"
    target.mkdir(parents=True, exist_ok=True)
    probe = target / "_path_probe_resume.txt"
    probe.write_text("probe", encoding="utf-8")
    try:
        resolved = resolve_resume_path(r"D:\Desktop\hr-ai-robot1-develop\var\resumes\_path_probe_resume.txt")
        assert resolved is not None and resolved.exists()
        assert resolved.name == "_path_probe_resume.txt"
    finally:
        probe.unlink(missing_ok=True)


# --------------------------------------------------- 9. 索引体检（防优化被改回去）


async def test_key_indexes_exist(container) -> None:
    """把「这次优化真正依赖的索引」钉死在测试里。

    只测功能不测索引的话，下次有人觉得「这个索引没用到」把它删掉，
    一切测试照样全绿，线上慢慢变慢——这类退化最难发现。
    """
    from sqlalchemy import text

    expected = {
        "ix_candidates_status_position_id",  # 主管看自己名下的名单
        "ix_candidates_position_status_score",  # 按职位取最高分
        "ix_candidates_skills_gin",  # 技能 JSONB 包含查询
        "ix_candidates_email",  # 按邮箱查重投
        "ux_messages_channel_external",  # 消息幂等（唯一）
        "ix_messages_external_id",  # 幂等判重的实际查询入口
        "ix_messages_retry",  # 失败补发 / 待确认扫描
        "ix_events_object",  # 事件溯源
        "ix_email_threads_message_ids_gin",  # 按 Message-ID 归线程
    }
    async with container.engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
        )
        actual = {row[0] for row in rows}
    missing = expected - actual
    assert not missing, f"索引缺失：{sorted(missing)}"


async def test_idempotency_lookup_uses_index(container) -> None:
    """幂等判重必须走索引，不能是全表扫。

    0003 那条部分唯一索引的谓词里带 ``status``，而判重查询并不约束 status，
    PostgreSQL 无法证明查询行落在索引里 —— 实测退化成全表扫（6 万行 7.5ms/次）。
    0004 补的单列索引就是为这条路径准备的。

    这里只用 ``external_id`` 一个条件来解释：真实查询还带 channel/direction，
    小表上规划器可能选那条复合索引（同样不是全表扫，可以接受），
    只有单独按 external_id 过滤时计划才是确定性的，适合做断言。
    """
    from sqlalchemy import text

    async with container.engine.connect() as conn:
        await conn.execute(text("SET enable_seqscan = off"))
        plan_rows = await conn.execute(
            text(
                "EXPLAIN SELECT id FROM messages "
                "WHERE external_id = '<probe@example.com>' LIMIT 1"
            )
        )
        plan = "\n".join(row[0] for row in plan_rows)
    assert "ix_messages_external_id" in plan, f"external_id 查询没走索引：\n{plan}"
    assert "Seq Scan" not in plan, f"退化成全表扫了：\n{plan}"


async def test_auth_error_uses_unified_envelope(app, settings) -> None:
    """401 也要走统一错误信封，并带 WWW-Authenticate。

    以前 HTTPException 被渲染成 ``http_401``，而且 headers 被丢掉——
    调用方既拿不到可读的 code，也不知道该用哪种认证方式。
    """
    settings.api_token = "unit-test-token"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/v1/recruitment/positions")
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthorized"
    assert body["error"]["request_id"], "错误信封必须带 request_id，否则排查断链"
    assert response.headers.get("WWW-Authenticate") == "Bearer"


# ------------------------------------------- 10. 排期回执只发一条（2026-11-14）


from tests.test_workflow import BACKEND_SUPERVISOR, cycle  # noqa: E402,F401


async def _outbound_feishu(client) -> list[dict]:
    resp = await client.get(
        "/api/v1/recruitment/messages",
        params={"channel": "feishu", "direction": "outbound"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_schedule_reply_is_single_message(client, cycle) -> None:
    """主管一句话点人+给时间，回执只能有一条汇总，且不能再问「确认」。

    改之前：按岗位各发一条「已通知 N 人」，且确认闸门默认开着，还会追问
    「请确认全发 / 确认 1,3 / 取消」——主管明明已经说了要通知谁，等于让
    他把同一件事说第二遍；多条消息还会累加（2 份 → 3 份）。
    """
    payload = feishu_text_event(
        open_id=BACKEND_SUPERVISOR,
        text="通知张明、李四，下周三下午14:00来公司面试",
        message_id="om_single_receipt_1",
    )
    resp = await client.post("/api/v1/feishu/webhook", json=payload)
    assert resp.status_code == 200, resp.text
    detail = resp.json()["detail"]
    assert detail["action"] == "interview_scheduled"
    assert detail["notices_pending_confirm"] == 0, "闸门默认关闭，不该再挂起等确认"
    assert detail["notices_sent"] == 2

    receipts = [m for m in await _outbound_feishu(client) if "已按您的话安排" in (m["body"] or "")]
    assert len(receipts) == 1, f"一句话只该回一条汇总，实际回了 {len(receipts)} 条"
    body = receipts[0]["body"]
    assert "张明" in body and "李四" in body
    assert "地点：" in body, "回执必须带地点，否则主管还要再问一次"
    assert "待确认" not in body
    assert "确认 1,3" not in body


async def test_unknown_name_reported_in_same_receipt(client, cycle) -> None:
    """库里没有的人名也要在同一条回执里说清楚，不能当作没看见。

    主管写「通知张明、李四、王五」而王五不在系统里时，改之前模型只抽出
    前两个，王五彻底消失，主管以为三个都安排了。
    """
    payload = feishu_text_event(
        open_id=BACKEND_SUPERVISOR,
        text="通知张明、李四、王五，下周三下午14:00来公司面试",
        message_id="om_unknown_name_1",
    )
    resp = await client.post("/api/v1/feishu/webhook", json=payload)
    assert resp.status_code == 200, resp.text

    receipts = [m for m in await _outbound_feishu(client) if "已按您的话安排" in (m["body"] or "")]
    assert len(receipts) == 1, f"应当只有一条汇总回执，实际 {len(receipts)} 条"
    body = receipts[0]["body"]
    assert "王五" in body, f"王五必须出现在回执里：{body}"
    assert "没有这个人" in body, f"要说清为什么排不了：{body}"
