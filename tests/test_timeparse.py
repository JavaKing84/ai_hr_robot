"""中文时间解析测试（主管的「下周三下午 14:00」是闭环里最容易出错的一环）。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core.timeparse import (
    cn_to_int,
    format_interview_time,
    parse_chinese_datetime,
    to_local,
    to_utc_naive,
)

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 3, 15, 53, tzinfo=TZ)  # 2026-11-03 是周二


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("请通知张三、李四，下周三下午14:00来公司面试", datetime(2026, 11, 11, 14, 0)),
        ("请通知王小明 赵敏 于下周三下午14:00点来公司面试", datetime(2026, 11, 11, 14, 0)),
        ("周三上午10点", datetime(2026, 11, 4, 10, 0)),
        ("11月12日下午2点", datetime(2026, 11, 12, 14, 0)),
        ("明天下午3点", datetime(2026, 11, 4, 15, 0)),
        ("下周二上午十点", datetime(2026, 11, 10, 10, 0)),
        ("2026-11-20 09:30", datetime(2026, 11, 20, 9, 30)),
    ],
)
def test_parse_chinese_datetime(text: str, expected: datetime) -> None:
    parsed = parse_chinese_datetime(text, now=NOW, tz="Asia/Shanghai")
    assert parsed is not None
    assert parsed.replace(tzinfo=None) == expected


def test_past_weekday_rolls_to_next_week() -> None:
    friday = datetime(2026, 11, 6, 10, 0, tzinfo=TZ)
    parsed = parse_chinese_datetime("本周三上午10点", now=friday, tz="Asia/Shanghai")
    assert parsed is not None
    assert parsed.date() == datetime(2026, 11, 11).date()


def test_time_only_rolls_to_tomorrow_when_past() -> None:
    parsed = parse_chinese_datetime("下午3点半", now=NOW, tz="Asia/Shanghai")
    assert parsed is not None
    assert parsed.date() == datetime(2026, 11, 4).date()
    assert (parsed.hour, parsed.minute) == (15, 30)


def test_no_time_information_returns_none() -> None:
    assert parse_chinese_datetime("请通知张三", now=NOW, tz="Asia/Shanghai") is None
    assert parse_chinese_datetime("", now=NOW, tz="Asia/Shanghai") is None


def test_date_without_time_uses_default_hour() -> None:
    parsed = parse_chinese_datetime("下周三来面试", now=NOW, tz="Asia/Shanghai")
    assert parsed is not None
    assert (parsed.hour, parsed.minute) == (10, 0)


def test_utc_round_trip_and_display() -> None:
    parsed = parse_chinese_datetime("下周三下午14:00", now=NOW, tz="Asia/Shanghai")
    assert parsed is not None
    stored = to_utc_naive(parsed)
    assert stored.hour == 6  # 14:00 +08:00 → 06:00 UTC
    assert to_local(stored, "Asia/Shanghai").hour == 14
    assert format_interview_time(stored, "Asia/Shanghai") == "2026年11月11日（周三）14:00"


def test_format_empty_time() -> None:
    assert format_interview_time(None) == "时间待定"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("十四", 14), ("二十", 20), ("二十三", 23), ("十", 10), ("两", 2), ("9", 9), ("三十一", 31)],
)
def test_cn_to_int(text: str, expected: int) -> None:
    assert cn_to_int(text) == expected


def test_reschedule_time_prefers_target_clause() -> None:
    """「下周二…改到周四下午3点」必须解析成周四，而不是句子开头的下周二。"""
    from core.timeparse import parse_reschedule_time

    parsed = parse_reschedule_time("我下周二临时有会，能否改到周四下午 3 点？", now=NOW, tz="Asia/Shanghai")
    assert parsed is not None
    assert parsed.date() == datetime(2026, 11, 5).date()
    assert (parsed.hour, parsed.minute) == (15, 0)


def test_reschedule_time_falls_back_to_any_expression() -> None:
    from core.timeparse import parse_reschedule_time

    parsed = parse_reschedule_time("周四下午三点可以吗", now=NOW, tz="Asia/Shanghai")
    assert parsed is not None
    assert parsed.date() == datetime(2026, 11, 5).date()
