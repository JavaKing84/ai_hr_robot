"""职位匹配失败兜底（§4-A.1 子任务 2）：无法归位时降级入库 + 飞书告警，不抛中断异常。

完全离线（飞书 mock / LLM off），确定性，独立。
保护的不变量：
- 不修改 CandidateStatus 的 10 个枚举（§3.2 / 附录 A）——未归位者停留在 received；
- 不抛 400 中断批处理（I-10 精神：单点异常不阻断整轮）；
- 每次状态变化追加 event（I-09）——补 candidate.received + candidate.unmatched_position；
- 飞书发送逻辑在 service 层（分层红线 §3.4）。
"""

from __future__ import annotations

import os

from client.feishu import FeishuSendResult
from model.candidate import Candidate, CandidateStatus
from repository.candidate import CandidateRepository
from service.ingestion import PARSE_FAILED_ENGINE

# 一份「确实无法归位」的简历：岗位名与所有技能/专业都不命中 positions.yaml 任何关键词，
# 且系统存在多个在招职位（len(positions) != 1），因此 _resolve_position 必返回 None。
UNMATCHED_RESUME = (
    "姓名：测试员\n"
    "邮箱：tester@example.com\n"
    "电话：13900000000\n"
    "学历：本科\n"
    "专业：哲学\n"
    "工作年限：2年\n"
    "技能：围棋、书法、茶艺\n"
    "求职意向：古籍修复师\n"
)


async def test_unmatched_resume_stored_and_feishu_alerted(services, monkeypatch) -> None:
    """无法归位：① 不抛异常 ② 数据库新增 1 条（position_id=None, status=received）③ 触发飞书告警。"""
    services.ingestion.hr_alert_open_id = "ou_hr_test"  # 固定已知接收人，精确断言

    sent: list[tuple[str, str]] = []

    async def spy_send_text(open_id: str, text: str) -> FeishuSendResult:
        sent.append((open_id, text))
        return FeishuSendResult(ok=True, mode="mock", message_id="mock-1")

    monkeypatch.setattr(services.ingestion.feishu, "send_text", spy_send_text)

    outcome = await services.ingestion.ingest_resume_bytes(
        UNMATCHED_RESUME.encode("utf-8"), "tester.txt", source="manual"
    )

    # ① 不抛异常（前序未 raise 即成立）；返回有效 outcome 且含 candidate
    assert outcome.candidate is not None
    assert outcome.is_duplicate is False

    # ② 数据库新增了记录，且未归位（position_id=None, status=received, position_code="")
    items, total = await CandidateRepository(services.session).search(name="测试员")
    assert total == 1
    cand = items[0]
    assert cand.position_id is None
    assert cand.status == CandidateStatus.RECEIVED
    assert cand.position_code == ""

    # ③ 触发了飞书告警：发给指定接收人，内容含候选人姓名与「无法自动归位」
    assert sent, "应当触发飞书告警消息"
    assert any(open_id == "ou_hr_test" for open_id, _ in sent)
    assert any("无法自动归位" in text and "测试员" in text for _, text in sent)


async def test_no_active_positions_still_stored_without_feishu_alert(services, monkeypatch) -> None:
    """即使没有在招职位（配置为空），也不抛异常、仍留档，且因无接收人不发飞书。"""
    async def empty_positions():
        return []

    monkeypatch.setattr(services.ingestion.positions, "list_active", empty_positions)
    services.ingestion.hr_alert_open_id = ""  # 模拟无 HR open_id

    sent: list[tuple[str, str]] = []

    async def spy_send_text(open_id: str, text: str) -> FeishuSendResult:
        sent.append((open_id, text))
        return FeishuSendResult(ok=True, mode="mock", message_id="mock-1")

    monkeypatch.setattr(services.ingestion.feishu, "send_text", spy_send_text)

    outcome = await services.ingestion.ingest_resume_bytes(
        UNMATCHED_RESUME.encode("utf-8"), "tester2.txt", source="manual"
    )

    # 不抛异常、仍返回有效 candidate
    assert outcome.candidate is not None
    items, total = await CandidateRepository(services.session).search(name="测试员")
    assert total == 1  # 仍留档
    # 无接收人 → 不发飞书（仅日志告警）
    assert sent == []


# ---------------------------------------------------------------------------
# PDF 崩溃隔离（§4-A.1 e）：损坏 PDF 让解析子进程直接挂掉时，本轮其余简历必须照常处理。
# 保护的不变量：
# - I-10 精神：单份简历解析失败不阻断整轮（这里体现为「不抛异常、返回有效 outcome」）；
# - 失败原因必须留痕（ParsedResume.warnings + 结构化日志），否则运维无从排查。
# ---------------------------------------------------------------------------


class _CrashingResumeParser:
    """模拟「损坏 PDF 把解析器带崩」：子进程里没有任何 Python 异常，直接 os._exit。"""

    async def save_and_parse(self, data: bytes, filename: str):  # noqa: ANN201 - 测试替身
        os._exit(9)


async def test_crashing_parser_degrades_to_warnings(services, monkeypatch) -> None:
    """解析子进程崩溃 → 不抛出，降级为空文本 ParsedResume，且原因写进 warnings。"""
    monkeypatch.setattr(services.ingestion, "parser", _CrashingResumeParser())

    parsed = await services.ingestion._parse_resume_safely(b"%PDF-1.7 broken", "broken.pdf")

    assert parsed.text == ""
    assert parsed.engine == PARSE_FAILED_ENGINE
    assert parsed.warnings, "崩溃原因必须写进 warnings，否则运维无从排查"
    assert any("解析子进程失败" in w for w in parsed.warnings)


async def test_ingest_survives_parser_crash(services, monkeypatch) -> None:
    """主流程不中断：解析崩溃的简历仍走完入库（留档待人工），不抛 400 打断批处理。"""
    monkeypatch.setattr(services.ingestion, "parser", _CrashingResumeParser())
    services.ingestion.hr_alert_open_id = ""  # 无接收人 → 只走日志告警，不发飞书

    outcome = await services.ingestion.ingest_resume_bytes(
        b"%PDF-1.7 broken", "broken.pdf", source="imap"
    )

    assert outcome.candidate is not None, "解析失败也要留档，不能静默丢弃"
    assert outcome.is_duplicate is False
    assert "未归位" in outcome.note or "留档" in outcome.note


# ---------------------------------------------------------------------------
# PDF 崩溃隔离（§4-A.1 e）：损坏 PDF 让解析子进程直接挂掉时，本轮其余简历必须照常处理。
# 保护的不变量：
# - I-10 精神：单份简历解析失败不阻断整轮（这里体现为「不抛异常、返回有效 outcome」）；
# - 失败原因必须留痕（ParsedResume.warnings + 结构化日志），否则运维无从排查。
# ---------------------------------------------------------------------------


class _CrashingResumeParser:
    """模拟「损坏 PDF 把解析器带崩」：子进程里没有任何 Python 异常，直接 os._exit。"""

    async def save_and_parse(self, data: bytes, filename: str):  # noqa: ANN201 - 测试替身
        os._exit(9)


async def test_crashing_parser_degrades_to_warnings(services, monkeypatch) -> None:
    """解析子进程崩溃 → 不抛出，降级为空文本 ParsedResume，且原因写进 warnings。"""
    monkeypatch.setattr(services.ingestion, "parser", _CrashingResumeParser())

    parsed = await services.ingestion._parse_resume_safely(b"%PDF-1.7 broken", "broken.pdf")

    assert parsed.text == ""
    assert parsed.engine == PARSE_FAILED_ENGINE
    assert parsed.warnings, "崩溃原因必须写进 warnings，否则运维无从排查"
    assert any("解析子进程失败" in w for w in parsed.warnings)


async def test_ingest_survives_parser_crash(services, monkeypatch) -> None:
    """主流程不中断：解析崩溃的简历仍走完入库（留档待人工），不抛 400 打断批处理。"""
    monkeypatch.setattr(services.ingestion, "parser", _CrashingResumeParser())
    services.ingestion.hr_alert_open_id = ""  # 无接收人 → 只走日志告警，不发飞书

    outcome = await services.ingestion.ingest_resume_bytes(
        b"%PDF-1.7 broken", "broken.pdf", source="imap"
    )

    assert outcome.candidate is not None, "解析失败也要留档，不能静默丢弃"
    assert outcome.is_duplicate is False
    assert "未归位" in outcome.note or "留档" in outcome.note
