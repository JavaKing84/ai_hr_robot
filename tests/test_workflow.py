"""端到端闭环测试（mock 飞书 + mock 邮件）：

收简历 → 解析筛选 → 飞书发主管 → 主管飞书回复名单+时间 → 面试邮件
→ 候选人回信改期 → 主管同意 → 补发确认邮件 → 另一位候选人放弃 → 通知主管
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import drop_resume_email, feishu_text_event, read_outbox, resume_text

BACKEND_SUPERVISOR = "ou_mock_zhangwei"


@pytest.fixture
async def cycle(client, settings):
    """准备三份简历并跑一轮：2 份通过、1 份学历淘汰。"""
    drop_resume_email(
        settings,
        resume=resume_text(),
        sender="zhangming@example.com",
        subject="应聘高级后端工程师-张明",
        filename="张明-高级后端工程师.txt",
    )
    drop_resume_email(
        settings,
        resume=resume_text(
            name="李四",
            email="lisi@example.com",
            phone="13800002222",
            school="北京邮电大学",
            years=5,
            skills="Python、Java、MySQL、Redis、微服务",
            projects=("负责订单系统性能优化，接口耗时下降 60%", "主导微服务拆分与灰度发布"),
        ),
        sender="lisi@example.com",
        subject="应聘高级后端工程师-李四",
        filename="李四-高级后端工程师.txt",
    )
    drop_resume_email(
        settings,
        resume=resume_text(
            name="王芳",
            email="wangfang@example.com",
            phone="13800003333",
            education="大专",
            school="某职业技术学院",
            years=2,
            skills="PS、Excel",
            projects=("负责门店日常运营数据整理",),
        ),
        sender="wangfang@example.com",
        subject="应聘高级后端工程师-王芳",
        filename="王芳-高级后端工程师.txt",
    )

    response = await client.post("/api/v1/recruitment/run")
    assert response.status_code == 200, response.text
    return response.json()


async def test_cycle_screens_and_dispatches(client, cycle, settings) -> None:
    assert cycle["ingested"] == 3
    assert cycle["screened_passed"] == 2
    assert cycle["screened_rejected"] == 1
    assert cycle["duplicates"] == 0
    assert len(cycle["packages"]) == 1

    package = cycle["packages"][0]
    assert package["position_code"] == "backend_senior"
    assert package["passed_count"] == 2
    assert package["received_count"] == 3
    assert set(package["candidate_names"]) == {"张明", "李四"}
    assert Path(package["package_path"]).exists()
    assert Path(package["package_path"]).suffix == ".zip"

    # 飞书通知文案对齐截图验收样例
    messages = (
        await client.get("/api/v1/recruitment/messages", params={"channel": "feishu", "direction": "outbound"})
    ).json()
    dispatch_text = next(m["body"] for m in messages if "本次贵部门面试岗位" in m["body"])
    assert dispatch_text.startswith("张总")
    assert "共收到 3 份简历" in dispatch_text
    assert "符合条件的简历为 2 份" in dispatch_text
    assert "已经发送到您邮箱" in dispatch_text

    # 主管邮箱收到简历包副本（这条路径支撑“在原邮件上回复”）
    outbox = read_outbox(settings)
    supervisor_mail = next(mail for mail in outbox if mail.to_addresses[0] == "zhangwei@xingye-test.com")
    assert supervisor_mail.attachments
    assert supervisor_mail.attachments[0].filename.endswith(".zip")
    assert "面试" in supervisor_mail.body_text

    candidates = (await client.get("/api/v1/recruitment/candidates")).json()["items"]
    statuses = {item["name"]: item["status"] for item in candidates}
    assert statuses["张明"] == "dispatched"
    assert statuses["李四"] == "dispatched"
    assert statuses["王芳"] == "screened_rejected"


async def test_supervisor_reply_triggers_interview_emails(client, cycle, settings) -> None:
    payload = feishu_text_event(
        open_id=BACKEND_SUPERVISOR,
        text="我已经筛选审核完毕，请通知张明、李四，于下周三下午14:00点来公司面试",
        message_id="om_reply_1",
    )
    response = await client.post("/api/v1/feishu/webhook", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["handled"] is True
    assert body["detail"]["action"] == "interview_scheduled"
    assert body["detail"]["notices_sent"] == 2

    candidates = (await client.get("/api/v1/recruitment/candidates")).json()["items"]
    for item in candidates:
        if item["name"] in {"张明", "李四"}:
            assert item["status"] == "interview_notified"
            assert item["interview_time_display"]

    outbox = read_outbox(settings)
    notices = [mail for mail in outbox if "面试通知" in mail.subject]
    assert len(notices) == 2
    assert all("面试时间" in mail.body_text for mail in notices)

    messages = (
        await client.get("/api/v1/recruitment/messages", params={"channel": "feishu", "direction": "outbound"})
    ).json()
    assert any("已按您的安排通知" in m["body"] for m in messages)


async def test_candidate_reschedule_and_supervisor_approval(client, cycle, settings) -> None:
    # 主管先给出名单与时间
    await client.post(
        "/api/v1/feishu/webhook",
        json=feishu_text_event(
            open_id=BACKEND_SUPERVISOR,
            text="请通知张明、李四，下周三下午14:00来公司面试",
            message_id="om_reply_1",
        ),
    )

    outbound = (
        await client.get("/api/v1/recruitment/messages", params={"channel": "email", "direction": "outbound"})
    ).json()
    notice = next(m for m in outbound if m["recipient"] == "zhangming@example.com" and "面试通知" in m["subject"])

    reply = await client.post(
        "/api/v1/email/simulate-inbound",
        json={
            "from_address": "zhangming@example.com",
            "subject": "回复：面试通知",
            "body": "您好，下周二我临时有会，能否改到周四下午 3 点？",
            "in_reply_to": notice["external_id"],
            "references": [notice["external_id"]],
        },
    )
    assert reply.status_code == 200, reply.text
    assert reply.json()["details"][0]["kind"] == "candidate_reschedule"

    candidates = (await client.get("/api/v1/recruitment/candidates", params={"name": "张明"})).json()["items"]
    assert candidates[0]["status"] == "reschedule_pending"

    messages = (
        await client.get("/api/v1/recruitment/messages", params={"channel": "feishu", "direction": "outbound"})
    ).json()
    assert any("希望改期" in m["body"] for m in messages)

    # 主管回复“同意”，机器人按候选人提议时间补发确认邮件
    approval = await client.post(
        "/api/v1/feishu/webhook",
        json=feishu_text_event(
            open_id=BACKEND_SUPERVISOR,
            text="同意，按候选人提议的时间安排",
            message_id="om_reply_2",
        ),
    )
    assert approval.json()["detail"]["action"] == "reschedule_confirmed"

    candidates = (await client.get("/api/v1/recruitment/candidates", params={"name": "张明"})).json()["items"]
    assert candidates[0]["status"] == "interview_notified"
    assert "15:00" in candidates[0]["interview_time_display"]

    outbox = read_outbox(settings)
    assert any("面试时间调整确认" in mail.subject for mail in outbox)


async def test_candidate_decline_notifies_supervisor(client, cycle) -> None:
    await client.post(
        "/api/v1/feishu/webhook",
        json=feishu_text_event(
            open_id=BACKEND_SUPERVISOR,
            text="请通知张明、李四，下周三下午14:00来公司面试",
            message_id="om_reply_1",
        ),
    )
    outbound = (
        await client.get("/api/v1/recruitment/messages", params={"channel": "email", "direction": "outbound"})
    ).json()
    notice = next(m for m in outbound if m["recipient"] == "lisi@example.com")

    response = await client.post(
        "/api/v1/email/simulate-inbound",
        json={
            "from_address": "lisi@example.com",
            "subject": "回复：面试通知",
            "body": "不好意思，我已经找到工作了，这次就不参加了。",
            "in_reply_to": notice["external_id"],
        },
    )
    assert response.json()["details"][0]["kind"] == "candidate_decline"

    candidates = (await client.get("/api/v1/recruitment/candidates", params={"name": "李四"})).json()["items"]
    assert candidates[0]["status"] == "declined"

    messages = (
        await client.get("/api/v1/recruitment/messages", params={"channel": "feishu", "direction": "outbound"})
    ).json()
    assert any("放弃面试" in m["body"] for m in messages)


async def test_supervisor_reply_by_email_path(client, cycle) -> None:
    """第二条回复路径：主管直接回在原简历邮件上（不经过飞书）。"""
    outbound = (
        await client.get("/api/v1/recruitment/messages", params={"channel": "email", "direction": "outbound"})
    ).json()
    package_mail = next(m for m in outbound if m["recipient"] == "zhangwei@xingye-test.com")

    response = await client.post(
        "/api/v1/email/simulate-inbound",
        json={
            "from_address": "zhangwei@xingye-test.com",
            "subject": "回复：【简历筛选】技术部·高级后端工程师",
            "body": "这 12 份我只留 2 份，请通知张明、李四，下周二上午10点来公司面试",
            "in_reply_to": package_mail["external_id"],
        },
    )
    assert response.status_code == 200, response.text
    detail = response.json()["details"][0]
    assert detail["kind"] == "supervisor_feedback"
    assert detail["action"] == "interview_scheduled"

    candidates = (await client.get("/api/v1/recruitment/candidates")).json()["items"]
    assert all(
        item["status"] == "interview_notified" for item in candidates if item["name"] in {"张明", "李四"}
    )
