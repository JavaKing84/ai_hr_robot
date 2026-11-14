"""岗位录入「说明 → 确认 → 生效」全链路测试。

这里锁的是三件此前没有任何测试保护的事：

1. **暂存不等于生效**：主管发一段 JD，正式配置一个字都不能动；
2. **确认才生效**：HR 回「确认岗位」后草稿才落进 ``positions.d`` 并同步进数据库；
3. **「确认」不抢面试通知**：同一个词在两处都用，必须靠关键词优先级分开，
   否则 HR 想确认岗位、结果把待发的面试通知全发给候选人了。

另外顺手钉住本轮修的两个 bug：
- 草稿同 code 时**覆盖**主配置（以前是 continue，改了等于没改）；
- 岗位 code 不能退化成单个数字（以前「3 年经验的产品经理」和「3 年经验的后端」
  都得到 code="3"，后录的盖掉先录的）。
"""

from __future__ import annotations

import shutil
import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from config.settings import Settings
from model.candidate import Candidate, CandidateStatus
from model.message import Message
from model.position import Position
from schema.agent import AgentIntent, AgentPlan
from schema.config import PositionConfig, ScreeningConfig
from schema.message import InboundMessage
from service import job_intake as job_intake_module
from service.job_intake import JobIntakeService, describe_pending

NOW = datetime(2026, 11, 14, 10, 0, 0)
JD_TEXT = "高级测试工程师，本科以上学历，3 年以上经验，熟悉 pytest 与接口自动化，负责测试平台建设"


# ---------------------------------------------------------------- 夹具与构造工具


@pytest.fixture
def job_dirs(tmp_path, settings, monkeypatch):
    """把「主配置」和「草稿目录」都搬到 tmp，别让测试往仓库里写文件。

    只 monkeypatch ``DRAFT_DIR`` 是不够的：``load_company_config()`` 读的是
    ``settings.config_path.parent / "positions.d"``，两处必须指向同一个 tmp 目录，
    否则「写完草稿 → 同步进数据库」这一段永远看不到文件。
    """
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    shutil.copy(settings.config_path, cfg_dir / "positions.yaml")
    draft_dir = cfg_dir / "positions.d"

    monkeypatch.setattr(job_intake_module, "DRAFT_DIR", draft_dir)
    monkeypatch.setattr(settings, "config_path", cfg_dir / "positions.yaml")
    return draft_dir


def _supervisor_open_id(settings: Settings, code: str = "backend_senior") -> str:
    for department in settings.load_company_config().departments:
        if any(position.code == code for position in department.positions):
            return department.supervisor.feishu_open_id
    raise AssertionError(f"配置里找不到 {code} 所属部门的主管")


def _feishu(settings: Settings, text: str) -> InboundMessage:
    return InboundMessage(
        channel="feishu",
        sender_id=_supervisor_open_id(settings),
        sender_name="主管",
        text=text,
        message_id=f"om_job_{uuid.uuid4().hex[:10]}",
        received_at=NOW,
    )


def _position(
    code: str = "qa_engineer",
    title: str = "测试工程师",
    *,
    department: str = "技术部",
    threshold: float = 60,
    keywords: list[str] | None = None,
) -> PositionConfig:
    return PositionConfig(
        code=code,
        title=title,
        department=department,
        headcount=2,
        description="负责测试平台建设",
        keywords=keywords or ["pytest", "自动化"],
        screening=ScreeningConfig(
            threshold=threshold,
            rule_version="v1-ai",
            hard_conditions=[
                {
                    "field": "education_level",
                    "operator": "gte",
                    "value": "本科",
                    "label": "学历本科及以上",
                }
            ],
            weighted=[],
        ),
    )


def _stager(settings: Settings) -> JobIntakeService:
    return JobIntakeService(settings=settings, llm=None)


async def _stage(settings: Settings, position: PositionConfig) -> None:
    _stager(settings).stage(position)


async def _plan_job_posting(monkeypatch, services) -> None:
    """让路由直接把这句话判成 job_posting（这里不测模型，只测分流后的行为）。"""

    async def fake_plan(inbound, **kwargs):
        return AgentPlan(intent=AgentIntent.JOB_POSTING, confidence=0.9, method="llm")

    monkeypatch.setattr(services.feedback.agent, "plan", fake_plan)


async def _events(services, action: str) -> list:
    rows = await services.events.list_recent(limit=200)
    return [row for row in rows if row.action == action]


# ---------------------------------------------------------------- A. 暂存 ≠ 生效


async def test_job_posting_stages_without_touching_config(
    services, settings, job_dirs, monkeypatch
) -> None:
    """主管发 JD 后：暂存目录有文件，正式目录与数据库都不该有任何变化。"""
    await _plan_job_posting(monkeypatch, services)

    outcome = await services.feedback.handle(_feishu(settings, JD_TEXT))

    assert outcome.handled is True
    assert outcome.action == "job_intake_staged"
    assert list(job_dirs.glob("*.yaml")) == [], "确认前不能往正式草稿目录写任何文件"
    assert len(list((job_dirs / "_pending").glob("*.yaml"))) == 1
    assert await services.positions.get_by_code(outcome.detail["code"]) is None, (
        "确认前数据库里不该出现这个岗位"
    )


async def test_job_posting_reply_is_a_preview_not_a_receipt(
    services, settings, job_dirs, monkeypatch
) -> None:
    """回复必须是「请确认」+ 可核对的字段，不能是「已录入」。"""
    await _plan_job_posting(monkeypatch, services)

    outcome = await services.feedback.handle(_feishu(settings, JD_TEXT))

    text = outcome.reply_text
    assert "确认新增岗位" in text
    assert "取消" in text, "必须给出取消的办法，否则 HR 只能将错就错"
    assert "硬性条件" in text and "及格线" in text, "要摊开给 HR 核对"
    assert "已录入" not in text, "还没生效就说已录入，正是本轮要消灭的假成功"


async def test_job_posting_reports_modification_when_code_exists(
    services, settings, job_dirs, monkeypatch
) -> None:
    """同 code 等于改已有岗位，文案要说「修改」而不是「新增」。"""
    existing = _position(code="qa_engineer")
    _stager(settings).apply(existing)  # 先让正式草稿里有这个 code

    staged = _stager(settings).stage(existing)
    assert staged.exists()
    exists = any(
        p.code == "qa_engineer"
        for p in services.feedback.settings.load_company_config().positions
    )
    assert exists, "夹具自检：正式草稿应已被合并进配置"
    assert describe_pending(existing, exists=True).startswith("确认修改岗位")


async def test_job_posting_failure_does_not_stage_anything(
    services, settings, job_dirs, monkeypatch
) -> None:
    """读不出岗位要求时只回提示，不留下半成品草稿。"""
    await _plan_job_posting(monkeypatch, services)

    outcome = await services.feedback.handle(_feishu(settings, "嗯"))

    assert outcome.handled is False
    assert outcome.action == "job_intake_failed"
    assert list((job_dirs / "_pending").glob("*.yaml")) == []


async def test_rule_fallback_is_disclosed_to_hr(settings) -> None:
    """模型没响应、结果退回规则拼装时，必须明说，不能冒充模型抽取。

    2026-11-14 真实环境实测到：模型供应商偶发失败后，规则兜底给出的岗位名
    是「整句话」、部门是默认的技术部、人数是 1——形式完整、内容离谱。
    不加这句提示，HR 会以为自己核对的是模型的输出。
    """

    class BrokenLLM:
        async def complete_json(self, **kwargs):
            raise RuntimeError("provider down")

    service = JobIntakeService(settings=settings, llm=BrokenLLM())
    position = await service.parse(JD_TEXT)

    assert service.last_source == "rule"
    assert "模型这次没响应" in describe_pending(position, source=service.last_source)


def test_llm_source_has_no_warning() -> None:
    """正常走模型时不加这段提示，否则每句话都在道歉。"""
    assert "模型这次没响应" not in describe_pending(_position(), source="llm")


# ---------------------------------------------------------------- B. 确认后生效


async def test_confirm_activates_staged_position(
    services, settings, job_dirs
) -> None:
    """回「确认」后：正式草稿出现、暂存清空、数据库里有这个岗位。"""
    await _stage(settings, _position())

    outcome = await services.feedback.handle(_feishu(settings, "确认"))

    assert outcome.handled is True
    assert outcome.action == "job_intake"
    assert (job_dirs / "qa_engineer.yaml").exists()
    assert job_intake_module.JobIntakeService(settings=settings, llm=None).staged_codes() == []
    row = await services.positions.get_by_code("qa_engineer")
    assert row is not None and row.title == "测试工程师"
    assert row.screening["threshold"] == 60


async def test_confirm_uses_the_very_file_hr_saw(
    services, settings, job_dirs
) -> None:
    """确认时读暂存文件，不重新调模型——否则 HR 确认的和生效的可能不是同一份。"""
    await _stage(settings, _position(threshold=60))
    path = job_intake_module.pending_dir() / "qa_engineer.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("threshold: 60", "threshold: 42"),
        encoding="utf-8",
    )

    await services.feedback.handle(_feishu(settings, "确认岗位"))

    row = await services.positions.get_by_code("qa_engineer")
    assert row.screening["threshold"] == 42


async def test_confirm_records_event_and_clears_pending(
    services, settings, job_dirs
) -> None:
    """生效要留痕：事件表里有 position.intaken，且没有残留的暂存。"""
    await _stage(settings, _position())

    await services.feedback.handle(_feishu(settings, "确认岗位"))

    assert await _events(services, "position.intaken"), "确认生效必须留事件，方便回溯是谁改的配置"
    assert job_intake_module.JobIntakeService(settings=settings, llm=None).staged_codes() == []


async def test_confirm_without_staged_draft_is_not_intercepted(
    services, settings, job_dirs
) -> None:
    """没有待确认岗位时，「确认」不能被我这段逻辑吃掉（要留给面试通知）。"""
    outcome = await services.feedback.handle(_feishu(settings, "确认"))
    assert outcome.action != "job_intake"
    assert outcome.action != "job_intake_discarded"


# ---------------------------------------------------------------- C. 取消


async def test_cancel_discards_and_keeps_config_intact(
    services, settings, job_dirs
) -> None:
    """回「取消岗位」：暂存删掉，正式配置一个字没动。"""
    await _stage(settings, _position())

    outcome = await services.feedback.handle(_feishu(settings, "取消岗位"))

    assert outcome.action == "job_intake_discarded"
    assert job_intake_module.JobIntakeService(settings=settings, llm=None).staged_codes() == []
    assert list(job_dirs.glob("*.yaml")) == []
    assert await services.positions.get_by_code("qa_engineer") is None
    assert await _events(services, "position.intake_discarded")


async def test_generic_cancel_goes_to_job_when_nothing_pending(
    services, settings, job_dirs
) -> None:
    """没有待发面试通知时，单独一个「取消」就是取消岗位草稿。"""
    await _stage(settings, _position())

    outcome = await services.feedback.handle(_feishu(settings, "取消"))

    assert outcome.action == "job_intake_discarded"


async def test_cancel_without_staged_draft_is_noop(
    services, settings, job_dirs
) -> None:
    """没有草稿时「取消岗位」不该报错，也不该改任何东西。"""
    outcome = await services.feedback.handle(_feishu(settings, "取消岗位"))
    assert outcome.action != "job_intake_discarded"


# ---------------------------------------------------------------- D. 关键词优先级


async def _parked_interview_notice(services, settings) -> None:
    """制造一份「待 HR 确认」的面试通知，用来验证「确认」归谁。"""
    settings.interview_notice_require_confirm = True
    position = (
        await services.session.execute(select(Position).where(Position.code == "backend_senior"))
    ).scalars().one()
    candidate = Candidate(
        name="待确认候选人",
        email="pending@example.com",
        phone="13900001111",
        education_level="本科",
        school="华北电力大学",
        major="计算机科学与技术",
        years_experience=6.0,
        position_id=position.id,
        position_code=position.code,
        status=CandidateStatus.DISPATCHED,
        score=88.0,
    )
    services.session.add(candidate)
    await services.session.flush()
    result = await services.notification.send_interview_notice(
        candidate, position, interview_time=NOW, location="北京市海淀区"
    )
    assert result.pending is True, "夹具自检：通知应处于待确认状态"
    assert await services.notification.pending_interview_notices()


async def test_confirm_position_keyword_wins_over_interview_notice(
    services, settings, job_dirs
) -> None:
    """两件事都挂着时，「确认岗位」只确认岗位，不能顺手把面试通知发出去。"""
    await _parked_interview_notice(services, settings)
    await _stage(settings, _position())

    outcome = await services.feedback.handle(_feishu(settings, "确认岗位"))

    assert outcome.action == "job_intake"
    assert await services.positions.get_by_code("qa_engineer") is not None
    assert await services.notification.pending_interview_notices(), (
        "面试通知必须还在待确认状态——HR 说的是岗位"
    )


async def test_generic_confirm_still_goes_to_interview_notice(
    services, settings, job_dirs
) -> None:
    """有待发面试通知时，单独「确认」维持既有行为，岗位草稿不受影响。"""
    await _parked_interview_notice(services, settings)
    await _stage(settings, _position())

    outcome = await services.feedback.handle(_feishu(settings, "确认"))

    assert outcome.action == "interview_notice_confirmed"
    assert await services.positions.get_by_code("qa_engineer") is None
    assert job_intake_module.JobIntakeService(settings=settings, llm=None).staged_codes(), (
        "岗位草稿应当原样保留，等 HR 明确说「确认岗位」"
    )


# ---------------------------------------------------------------- E. 草稿覆盖主配置


async def test_draft_overrides_main_config_with_same_code(settings, job_dirs) -> None:
    """同 code 的草稿必须盖掉主配置——这是本轮修的 bug。"""
    before = settings.load_company_config().position_by_code("backend_senior")
    assert before.title == "高级后端工程师"

    _stager(settings).apply(
        _position(code="backend_senior", title="高级后端工程师（改）", threshold=70)
    )
    after = settings.load_company_config().position_by_code("backend_senior")

    assert after.title == "高级后端工程师（改）"
    assert after.screening.threshold == 70, "改了阈值必须真的生效，否则就是假成功"
    assert len(settings.load_company_config().positions) == 5, "不能多出一个同 code 的重复岗位"


async def test_draft_moves_position_between_departments(settings, job_dirs) -> None:
    """草稿把岗位调到别的部门时，旧部门要摘掉，不能两边各留一份。"""
    _stager(settings).apply(
        _position(code="backend_senior", title="高级后端工程师", department="产品部")
    )
    config = settings.load_company_config()
    hits = [d.name for d in config.departments for p in d.positions if p.code == "backend_senior"]

    assert hits == ["产品部"]
    assert len(config.positions) == 5


async def test_pending_subdir_is_never_read_as_real_position(settings, job_dirs) -> None:
    """_pending 是待确认区，合并配置时必须无视它。"""
    await _stage(settings, _position(code="should_not_exist"))

    config = settings.load_company_config()

    assert config.position_by_code("should_not_exist") is None
    assert len(config.positions) == 5


# ---------------------------------------------------------------- F. code 稳定性


def test_slug_does_not_collapse_to_digits() -> None:
    """「3 年经验的产品经理」和「3 年经验的后端」不能撞成同一个 code。"""
    first = job_intake_module._slug("3年以上经验的产品经理")
    second = job_intake_module._slug("3年以上经验的后端工程师")

    assert first != second
    assert not first.isdigit(), "纯数字不能当岗位标识"


def test_slug_is_stable_across_calls() -> None:
    assert job_intake_module._slug("高级后端工程师") == job_intake_module._slug("高级后端工程师")
    assert job_intake_module._slug("Backend Engineer II") == "backend_engineer_ii"
