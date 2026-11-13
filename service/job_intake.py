"""招聘需求接入：主管发一段 JD（或一张 JD 截图），机器人自己生成岗位配置。

为什么不让 HR 改 YAML：
``config/positions.yaml`` 是给开发看的，缩进错一格整轮任务就起不来。
这个服务把「人话 → 结构化配置」这段接管掉，写盘落在 ``config/positions.d/``，
一个岗位一个文件：加岗位不用碰主配置，删岗位就是删文件。

写盘后由 ``Settings.load_company_config()`` 合并读取，下一轮任务即生效。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from client.llm import LLMClient
from config.settings import BASE_DIR, Settings
from core.logging import get_logger, log_extra
from prompt.job_intake import ALLOWED_FIELDS, ALLOWED_OPERATORS, JOBS_INTAKE_SYSTEM, build_intake_prompt
from schema.config import PositionConfig

logger = get_logger(__name__)

DRAFT_DIR = BASE_DIR / "config" / "positions.d"


class JobIntakeError(ValueError):
    """JD 解析失败或字段非法：消息直接回给主管，让他补一句。"""


class JobIntakeService:
    def __init__(self, *, settings: Settings, llm: LLMClient | None) -> None:
        self.settings = settings
        self.llm = llm

    async def parse(self, text: str) -> PositionConfig:
        """JD 文本 → 已校验的岗位配置。"""
        text = (text or "").strip()
        if len(text) < 8:
            raise JobIntakeError("这段太短了，我没看出岗位要求。把 JD 原文发我一遍？")

        config = self.settings.load_company_config()
        departments = [d.name for d in config.departments]
        default_department = departments[0] if departments else "技术部"

        data: dict[str, Any] | None = None
        if self.llm is not None:
            try:
                data = await self.llm.complete_json(
                    system=JOBS_INTAKE_SYSTEM,
                    user=build_intake_prompt(
                        text, departments=departments, default_department=default_department
                    ),
                    temperature=0.1,
                    max_tokens=1200,
                )
            except Exception as exc:  # noqa: BLE001 - 模型失败退回规则解析
                logger.warning("job_intake.llm_failed", extra=log_extra(error=f"{type(exc).__name__}: {exc}"))
        # 模型偶尔只吐一条规则或包了一层 {"position": {...}}，这里都拆平；
        # 实在没有岗位名就退回规则解析，保证「一定能落一个岗位」，而不是写个空壳。
        data = _unwrap(data or {})
        if not str(data.get("title") or "").strip():
            fallback = _rule_parse(text, default_department=default_department)
            fallback.update({k: v for k, v in data.items() if v})
            data = fallback
        return _to_position(data, default_department=default_department)

    async def parse_image(self, image_bytes: bytes, *, mime: str = "image/png") -> PositionConfig:
        """JD 截图 → 岗位配置：先让多模态模型抄出文字，再走同一套文本解析。"""
        if self.llm is None:
            raise JobIntakeError("截图收到，但我这边没配模型，读不了图。把岗位要求用文字发我一遍？")
        try:
            text = await self.llm.complete_vision(
                image_bytes=image_bytes,
                mime=mime,
                system="你只做一件事：把招聘截图里的岗位要求逐字转录成中文文字，不要总结、不要补充。",
                user="请完整抄写这张图里的招聘信息（岗位名、职责、任职要求）。",
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("job_intake.vision_unavailable", extra=log_extra(error=str(exc)))
            raise JobIntakeError(
                "截图收到了，但当前没配多模态模型（LLM_VISION_MODEL），读不了图。"
                "把岗位要求用文字发我一遍，我马上配好。"
            ) from exc
        if len(text) < 8:
            raise JobIntakeError("图上没读到有效文字，可能是空白图或字体太小。换张清楚点的，或直接发文字。")
        return await self.parse(text)

    def apply(self, position: PositionConfig) -> Path:
        """把岗位配置写成 ``config/positions.d/<code>.yaml``（同 code 覆盖）。"""
        DRAFT_DIR.mkdir(parents=True, exist_ok=True)
        path = DRAFT_DIR / f"{position.code}.yaml"
        payload = position.model_dump(mode="json", exclude_none=True)
        path.write_text(
            "# 由机器人生成（主管在飞书/邮件里发的招聘需求）。手改也可以，格式同 positions.yaml。\n"
            + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        logger.info("job_intake.applied", extra=log_extra(code=position.code, path=str(path)))
        return path


def describe(position: PositionConfig) -> str:
    """回给主管的确认文案：短，但要能让他核对关键规则。"""
    hard = "、".join(c.label or f"{c.field}" for c in position.screening.hard_conditions) or "无"
    return (
        f"已录入「{position.title}」（{position.department}，招 {position.headcount} 人）。\n"
        f"硬性条件：{hard}\n"
        f"及格线 {position.screening.threshold:.0f} 分。下一轮收简历就按这个筛。\n"
        "要改直接再发我一遍新的要求。"
    )


# ---------------------------------------------------------------- 内部：校验与兜底
def _unwrap(data: dict[str, Any]) -> dict[str, Any]:
    """兼容 {"position": {...}} / {"job": {...}} 这类多包一层，或只返回了单条规则的情况。"""
    for key in ("position", "job", "data", "result"):
        inner = data.get(key)
        if isinstance(inner, dict):
            return inner
    # 只返回了一条规则（有 field + operator，没有 title）
    if "field" in data and "title" not in data:
        return {"weighted": [data]} if "weight" in data else {"hard_conditions": [data]}
    return data

def _to_position(data: dict[str, Any], *, default_department: str) -> PositionConfig:
    """模型输出 → PositionConfig，顺手把非法字段剔掉。"""
    screening = data.get("screening") or {}
    hard = data.get("hard_conditions") or screening.get("hard_conditions") or []
    weighted = data.get("weighted") or screening.get("weighted") or []
    payload = {
        "code": _slug(data.get("code") or data.get("title") or "new_position"),
        "title": str(data.get("title") or "").strip() or "未命名岗位",
        "department": str(data.get("department") or "").strip() or default_department,
        "headcount": _as_int(data.get("headcount"), 1),
        "description": str(data.get("description") or "").strip(),
        "keywords": [str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()][:10],
        "screening": {
            "threshold": min(max(_as_int(data.get("threshold"), 55), 0), 100),
            "rule_version": "v1-ai",
            "hard_conditions": [c for c in (_clean_condition(x) for x in hard) if c],
            "weighted": [c for c in (_clean_weight(x) for x in weighted) if c],
        },
    }
    try:
        return PositionConfig.model_validate(payload)
    except Exception as exc:  # noqa: BLE001
        raise JobIntakeError(f"这条需求我没能转成可执行的规则（{exc}）。换个说法再发我一次？") from exc


def _clean_condition(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    field = str(raw.get("field") or "").strip()
    operator = str(raw.get("operator") or "gte").strip()
    # 「专业 in [计算机, 软件]」这种写法会把「计算机科学与技术」判成不符合，
    # 包含类字段统一改成 contains_any，与 positions.yaml 的既有写法一致。
    if field in {"major", "school", "skills", "project_highlights", "summary"} and operator in {"in", "eq"}:
        operator = "contains_any"
    if field not in ALLOWED_FIELDS or operator not in ALLOWED_OPERATORS:
        return None
    value = raw.get("value")
    if value in (None, "", []):
        return None
    return {"field": field, "operator": operator, "value": value, "label": str(raw.get("label") or "")}


def _clean_weight(raw: Any) -> dict[str, Any] | None:
    cleaned = _clean_condition(raw)
    if cleaned is None:
        return None
    weight = _as_int(raw.get("weight"), 0)
    if weight <= 0:
        return None
    cleaned["weight"] = weight
    cleaned.setdefault("operator", "contains_any")
    return cleaned


def _slug(text: str) -> str:
    ascii_part = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")
    return ascii_part or f"position_{abs(hash(str(text))) % 10000}"


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def _rule_parse(text: str, *, default_department: str) -> dict[str, Any]:
    """模型不可用时的兜底：只抓最明显的四件事，保证功能不是「没网就死」。"""
    years = re.search(r"(\d+)\s*年(以上|及以上)?", text)
    education = next((x for x in ("博士", "硕士", "本科", "大专") if x in text), "")
    hard: list[dict[str, Any]] = []
    if education:
        hard.append({"field": "education_level", "operator": "gte", "value": education, "label": f"学历{education}及以上"})
    if years:
        hard.append(
            {
                "field": "years_experience",
                "operator": "gte",
                "value": int(years.group(1)),
                "label": f"{years.group(1)} 年以上经验",
            }
        )
    words = [w for w in re.split(r"[，。；、\s/|]+", text) if 2 <= len(w) <= 12][:8]
    return {
        "code": _slug(text[:20]),
        "title": (text.strip().splitlines() or ["未命名岗位"])[0][:30],
        "department": default_department,
        "headcount": 1,
        "description": text[:80],
        "keywords": words,
        "threshold": 55,
        "hard_conditions": hard,
        "weighted": [{"field": "skills", "operator": "contains_any", "weight": 100, "value": words[:6], "label": "关键词匹配"}]
        if words
        else [],
    }


__all__ = ["DRAFT_DIR", "JobIntakeError", "JobIntakeService", "describe"]
