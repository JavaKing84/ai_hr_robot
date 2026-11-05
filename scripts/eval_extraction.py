"""LLM 抽取量化评估（§4-A.2：few-shot / 测试集 / 准确率 / 复盘）。

离线（无 LLM key）自动走确定性规则抽取主路径；有 key 时走 LLM 路径。
本脚本统计：字段级精确匹配准确率 + 姓名接地命中率，并打印对比表。

    python scripts/eval_extraction.py

注意：评估集 EVAL_CASES 同时被 tests/test_extraction_eval.py 复用（共享人工标注）。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from zoneinfo import ZoneInfo

from client.resume_parser import ResumeParser
from schema.message import InboundMessage
from scripts._bootstrap import bootstrap
from service.extraction import ExtractionService

TZ = ZoneInfo("Asia/Shanghai")
# 固定“当前时间”，保证时间解析确定性（§7.3：涉及时间的用固定 now=）
NOW = datetime(2026, 11, 3, 15, 53, tzinfo=TZ)

# 名单（评估集里的姓名均来自这里；不在名单里的词不得被当作姓名）
ROSTER = ["张明", "张三", "李四", "王小明", "赵敏", "刘洋", "陈静", "周涛", "孙强"]

# ----------------------------------------------------------------- 人工标注评估集
# kind: "interview"（主管安排）/ "reply"（候选人回信）
# expected_names / expected_time：interview 的字段级真值
# expected_intent / expected_proposed：reply 的字段级真值
# grade_time=False：该样本时间客观上无法唯一标注，不计入时间分项分母（但仍计入姓名/意图）
EVAL_CASES: list[dict] = [
    # ---- 主管面试安排（含乱序 / 含糊 / 夹杂噪声 / 接地排除 / 规则弱项）----
    dict(kind="interview", note="常规",
         text="请通知张明、李四，下周三下午14:00来公司面试",
         expected_names=["张明", "李四"], expected_time=datetime(2026, 11, 11, 14, 0, tzinfo=TZ)),
    dict(kind="interview", note="姓名带'和'分隔",
         text="请通知张三和李四，下周三下午两点来面试",
         expected_names=["张三", "李四"], expected_time=datetime(2026, 11, 11, 14, 0, tzinfo=TZ)),
    dict(kind="interview", note="姓名用空格分隔",
         text="我已经筛选完毕，请通知王小明 赵敏 于下周三下午14:00点来公司面试",
         expected_names=["王小明", "赵敏"], expected_time=datetime(2026, 11, 11, 14, 0, tzinfo=TZ)),
    dict(kind="interview", note="夹杂'12份/3人'噪声（必须不误判为姓名）",
         text="这12份我只留了4份，请通知刘洋、陈静、周涛3人于下周二上午10点到公司面试",
         expected_names=["刘洋", "陈静", "周涛"], expected_time=datetime(2026, 11, 10, 10, 0, tzinfo=TZ)),
    dict(kind="interview", note="乱序：时间在前、姓名在后、无小时（默认10:00）",
         text="下周三张三来面试",
         expected_names=["张三"], expected_time=datetime(2026, 11, 11, 10, 0, tzinfo=TZ)),
    dict(kind="interview", note="乱序：时间夹在中间",
         text="下周三下午两点张明和李四来面试",
         expected_names=["张明", "李四"], expected_time=datetime(2026, 11, 11, 14, 0, tzinfo=TZ)),
    dict(kind="interview", note="含糊：只说'下周'无具体时间 → null",
         text="请通知张明李四下周来面试",
         expected_names=["张明", "李四"], expected_time=None),
    dict(kind="interview", note="下周五",
         text="请通知孙强，下周五上午9点面试",
         expected_names=["孙强"], expected_time=datetime(2026, 11, 13, 9, 0, tzinfo=TZ)),
    dict(kind="interview", note="接地排除：名单外的'小李'必须被丢弃",
         text="请通知赵敏和那个新来的小李，下周三面试",
         expected_names=["赵敏"], expected_time=datetime(2026, 11, 11, 10, 0, tzinfo=TZ)),
    dict(kind="interview", note="规则弱项：'下个月5号'无法解析 → 误差样本",
         text="通知张明，下个月5号上午十点面试",
         expected_names=["张明"], expected_time=datetime(2026, 12, 5, 10, 0, tzinfo=TZ)),
    dict(kind="interview", note="规则弱项：双时间坍缩为单一 → 时间不计入分母", grade_time=False,
         text="张明下周三、李四下周四都来面试",
         expected_names=["张明", "李四"], expected_time=None),

    # ---- 候选人回信（含改期 / 放弃 / 确认 / 含糊）----
    dict(kind="reply", note="改期：'改到周四下午3点'",
         text="您好，我下周二临时有会，能否改到周四下午 3 点？",
         expected_intent="reschedule", expected_proposed=datetime(2026, 11, 5, 15, 0, tzinfo=TZ)),
    dict(kind="reply", note="放弃：'找到工作/不参加'",
         text="不好意思，我已经找到工作了，这次就不参加了。",
         expected_intent="decline", expected_proposed=None),
    dict(kind="reply", note="确认：'准时参加'",
         text="收到，我会准时参加面试，谢谢！",
         expected_intent="confirm", expected_proposed=None),
    dict(kind="reply", note="改期但含糊：'换个时间'无新时间",
         text="抱歉时间恐怕冲突了，我想换个时间",
         expected_intent="reschedule", expected_proposed=None),
    dict(kind="reply", note="放弃含糊：'去不了了'",
         text="我有点事去不了了",
         expected_intent="decline", expected_proposed=None),
    dict(kind="reply", note="改期：'改到下周五上午9点'",
         text="下周三我出差，能改到下周五上午9点吗",
         expected_intent="reschedule", expected_proposed=datetime(2026, 11, 13, 9, 0, tzinfo=TZ)),
]


def _time_key(dt: datetime | None) -> tuple | None:
    """时间只比对 日期 + 时 + 分（忽略秒与微秒），符合业务精度。"""
    if dt is None:
        return None
    return (dt.year, dt.month, dt.day, dt.hour, dt.minute)


def _message(text: str) -> InboundMessage:
    return InboundMessage(
        channel="feishu",  # type: ignore[arg-type]
        sender_id="ou_eval",
        text=text,
        received_at=NOW.replace(tzinfo=None),
        message_id="eval-msg",
    )


async def _run(service: ExtractionService) -> list[dict]:
    rows: list[dict] = []
    for case in EVAL_CASES:
        row = dict(case)
        if case["kind"] == "interview":
            sched = await service.extract_interview_schedule(
                _message(case["text"]), roster=ROSTER, now=NOW
            )
            got_names = list(sched.candidate_names)
            got_time = sched.interview_time
            row["got_names"] = got_names
            row["got_time"] = got_time
            row["grounded"] = all(n in ROSTER for n in got_names)
            row["names_ok"] = set(got_names) == set(case["expected_names"])
            grade_time = case.get("grade_time", True)
            row["time_ok"] = (None if not grade_time else (_time_key(got_time) == _time_key(case["expected_time"])))
            row["time_graded"] = grade_time
        else:
            reply = await service.extract_candidate_reply(
                case["text"], candidate_name=case.get("expected_names", [""])[0], now=NOW
            )
            row["got_intent"] = reply.intent
            row["got_proposed"] = reply.proposed_time
            row["intent_ok"] = reply.intent == case["expected_intent"]
            row["proposed_ok"] = _time_key(reply.proposed_time) == _time_key(case["expected_proposed"])
        rows.append(row)
    return rows


def _print_report(rows: list[dict]) -> None:
    line = "─" * 92
    print(line)
    print(f"{'#':<3}{'类型':<9}{'样本说明':<30}{'字段比对':<34}{'接地'}")
    print(line)
    for i, r in enumerate(rows, 1):
        if r["kind"] == "interview":
            name_part = f"姓名 {r['got_names']}"
            time_part = "时间 不计" if not r["time_graded"] else f"时间 {r['time_ok']}"
            cmp = f"{r['names_ok']} | {time_part}"
            grd = "✓" if r["grounded"] else "✗"
        else:
            cmp = f"意图 {r['intent_ok']} | 新时间 {r['proposed_ok']}"
            grd = "-"
        print(f"{i:<3}{r['kind']:<9}{r['note']:<28}{cmp:<36}{grd}")
    print(line)

    # 聚合准确率（按字段分别统计，只在本 kind 内计数，避免跨样本错算分母）
    per_field: dict[str, list[int]] = {}  # 字段名 -> [命中, 总数]
    grounded_total = grounded_hit = 0

    def _acc(key: str, ok: bool) -> None:
        bucket = per_field.setdefault(key, [0, 0])
        bucket[1] += 1
        if ok:
            bucket[0] += 1

    for r in rows:
        if r["kind"] == "interview":
            grounded_total += 1
            grounded_hit += 1 if r["grounded"] else 0
            _acc("姓名", bool(r["names_ok"]))
            if r["time_graded"]:
                _acc("时间", bool(r["time_ok"]))
        else:
            _acc("意图", bool(r["intent_ok"]))
            _acc("新时间", bool(r["proposed_ok"]))

    print("\n字段级精确匹配准确率（按字段）：")
    all_hit = all_tot = 0
    for fname in ("姓名", "时间", "意图", "新时间"):
        hit, tot = per_field.get(fname, [0, 0])
        all_hit += hit
        all_tot += tot
        pct = (hit / tot * 100) if tot else 0.0
        print(f"  · {fname:<6} {hit}/{tot} = {pct:5.1f}%")
    overall = (all_hit / all_tot * 100) if all_tot else 0.0
    print(f"  · {'整体':<6} {all_hit}/{all_tot} = {overall:5.1f}%")
    grd_rate = (grounded_hit / grounded_total * 100) if grounded_total else 0.0
    print(f"\n姓名接地命中率（抽取姓名全部来自名单）：{grounded_hit}/{grounded_total} = {grd_rate:5.1f}%")
    print(line)


async def main() -> None:
    container = bootstrap()
    settings = container.settings
    # 显式不传 LLM → 走确定性规则抽取主路径（离线可复现，不依赖任何外部 key）
    service = ExtractionService(settings, llm=None, parser=ResumeParser(settings))
    print(f"抽取路径：rule（确定性规则，离线主路径）｜用例数：{len(EVAL_CASES)}｜固定 now={NOW}")
    rows = await _run(service)
    _print_report(rows)


if __name__ == "__main__":
    asyncio.run(main())
