"""简历文本提取与清洗（ETL 的第一层）。

借鉴 ``Unstructured-IO/unstructured`` 的分层思想：**先把杂乱格式统一成干净文本，
再交给下游做结构化抽取**。因此这里只输出纯文本 + 少量确定性预填字段，
不做任何筛选或业务判断（那些在 ``service/`` 层）。

运行环境没有 ``unstructured`` 时自动降级到 pypdf / python-docx，行为对上层一致。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from config.settings import Settings
from core.exceptions import ValidationFailed
from core.logging import get_logger, log_extra
from schema.extraction import ExtractedCandidate

logger = get_logger(__name__)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_NAME_RE = re.compile(r"姓\s*名[:：]\s*([\u4e00-\u9fa5]{2,4})")
_SCHOOL_RE = re.compile(r"([\u4e00-\u9fa5]{2,12}(?:大学|学院|学校))")
_MAJOR_RE = re.compile(r"(?:专\s*业|所学专业)[:：]\s*([\u4e00-\u9fa5A-Za-z（）()\- ]{2,20})")
_MAJOR_SUFFIX_RE = re.compile(r"([\u4e00-\u9fa5]{2,10}专业)")
_YEARS_RE = re.compile(r"(?:工作年限|工作经验|从业经验)[:：]?\s*([0-9]{1,2})\s*年")
_YEARS_ALT_RE = re.compile(r"([0-9]{1,2})\s*年(?:以上)?(?:的)?(?:工作|开发|运营|产品|算法)?经验")

_EDUCATION_KEYWORDS: list[tuple[str, str]] = [
    ("博士", "博士"),
    ("硕士", "硕士"),
    ("研究生", "硕士"),
    ("MBA", "硕士"),
    ("本科", "本科"),
    ("学士", "本科"),
    ("大专", "大专"),
    ("专科", "大专"),
    ("高中", "高中"),
    ("中专", "高中"),
]

#: 用于确定性预填的技能词典（LLM 可用时会补齐词典外的技能）
SKILL_DICTIONARY: tuple[str, ...] = (
    "Python", "Java", "Go", "C++", "C#", "JavaScript", "TypeScript", "PHP", "Rust",
    "FastAPI", "Django", "Flask", "Spring", "SpringBoot", "Node.js", "Vue", "React",
    "MySQL", "PostgreSQL", "Redis", "MongoDB", "Elasticsearch", "Kafka", "RabbitMQ",
    "Docker", "Kubernetes", "Linux", "Git", "Nginx", "微服务", "分布式", "高并发",
    "PyTorch", "TensorFlow", "深度学习", "机器学习", "自然语言处理", "NLP", "CV",
    "大模型", "LLM", "Transformer", "推荐系统", "数据挖掘", "Spark", "Hadoop",
    "需求分析", "竞品分析", "PRD", "数据分析", "用户研究", "Axure", "SQL", "版本规划",
    "内容运营", "活动策划", "社群", "投放", "SEO", "SEM", "短视频", "新媒体", "增长",
)

_HIGHLIGHT_KEYWORDS: tuple[str, ...] = (
    "负责", "主导", "搭建", "优化", "落地", "重构", "设计", "上线", "提升", "实现",
)


@dataclass
class ParsedResume:
    filename: str
    path: str
    text: str
    engine: str
    page_count: int = 0
    warnings: list[str] = field(default_factory=list)


class ResumeParser:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------ 入口
    async def save_and_parse(self, data: bytes, filename: str) -> ParsedResume:
        """保存到 ``var/resumes`` 并解析（下载/上传的字节统一走这里）。"""
        if not data:
            raise ValidationFailed("简历内容为空")
        safe_name = _safe_filename(filename or "resume.bin")
        self.settings.resumes_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        path = self.settings.resumes_dir / f"{stamp}-{uuid.uuid4().hex[:6]}-{safe_name}"
        path.write_bytes(data)
        parsed = await self.parse_path(path)
        # 展示与打包用原始文件名，落盘路径才带时间戳前缀（避免重名）
        parsed.filename = safe_name
        return parsed

    async def parse_path(self, path: str | Path) -> ParsedResume:
        path = Path(path)
        if not path.exists():
            raise ValidationFailed(f"简历文件不存在: {path}")
        text, engine, page_count, warnings = await asyncio.to_thread(self._extract_text, path)
        if not text.strip():
            warnings.append("未能提取到文本（可能是扫描件/图片型 PDF）")
        logger.info(
            "resume.parsed",
            extra=log_extra(file=path.name, engine=engine, chars=len(text), pages=page_count),
        )
        return ParsedResume(
            filename=path.name,
            path=str(path),
            text=text,
            engine=engine,
            page_count=page_count,
            warnings=warnings,
        )

    # -------------------------------------------------------------- 文本提取
    def _extract_text(self, path: Path) -> tuple[str, str, int, list[str]]:
        warnings: list[str] = []
        suffix = path.suffix.lower()

        if suffix in {".txt", ".md"}:
            return path.read_text(encoding="utf-8", errors="replace"), "text", 1, warnings

        # 首选 unstructured：它对版面、表格、分栏的处理更稳
        text = _try_unstructured(path, warnings)
        if text.strip():
            return text, "unstructured", 0, warnings

        if suffix == ".pdf":
            text, pages = _pdf_text(path, warnings)
            return text, "pypdf", pages, warnings
        if suffix in {".docx", ".doc"}:
            text = _docx_text(path, warnings)
            return text, "python-docx", 0, warnings

        # 未知扩展名：按文本尽力读取
        try:
            return path.read_text(encoding="utf-8", errors="replace"), "text-fallback", 1, warnings
        except OSError:
            return "", "unsupported", 0, warnings + [f"不支持的文件类型: {suffix}"]

    # -------------------------------------------------------------- 确定性预填
    def prefill(self, text: str, filename: str = "") -> ExtractedCandidate:
        """规则预填：LLM 不可用时它就是抽取结果，可用时作为兜底与交叉校验。"""
        normalized = text.replace("\r\n", "\n")
        email_match = _EMAIL_RE.search(normalized)
        phone_match = _PHONE_RE.search(normalized)

        education = ""
        for keyword, level in _EDUCATION_KEYWORDS:
            if keyword in normalized:
                education = level
                break

        school_match = _SCHOOL_RE.search(normalized)
        major = ""
        major_match = _MAJOR_RE.search(normalized) or _MAJOR_SUFFIX_RE.search(normalized)
        if major_match:
            major = major_match.group(1).strip()

        years = 0.0
        years_match = _YEARS_RE.search(normalized) or _YEARS_ALT_RE.search(normalized)
        if years_match:
            years = float(years_match.group(1))

        skills = [skill for skill in SKILL_DICTIONARY if skill.lower() in normalized.lower()]
        highlights = _extract_highlights(normalized)

        return ExtractedCandidate(
            name=self._guess_name(normalized, filename),
            email=email_match.group(0).lower() if email_match else "",
            phone=phone_match.group(1) if phone_match else "",
            education_level=education,
            school=school_match.group(1) if school_match else "",
            major=major,
            years_experience=years,
            skills=skills,
            project_highlights=highlights,
            summary=_first_paragraph(normalized),
            confidence=0.55 if (email_match or phone_match) else 0.3,
        )

    @staticmethod
    def _guess_name(text: str, filename: str) -> str:
        match = _NAME_RE.search(text)
        if match:
            return match.group(1)
        stem = Path(filename).stem if filename else ""
        candidate = re.split(r"[-_—\s（(]", stem)[0].strip()
        if 2 <= len(candidate) <= 4 and re.fullmatch(r"[\u4e00-\u9fa5]{2,4}", candidate):
            return candidate
        return ""


# --------------------------------------------------------------------- 工具


def _safe_filename(name: str) -> str:
    name = Path(name).name
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]", "_", name).strip()
    return name[:120] or "resume.bin"


def _try_unstructured(path: Path, warnings: list[str]) -> str:
    try:
        from unstructured.partition.auto import partition  # type: ignore
    except Exception:  # noqa: BLE001 - 未安装即降级
        return ""
    try:
        elements = partition(filename=str(path))
        return "\n".join(str(element) for element in elements if str(element).strip())
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"unstructured 解析失败，已降级: {exc}")
        return ""


def _pdf_text(path: Path, warnings: list[str]) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
    except ImportError:
        warnings.append("未安装 pypdf，无法解析 PDF")
        return "", 0
    try:
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(pages).strip(), len(reader.pages)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"PDF 解析失败: {exc}")
        return "", 0


def _docx_text(path: Path, warnings: list[str]) -> str:
    try:
        import docx  # type: ignore
    except ImportError:
        warnings.append("未安装 python-docx，无法解析 DOCX")
        return ""
    try:
        document = docx.Document(str(path))
        chunks = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
        for table in document.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    chunks.append(" | ".join(cells))
        return "\n".join(chunks)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"DOCX 解析失败: {exc}")
        return ""


def _extract_highlights(text: str) -> list[str]:
    highlights: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip(" ·•-—\t")
        if not (8 <= len(stripped) <= 150):
            continue
        if any(keyword in stripped for keyword in _HIGHLIGHT_KEYWORDS):
            if stripped not in highlights:
                highlights.append(stripped)
        if len(highlights) >= 6:
            break
    return highlights


def _first_paragraph(text: str) -> str:
    for line in text.split("\n"):
        stripped = line.strip()
        if len(stripped) >= 20:
            return stripped[:300]
    return text.strip()[:300]
