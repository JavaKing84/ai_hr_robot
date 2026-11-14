"""招聘需求接入：主管发一段 JD（或一张 JD 截图），机器人自己生成岗位配置。

为什么不让 HR 改 YAML：
``config/positions.yaml`` 是给开发看的，缩进错一格整轮任务就起不来。
这个服务把「人话 → 结构化配置」这段接管掉，写盘落在 ``config/positions.d/``，
一个岗位一个文件：加岗位不用碰主配置，删岗位就是删文件。

写盘后由 ``Settings.load_company_config()`` 合并读取，下一轮任务即生效。
"""

from __future__ import annotations

import hashlib
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

#: 待确认草稿的子目录名。放在 ``positions.d`` 下面有个好处：
#: ``Settings._merge_draft_positions`` 用的是 ``glob("*.yaml")``（不递归），
#: 这个子目录天然不会被当成正式岗位读进去。
#:
#: 这里刻意**不**把路径算成模块级常量 ``PENDING_DIR = DRAFT_DIR / "_pending"``：
#: 测试会 monkeypatch ``DRAFT_DIR`` 指到临时目录，常量在 import 时就固化了，
#: 结果待确认文件会写进真实仓库。每次调用时再算才能跟着 monkeypatch 走。
PENDING_DIRNAME = "_pending"


def pending_dir() -> Path:
    """当前的待确认目录（跟随 ``DRAFT_DIR``，便于测试替换）。"""
    return DRAFT_DIR / PENDING_DIRNAME


class JobIntakeError(ValueError):
    """JD 解析失败或字段非法：消息直接回给主管，让他补一句。"""


class JobIntakeService:
    def __init__(self, *, settings: Settings, llm: LLMClient | None) -> None:
        self.settings = settings
        self.llm = llm
        #: 上一次解析是模型抽的还是规则兜底的（``llm`` / ``rule``）。
        #: 模型偶发超时会让结果退回 ``_rule_parse``，那份结果的标题常常是整句话、
        #: 部门是默认值，看起来却「像模像样」。这个标记让确认文案能如实说明，
        #: 否则 HR 会以为自己核对的是模型抽取的结果。
        self.last_source = "rule"

    async def parse(self, text: str) -> PositionConfig:
        """JD 文本 → 已校验的岗位配置。"""
        text = (text or "").strip()
        if len(text) < 8:
            raise JobIntakeError("这段太短了，我没看出岗位要求。把 JD 原文发我一遍？")

        config = self.settings.load_company_config()
        departments = [d.name for d in config.departments]
        default_department = departments[0] if departments else "技术部"

        data: dict[str, Any] | None = None
        self.last_source = "rule"
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
                self.last_source = "llm"
            except Exception as exc:  # noqa: BLE001 - 模型失败退回规则解析
                logger.warning("job_intake.llm_failed", extra=log_extra(error=f"{type(exc).__name__}: {exc}"))
        # 模型偶尔只吐一条规则或包了一层 {"position": {...}}，这里都拆平；
        # 实在没有岗位名就退回规则解析，保证「一定能落一个岗位」，而不是写个空壳。
        data = _unwrap(data or {})
        if not str(data.get("title") or "").strip():
            fallback = _rule_parse(text, default_department=default_department)
            fallback.update({k: v for k, v in data.items() if v})
            data = fallback
            # 连标题都没抽出来，说明模型这次没帮上忙，产物其实是规则拼的
            self.last_source = "rule"
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
        """把岗位配置写成 ``config/positions.d/<code>.yaml``（同 code 覆盖）。

        这是「直接生效」的老入口，脚本与测试还在用。走飞书对话时要先
        :meth:`stage` 再 :meth:`commit`——中间留给主管确认。
        """
        DRAFT_DIR.mkdir(parents=True, exist_ok=True)
        path = DRAFT_DIR / f"{position.code}.yaml"
        _dump(
            path,
            position,
            header="# 由机器人生成（主管在飞书/邮件里发的招聘需求）。手改也可以，格式同 positions.yaml。\n",
        )
        logger.info("job_intake.applied", extra=log_extra(code=position.code, path=str(path)))
        return path

    # ------------------------------------------------------ 二次确认：暂存 / 生效 / 作废
    def stage(self, position: PositionConfig) -> Path:
        """写进「待确认」目录，**不**影响任何在招岗位。

        确认前不碰 ``positions.d``：模型偶尔会把「五年经验」抽成一条奇怪的硬条件，
        直接生效的话，下一轮收简历就按错的规则筛，而 HR 以为自己只是「说了一句」。
        """
        directory = pending_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{position.code}.yaml"
        _dump(
            path,
            position,
            header="# 待主管确认的岗位草稿（尚未生效）。主管回复「确认」后由 commit() 移入 positions.d/。\n",
        )
        logger.info("job_intake.staged", extra=log_extra(code=position.code, path=str(path)))
        return path

    def commit(self, position: PositionConfig) -> Path:
        """主管确认后生效：写正式草稿，并删掉暂存文件。"""
        path = self.apply(position)
        self.discard(position.code)
        logger.info("job_intake.committed", extra=log_extra(code=position.code, path=str(path)))
        return path

    def discard(self, code: str) -> bool:
        """作废一份暂存草稿；返回是否真的删掉了文件。"""
        path = pending_dir() / f"{code}.yaml"
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        logger.info("job_intake.discarded", extra=log_extra(code=code))
        return True

    def load_staged(self, code: str) -> PositionConfig | None:
        """把暂存的配置读回来（确认时用**主管当时看到的那份**，不重新调模型）。"""
        path = pending_dir() / f"{code}.yaml"
        if not path.exists():
            return None
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return PositionConfig.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - 文件被手改坏了，当作没有暂存
            logger.warning(
                "job_intake.staged_invalid",
                extra=log_extra(code=code, error=f"{type(exc).__name__}: {exc}"),
            )
            return None

    def staged_codes(self) -> list[str]:
        """当前待确认的岗位 code 列表（按文件名排序，结果稳定）。"""
        directory = pending_dir()
        if not directory.is_dir():
            return []
        return sorted({p.stem for p in directory.glob("*.yaml")})

    def latest_staged_code(self) -> str:
        """最近一次录入的暂存 code；没有则返回空串。

        主管只说「确认」时，指的是刚刚发给他的那份。多份待确认并存
        （连发两份 JD 没回确认）就按修改时间取最新，避免让他记住 code。
        """
        directory = pending_dir()
        if not directory.is_dir():
            return ""
        files = [p for p in directory.glob("*.yaml") if p.is_file()]
        if not files:
            return ""
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files[0].stem


def _dump(path: Path, position: PositionConfig, *, header: str) -> None:
    payload = position.model_dump(mode="json", exclude_none=True)
    path.write_text(
        header + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )



def describe(position: PositionConfig) -> str:
    """回给主管的确认文案：短，但要能让他核对关键规则。"""
    hard = "、".join(c.label or f"{c.field}" for c in position.screening.hard_conditions) or "无"
    return (
        f"已录入「{position.title}」（{position.department}，招 {position.headcount} 人）。\n"
        f"硬性条件：{hard}\n"
        f"及格线 {position.screening.threshold:.0f} 分。下一轮收简历就按这个筛。\n"
        "要改直接再发我一遍新的要求。"
    )


def describe_pending(position: PositionConfig, *, exists: bool = False, source: str = "llm") -> str:
    """二次确认文案：把模型抽出来的东西摊开给主管核对，再问「确认」。

    ``exists=True`` 表示这是覆盖一个已有 code 的岗位（改要求），措辞要区分，
    否则主管会以为是新增，改完发现旧岗位不见了。

    ``source="rule"`` 表示模型这次没帮上忙、内容是简单规则拼的：必须写在脸上。
    那种结果的岗位名常常是「整句话」，部门是默认值——看起来却很像真的。
    """
    action = "修改岗位" if exists else "新增岗位"
    hard = "、".join(c.label or str(c.field) for c in position.screening.hard_conditions) or "无"
    keywords = "、".join(position.keywords[:8])
    if not keywords:
        keywords = "、".join(
            "、".join(str(v) for v in c.value) if isinstance(c.value, list) else str(c.value)
            for c in position.screening.weighted
        ) or "无"
    warn = (
        "⚠️ 模型这次没响应，下面是我用简单规则抓的，字段大概率不准。"
        "建议先回「取消」，再把岗位名称和要求分两行发我一遍。\n\n"
        if source != "llm"
        else ""
    )
    return (
        f"{warn}确认{action}？回复「确认」生效，回复「取消」作废。\n\n"
        f"岗位：{position.title}（{position.department}，招 {position.headcount} 人）\n"
        f"硬性条件：{hard}\n"
        f"加权关键词：{keywords}\n"
        f"及格线 {position.screening.threshold:.0f} 分"
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
    # 只认「至少两个连续字母」才算可读 slug。
    # 反例：JD 前 20 字里只有「3 年以上经验」的那个 3 时，ascii_part 会是 "3"，
    # 于是「3 年经验的产品经理」和「3 年经验的后端」都得到 code="3"——
    # 后录的那份把先录的覆盖掉，HR 却看到「已录入」。数字当不了岗位标识。
    if ascii_part and re.search(r"[a-z]{2,}", ascii_part):
        return ascii_part[:40].strip("_")
    # 中文标题没有 ascii 可用，用内容哈希兜底。
    # 不能用内置 hash()：它受 PYTHONHASHSEED 影响，同一段 JD 换个进程就算出另一个值，
    # 于是「同 code 覆盖」失效——主管把同一份 JD 发两次，会留下两个岗位文件，
    # 下一轮任务里就成了两个重复岗位。md5 与进程无关，稳定。
    digest = hashlib.md5(str(text).encode("utf-8")).hexdigest()[:8]
    return f"position_{digest}"


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
        # 用**整段** JD 算 code，不用前 20 字：两份要求不同但开头一样的 JD
        # （「3 年以上经验的产品经理」/「3 年以上经验的后端」）否则会撞成同一个岗位。
        "code": _slug(text),
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


__all__ = [
    "DRAFT_DIR",
    "PENDING_DIRNAME",
    "JobIntakeError",
    "JobIntakeService",
    "describe",
    "describe_pending",
    "pending_dir",
]
