"""中文时间解析：把主管/候选人自由文本里的「下周三下午 14:00」解析成时间。

设计约束：
- 只处理面试场景常见表达（周几、明天、下周三、11 月 12 日、上午/下午 + 点/冒号）。
- 时间统一以 **naive UTC** 入库（SQLite 不保留时区，避免 naive/aware 混用），
  展示时再按 `TIMEZONE`（默认 Asia/Shanghai）本地化。
- 解析失败返回 ``None``，由调用方决定「时间待定」还是向主管追问，不抛异常。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Shanghai"
WEEKDAY_NAMES = "一二三四五六日"

_CN_DIGITS: dict[str, int] = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_WEEKDAY_INDEX: dict[str, int] = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
    "1": 0,
    "2": 1,
    "3": 2,
    "4": 3,
    "5": 4,
    "6": 5,
    "7": 6,
}

_RELATIVE_DAYS: dict[str, int] = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2, "大后天": 3}

_ISO_DATE_RE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日号]?")
_CN_MONTH_DAY_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?")
# E-01：「下个月5号」「下月十五日」——相对月份 + 具体日，独立于上面两个绝对日期正则
_NEXT_MONTH_DAY_RE = re.compile(
    r"下个?月\s*(\d{1,2}|[一二三四五六七八九十]{1,3})\s*[日号]"
)
_WEEK_RE = re.compile(r"(上上|下下|上|下|本|这)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天1-7])")
_TIME_RE = re.compile(
    r"(凌晨|早上|早晨|清晨|上午|中午|下午|傍晚|晚上|晚)?\s*"
    r"([0-9]{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*"
    r"(?:[点时時]|[:：])\s*"
    r"([0-9]{1,2}|[零〇一二两三四五六七八九十]{1,3})?\s*"
    r"(半|分)?"
)

_PERIOD_AFTERNOON = {"中午", "下午", "傍晚", "晚上", "晚"}


def cn_to_int(text: str | None) -> int | None:
    """把「14」「十四」「二十三」「两」等转成 int；无法解析返回 None。"""
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = cn_to_int(head) if head else 1
        ones = cn_to_int(tail) if tail else 0
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    value = 0
    for char in text:
        if char not in _CN_DIGITS:
            return None
        value = value * 10 + _CN_DIGITS[char]
    return value


def now_local(tz: str = DEFAULT_TZ) -> datetime:
    return datetime.now(ZoneInfo(tz))


def now_utc_naive() -> datetime:
    """统一入库时间：naive UTC。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc_naive(aware: datetime) -> datetime:
    if aware.tzinfo is None:
        return aware
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def to_local(naive_utc: datetime, tz: str = DEFAULT_TZ) -> datetime:
    if naive_utc.tzinfo is not None:
        return naive_utc.astimezone(ZoneInfo(tz))
    return naive_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz))


def format_interview_time(naive_utc: datetime | None, tz: str = DEFAULT_TZ, *, pending: str = "地点待定") -> str:
    """渲染成「2026年11月11日（周三）14:00」；空值返回“时间待定”。"""
    if naive_utc is None:
        return "时间待定"
    local = to_local(naive_utc, tz)
    return f"{local.year}年{local.month}月{local.day}日（周{WEEKDAY_NAMES[local.weekday()]}）{local:%H:%M}"


def format_datetime(naive_utc: datetime | None, tz: str = DEFAULT_TZ, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if naive_utc is None:
        return ""
    return to_local(naive_utc, tz).strftime(fmt)


def _parse_date(text: str, now: datetime) -> date | None:
    # E-01：「下个月 X 号」是主管原话里最常见、也最容易丢时间的表达。
    # 独立拦截，命中即返回；不命中则完全走下面原有分支（未改动任何既有逻辑）。
    next_month = _NEXT_MONTH_DAY_RE.search(text)
    if next_month:
        day = cn_to_int(next_month.group(1))
        if day is None:
            return None
        # 下个月 1 号：+32 天必落在下个月，再归到 1 号
        first_of_next_month = (now.date().replace(day=1) + timedelta(days=32)).replace(day=1)
        try:
            return first_of_next_month.replace(day=day)
        except ValueError:
            # 下个月没有这一天（如 2 月 30 号）→ 不猜，交给调用方向主管追问（I-01）
            return None

    match = _ISO_DATE_RE.search(text)
    if match:
        year, month, day = (int(g) for g in match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None

    match = _CN_MONTH_DAY_RE.search(text)
    if match:
        month, day = int(match.group(1)), int(match.group(2))
        year = now.year
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate < now.date() - timedelta(days=1):
            candidate = date(year + 1, month, day)
        return candidate

    for keyword, offset in _RELATIVE_DAYS.items():
        if keyword in text:
            return now.date() + timedelta(days=offset)

    match = _WEEK_RE.search(text)
    if match:
        qualifier = match.group(1) or ""
        weekday = _WEEKDAY_INDEX.get(match.group(2))
        if weekday is None:
            return None
        monday = now.date() - timedelta(days=now.weekday())
        if qualifier == "下":
            return monday + timedelta(days=7 + weekday)
        if qualifier == "下下":
            return monday + timedelta(days=14 + weekday)
        if qualifier == "上":
            return monday - timedelta(days=7 - weekday)
        if qualifier == "上上":
            return monday - timedelta(days=14 - weekday)
        target = monday + timedelta(days=weekday)
        # 「本周三」「周三」若已过去，视为下一次（下周三）
        if target < now.date():
            target += timedelta(weeks=1)
        return target

    return None


def _parse_time(text: str) -> tuple[int, int] | None:
    for match in _TIME_RE.finditer(text):
        period, hour_raw, minute_raw, minute_suffix = match.groups()
        hour = cn_to_int(hour_raw)
        if hour is None or hour > 24:
            continue
        minute = cn_to_int(minute_raw) if minute_raw else None
        if minute is None:
            minute = 30 if minute_suffix == "半" else 0
        if minute > 59:
            continue
        if period in _PERIOD_AFTERNOON and hour < 12:
            hour += 12
        if period == "凌晨" and hour == 12:
            hour = 0
        if hour == 24:
            hour = 0
        return hour, minute
    return None


_RESCHEDULE_HINT_RE = re.compile(
    r"(?:改到|改在|改期到|改期为|调整到|调整为|换成|换到|推迟到|延到|改一下)\s*([^。；;！!？?\n]{0,24})"
)


def parse_reschedule_time(
    text: str | None,
    *,
    now: datetime | None = None,
    tz: str = DEFAULT_TZ,
) -> datetime | None:
    """从“改期”语境里取新时间。

    候选人常见句式会同时出现原时间和目标时间：
    「下周二我临时有会，能否改到周四下午 3 点？」
    直接整体解析会先命中“下周二”，因此优先解析「改到/改在/调整到…」后面的片段。
    """
    if not text:
        return None
    match = _RESCHEDULE_HINT_RE.search(text)
    if match:
        parsed = parse_chinese_datetime(match.group(1), now=now, tz=tz, default_hour=None)
        if parsed is not None:
            return parsed
    return parse_chinese_datetime(text, now=now, tz=tz, default_hour=None)


def parse_chinese_datetime(
    text: str | None,
    *,
    now: datetime | None = None,
    tz: str = DEFAULT_TZ,
    default_hour: int | None = 10,
) -> datetime | None:
    """解析中文时间表达，返回 **带时区的本地时间**；无法识别返回 None。

    ``default_hour``：只说了日期没说时间时的兜底小时（None 表示视为未给出时间）。
    """
    if not text:
        return None

    tzinfo = ZoneInfo(tz)
    current = now or datetime.now(tzinfo)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tzinfo)
    else:
        current = current.astimezone(tzinfo)

    parsed_date = _parse_date(text, current)
    parsed_time = _parse_time(text)

    if parsed_date is None and parsed_time is None:
        return None
    if parsed_time is None and default_hour is None:
        return None

    if parsed_date is None:
        # 只给了时间（如“改到下午 3 点”）：当天已过则顺延到次日
        assert parsed_time is not None
        candidate = datetime.combine(current.date(), datetime.min.time()).replace(
            hour=parsed_time[0], minute=parsed_time[1], tzinfo=tzinfo
        )
        if candidate <= current:
            candidate += timedelta(days=1)
        return candidate

    hour, minute = parsed_time if parsed_time else (default_hour or 0, 0)
    return datetime(
        parsed_date.year,
        parsed_date.month,
        parsed_date.day,
        hour,
        minute,
        tzinfo=tzinfo,
    )
