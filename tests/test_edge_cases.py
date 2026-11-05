"""边界与异常场景测试（任务 4 补强）：

锁死四类异常输入，证明系统不是“跑通流程的脚本”，而是对边界情况有明确的不变量：

1. 含糊不清的主管指令（缺姓名 / 缺时间）——feedback 层两道防线，绝不发错面试邮件；
2. 重复简历及附件——入库去重，二次投递只留痕、不新增记录；
3. 附件损坏或无法解析——解析降级链不崩溃、留 warnings 留痕；
4. 乱序信息（时间在名字前）——反馈路径下仍正确抽取并发送（正向对照；抽取层已被 eval 覆盖）。

全部离线（mock 飞书 + mock 邮件 + LLM off），确定性。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from client.resume_parser import ParsedResume, ResumeParser
from core.exceptions import ValidationFailed
from repository.candidate import CandidateRepository
from repository.event import EventRepository
from tests.conftest import drop_resume_email, feishu_text_event, read_outbox, resume_text

BACKEND_SUPERVISOR = "ou_mock_zhangwei"


async def _seed_one_dispatched(client, settings, *, name: str = "张明") -> None:
    """准备一名已通过筛选、处于 dispatched 的候选人（反馈路径的前置状态）。"""
    drop_resume_email(
        settings,
        resume=resume_text(name=name),
        sender=f"{name}@example.com",
        subject=f"应聘高级后端工程师-{name}",
        filename=f"{name}-高级后端工程师.txt",
    )
    response = await client.post("/api/v1/recruitment/run")
    assert response.status_code == 200, response.text


# ----------------------------------------------------------------- 1. 含糊不清的主管指令


async def test_supervisor_missing_time_does_not_send_notice(client, settings) -> None:
    """缺面试时间：只给姓名不给时间 → 回飞书追问时间，绝不发错面试通知邮件。

    守护不变量：缺少时间时机器人不得“猜一个时间”发出面试通知（I-05 精神：不猜不发）。
    """
    await _seed_one_dispatched(client, settings)
    response = await client.post(
        "/api/v1/feishu/webhook",
        json=feishu_text_event(
            open_id=BACKEND_SUPERVISOR,
            text="我已经筛选审核完毕，请通知张明来公司面试",  # 有姓名、无时间
            message_id="om_missing_time",
        ),
    )
    body = response.json()
    assert body["handled"] is False
    assert body["detail"]["action"] == "missing_time"

    outbox = read_outbox(settings)
    notices = [m for m in outbox if "面试通知" in m.subject]
    assert notices == [], "缺时间时绝不允许发出面试通知邮件（避免发错时间）"


async def test_supervisor_missing_name_does_not_send_notice(client, settings) -> None:
    """缺候选人姓名：只给时间不给姓名 → 回飞书说明格式，绝不向未知人选发邮件。

    守护不变量：名单必须来自本轮已发的候选人，绝不允许把时间套到“猜出来的人”身上。
    """
    await _seed_one_dispatched(client, settings)
    response = await client.post(
        "/api/v1/feishu/webhook",
        json=feishu_text_event(
            open_id=BACKEND_SUPERVISOR,
            text="下周三下午14:00来公司面试",  # 有时间、无姓名
            message_id="om_missing_name",
        ),
    )
    body = response.json()
    assert body["handled"] is False
    assert body["detail"]["action"] == "unparsed"

    outbox = read_outbox(settings)
    notices = [m for m in outbox if "面试通知" in m.subject]
    assert notices == [], "缺姓名时绝不允许向未知人选发出面试通知邮件"


# ----------------------------------------------------------------- 2. 重复简历及附件


async def test_duplicate_resume_not_ingested_twice(services) -> None:
    """同一份简历（相同邮箱 + 手机号）二次投递：返回 is_duplicate，数据库只留 1 条，并追加 candidate.duplicate 事件。

    守护不变量：重复投递/附件不得产生重复候选人记录（避免主管端出现“双份”与重复通知）。
    """
    data = resume_text(name="重复员", email="dup@example.com", phone="13899990000").encode("utf-8")

    first = await services.ingestion.ingest_resume_bytes(data, "dup.txt", source="manual")
    assert first.created is True
    assert first.is_duplicate is False

    second = await services.ingestion.ingest_resume_bytes(data, "dup.txt", source="manual")
    assert second.created is False
    assert second.is_duplicate is True

    items, total = await CandidateRepository(services.session).search(name="重复员")
    assert total == 1, "重复投递不得新增候选人记录"

    events = await EventRepository(services.session).list_for_object("candidate", items[0].id)
    assert any(e.action == "candidate.duplicate" for e in events), "必须追加去重事件留痕"


# ----------------------------------------------------------------- 3. 附件损坏 / 无法解析


async def test_empty_resume_rejected(settings) -> None:
    """边界输入：空简历内容必须被显式拒绝（而非静默入库）。"""
    parser = ResumeParser(settings)
    with pytest.raises(ValidationFailed):
        await parser.save_and_parse(b"", "empty.txt")


async def test_missing_file_rejected(settings) -> None:
    """边界输入：文件不存在必须抛 ValidationFailed（而非崩溃）。"""
    parser = ResumeParser(settings)
    with pytest.raises(ValidationFailed):
        await parser.parse_path("/nonexistent/path/resume.pdf")


async def test_unstructured_failure_falls_back_gracefully(settings, monkeypatch, tmp_path: Path) -> None:
    """风险区保护：绝不投喂真实损坏文件（会触发 unstructured 原生崩溃、进程级、不可捕获）。

    这里用 monkeypatch 模拟 unstructured 不可用/失败，验证降级链：
    unstructured 失败 → 不崩溃 → 落到后续引擎 → 文本为空但有 warnings 留痕。

    守护不变量：解析层任何引擎失败都不能中断批处理，必须降级并留痕（I-10 精神）。
    """
    parser = ResumeParser(settings)
    # 模拟 unstructured 抽不出内容（未安装或失败），让代码走后续降级分支
    monkeypatch.setattr("client.resume_parser._try_unstructured", lambda path, warnings: "")

    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 this is not a real pdf")
    result = await parser.parse_path(broken)  # 不应 raise

    assert isinstance(result, ParsedResume)
    assert result.engine != "unstructured", "unstructured 失败时必须降级到后续引擎"
    assert result.warnings, "降级路径必须保留 warnings 留痕（不静默吞掉失败）"


# ----------------------------------------------------------------- 4. 乱序信息（正向对照，抽取层已被 eval 覆盖）


async def test_out_of_order_schedule_still_notifies(client, settings) -> None:
    """乱序：时间在前、姓名在后（‘下周三张明来面试’）在反馈路径下仍能正确抽取并发送通知。

    与上面的“缺姓名/缺时间”形成对照：信息齐全但顺序颠倒时，系统不应误判为含糊指令。
    """
    await _seed_one_dispatched(client, settings)
    response = await client.post(
        "/api/v1/feishu/webhook",
        json=feishu_text_event(
            open_id=BACKEND_SUPERVISOR,
            text="下周三张明来面试",  # 时间在前、姓名在后
            message_id="om_out_of_order",
        ),
    )
    body = response.json()
    assert body["handled"] is True
    assert body["detail"]["action"] == "interview_scheduled"
    assert body["detail"]["notices_sent"] == 1

    outbox = read_outbox(settings)
    notices = [m for m in outbox if "面试通知" in m.subject]
    assert len(notices) == 1, "乱序消息也应正确发送一封面试通知"
