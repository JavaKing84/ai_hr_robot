"""简历文本提取与清洗（ETL 的第一层）。

借鉴 ``Unstructured-IO/unstructured`` 的分层思想：**先把杂乱格式统一成干净文本，
再交给下游做结构化抽取**。因此这里只输出纯文本 + 少量确定性预填字段，
不做任何筛选或业务判断（那些在 ``service/`` 层）。

2026-11-12 重写「确定性预填」部分。旧版直接在全文用宽松正则抓片段，导致
「提升的专业精神」→ 专业=「学习意愿和持续提升的专业」、「连续四年大学」→ 院校。
新版按四层做：

1. **规范化**：去私用区图标字符、全角空格；PDF「每行一个字」时合并且按字段标签补换行。
2. **分节**：先按「教育背景 / 工作经历 / 项目经历 / 技能」切块，字段只在对应块内抽。
3. **锚点**：专业必须来自「专业：」整行、「院校 + 专业（学历）」同行、或紧跟学历词的
   「XX专业」三种结构之一；院校必须在教育块内且过伪值黑名单。
4. **校验**：专业过 ``config/majors.txt`` 名录或学科词根，不合格一律置空——
   **宁可留空，也不填错值去触发硬条件淘汰**。

运行环境没有 ``unstructured`` 时自动降级到 pypdf / python-docx，行为对上层一致。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

from config.settings import Settings
from core.exceptions import ValidationFailed
from core.logging import get_logger, log_extra
from schema.extraction import ExtractedCandidate, SourceQuote

logger = get_logger(__name__)

# --------------------------------------------------------------------- 基础正则

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")

#: 姓名：只认「姓名：」标签（首屏孤立行兜底在 ``_name_from_standalone``）
_NAME_LABEL_RE = re.compile(r"姓\s*名\s*[:：]\s*([\u4e00-\u9fa5]{2,4}|[A-Za-z][A-Za-z .]{2,30})")
#: 兼容旧引用名
_NAME_RE = _NAME_LABEL_RE

#: 院校：排除数字/中文数字开头（挡「连续四年大学」这类拼出来的伪值）
_SCHOOL_RE = re.compile(
    r"(?<![\d一二三四五六七八九十])([\u4e00-\u9fa5]{2,10}(?:大学|学院|学校))"
)
_SCHOOL_LABEL_RE = re.compile(r"(?:毕业院校|毕业学校|院\s*校|学\s*校)\s*[:：]\s*([^\n|｜]{2,30})")

#: 教育行：院校 + 专业（学历）同行，BOSS/智联最常见的版式
_EDU_LINE_RE = re.compile(
    r"(?P<school>[\u4e00-\u9fa5]{2,12}(?:大学|学院|学校))"
    r"[ \t|｜·•\-—/]+"
    r"(?P<major>[\u4e00-\u9fa5A-Za-z]{2,20}?)"
    r"(?:[（(](?P<degree>本科|硕士|博士|专科|学士|研究生)[）)])?"
    r"(?=[ \t|｜\n]|$)"
)

#: 专业：整行标签式，必须 ^...$（旧版在句子中间乱抓的根源）
_MAJOR_LABEL_RE = re.compile(
    r"(?m)^[ \t]*(?:所学)?专\s*业[ \t]*[:：][ \t]*([^\n|｜]{2,20})[ \t]*$"
)
#: 专业：后缀式，但必须紧跟学历词（前瞻不满足就不认）
_MAJOR_SUFFIX_RE = re.compile(
    r"([\u4e00-\u9fa5]{2,12}专业)(?=[ \t]*(?:本科|硕士|学士|博士|研究生|学历|学位|\n|$))"
)

_EDU_LEVEL_LABEL_RE = re.compile(
    r"(?:学\s*历|学\s*位|最高学历)\s*[:：]?\s*(博士|硕士|研究生|本科|学士|大专|专科|高中|中专)"
)
_YEARS_LABELED_RE = re.compile(
    r"(?:工作年限|工作经验|从业年限|从业经验|工作年份)\s*[:：]?\s*(\d{1,2}(?:\.\d)?)\s*年"
)
_YEARS_ALT_RE = re.compile(r"(\d{1,2})\s*年(?:以上)?(?:的)?(?:工作|开发|运营|产品|算法|测试|设计)经验")

#: 起止时间：2021.07-至今 / 2019.09 - 2023.06 / 2020/03~2022/08
_DATE_RANGE_RE = re.compile(
    r"(?P<y1>(?:19|20)\d{2})[.\-/年]?\s*(?P<m1>\d{1,2})?\s*[-–—~至到]\s*"
    r"(?:(?P<y2>(?:19|20)\d{2})[.\-/年]?\s*(?P<m2>\d{1,2})?|至今|今|现在|目前)"
)

#: 求职意向：归位的最强信号（BOSS 简历 19 份里 15 份有这一行）
_POSITION_HINT_RE = re.compile(
    r"(?:求职意向|应聘岗位|期望职位|应聘职位|目标岗位|意向岗位|求职岗位)\s*[:：]?\s*([^\n|｜]{2,30})"
)

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

# --------------------------------------------------------------------- 分节词表

_SECTION_HEADS: tuple[tuple[str, str], ...] = (
    ("edu", r"教育(?:背景|经历|履历)|学习经历|学历信息"),
    ("work", r"工作(?:经历|经验)|职业经历|实习经历|实践经历"),
    ("project", r"项目(?:经历|经验|作品)|主要项目"),
    ("skills", r"专业技能|技能清单|技能特长|技术栈|个人技能"),
    ("intent", r"求职意向|应聘岗位|期望职位|应聘职位|目标岗位"),
    ("self", r"自我评价|个人优势|自我介绍|个人简介"),
    ("award", r"荣誉|奖项|证书|资格证书"),
)

#: 专业校验：出现这些词说明抓到的是句子片段/证书名，不是专业
_MAJOR_BLOCK_WORDS = (
    "资格", "证书", "考试", "认证", "培训", "课程", "精神", "能力", "知识", "素养",
    "技能", "方向", "团队", "技术与软件", "专业精神",
)

#: 学科词根：名录未收录但明显是专业的，用词根兜底
_MAJOR_ROOTS: tuple[str, ...] = (
    "计算机", "软件", "电子", "通信", "自动化", "人工智能", "信息", "网络", "数据",
    "数学", "统计", "物理", "化学", "材料", "机械", "生物", "经济", "管理", "金融",
    "会计", "法学", "医学", "新闻", "传播", "教育", "心理", "设计", "电气", "土木",
    "建筑", "交通", "能源", "环境", "食品", "农学", "药学", "护理", "英语", "日语",
    "汉语言", "历史", "哲学", "社会", "政治", "市场营销", "人力资源", "物流",
    "电子商务", "国际贸易", "财政", "税收", "审计", "保险", "投资", "资产评估",
    "工业工程", "测绘", "遥感", "船舶", "航空", "航天", "车辆", "动力", "安全工程",
    "消防", "公安", "侦查", "知识产权", "广告", "动画", "美术", "音乐",
)

#: 院校伪值黑名单
_NOT_SCHOOL_WORDS = (
    "四年", "期间", "毕业", "所大学", "的大学", "重点大学", "本科大学", "专科大学",
    "大学期间", "在校", "就读", "自考", "成人", "全国", "全省", "全市", "大学生",
    "中国大学", "外国大学",
)

#: 姓名伪值：这些词即使 2~4 汉字也不是人名
_NOT_NAME_WORDS = (
    "简历", "应聘", "求职", "工程师", "开发", "招聘", "通知", "岗位", "职位",
    "先生", "女士", "实习", "测试", "北京", "上海", "深圳", "广州", "杭州",
    "个人", "信息", "教育", "背景", "工作", "经历", "项目", "技能", "评价",
    "优势", "电话", "邮箱", "年龄", "性别", "学历", "专业", "院校", "公司",
    "一年以内", "应届", "在校", "统招", "本科", "硕士", "大专",
)

#: 私用区图标字符（BOSS 的 ``\ue619`` 之类）
_PRIVATE_USE_RE = re.compile(r"[\ue000-\uf8ff]")


@dataclass
class ParsedResume:
    filename: str
    path: str
    text: str
    engine: str
    page_count: int = 0
    warnings: list[str] = field(default_factory=list)


# ===================================================================== 规范化


def normalize_text(text: str) -> str:
    """把 PDF / DOCX 提取出来的脏文本整理成「一行一个语义块」的干净文本。

    处理三类实测问题：
    - BOSS 的私用区图标（``\\ue619``）夹在字段标签里，导致正则断开；
    - 全角空格 ``\\u3000`` 让「专　业：」匹配不上；
    - 部分 PDF 每个字符单独一行（``个\\n人\\n信\\n息``），技能词被拆碎。
    """
    text = _PRIVATE_USE_RE.sub(" ", text or "")
    text = text.replace("\u3000", " ").replace("\xa0", " ").replace("\r\n", "\n")
    text = re.sub(r"[ \t]{2,}", " ", text)
    if _looks_char_split(text):
        text = _join_char_split(text)
    return text


def _looks_char_split(text: str) -> bool:
    """单字符行占比过半 → 判定为「每字符一行」的 PDF。"""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 40:
        return False
    single = sum(1 for line in lines if len(line.strip()) == 1)
    return single / len(lines) > 0.55


def _join_char_split(text: str) -> str:
    """合并逐字符行，并在字段标签前补回换行，保住行级正则的锚定能力。"""
    joined = re.sub(r"\n(?=\S)", "", text)
    labels = (
        r"姓\s*名|电\s*话|手\s*机|邮\s*箱|学\s*历|专\s*业|毕业院校|教育背景|教育经历|"
        r"工作经历|项目经历|实习经历|专业技能|技能清单|自我评价|个人优势|求职意向|"
        r"应聘岗位|期望职位|出生年月|住\s*址|工作年限|工作经验"
    )
    joined = re.sub(rf"(?=(?:{labels})[ \t]*[:：])", "\n", joined)
    return joined


# ===================================================================== 分节


def _section_of(line: str) -> str:
    """判断某一行是不是「分节标题」。带冒号的行是有值的内容行，不算标题。"""
    if not line or len(line) > 26 or "：" in line or ":" in line:
        return ""
    for key, pattern in _SECTION_HEADS:
        if re.search(pattern, line):
            return key
    return ""


def split_sections(text: str) -> dict[str, str]:
    """按标题把简历切成 ``edu / work / project / skills / intent / self`` 等块。"""
    buckets: dict[str, list[str]] = {}
    current = "head"
    for line in text.splitlines():
        key = _section_of(line.strip())
        if key:
            current = key
            buckets.setdefault(current, [])
            continue
        buckets.setdefault(current, []).append(line)
    return {key: "\n".join(lines) for key, lines in buckets.items()}


# ===================================================================== 词典校验


@lru_cache(maxsize=1)
def major_dictionary() -> frozenset[str]:
    """加载 ``config/majors.txt``（读不到就返回空集，只靠词根兜底）。"""
    path = Path(__file__).resolve().parents[1] / "config" / "majors.txt"
    try:
        names = {
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
    except OSError:
        names = set()
    return frozenset(names)


def validate_major(value: str) -> str:
    """专业合法性校验：不合格返回空串，绝不把句子片段当专业。"""
    v = (value or "").strip(" 　·•-—|")
    if not v or len(v) > 20:
        return ""
    v = re.sub(r"[（(][^）)]*[）)]", "", v).strip()
    if v.endswith("专业"):
        v = v[:-2].strip()
    if len(v) < 2:
        return ""
    if any(word in v for word in _MAJOR_BLOCK_WORDS):
        return ""
    if v in major_dictionary():
        return v
    if any(root in v for root in _MAJOR_ROOTS):
        return v
    return ""


def validate_school(value: str) -> str:
    """院校合法性校验：必须是「XX大学/学院/学校」，且不是句子拼出来的伪值。"""
    v = (value or "").strip(" 　·•-—|")
    if not v or len(v) > 16:
        return ""
    match = _SCHOOL_RE.search(v)
    if not match:
        return ""
    name = match.group(1)
    if any(word in name for word in _NOT_SCHOOL_WORDS):
        return ""
    return name


# ===================================================================== 字段抽取


def _label_education_level(text: str) -> str:
    match = _EDU_LEVEL_LABEL_RE.search(text)
    if match:
        raw = match.group(1)
        for keyword, level in _EDUCATION_KEYWORDS:
            if keyword in raw:
                return level
    return ""


def _education_from_keywords(text: str) -> str:
    for keyword, level in _EDUCATION_KEYWORDS:
        if keyword in text:
            return level
    return ""


def _extract_education(text: str, sections: dict[str, str]) -> tuple[str, str, str, list[SourceQuote]]:
    """返回 (学历, 院校, 专业, 出处)。三个字段互相校验，宁空勿错。"""
    quotes: list[SourceQuote] = []
    edu_text = sections.get("edu", "")

    level = _label_education_level(text)
    if not level and edu_text:
        level = _label_education_level(edu_text)
    if not level:
        level = _education_from_keywords(edu_text) or _education_from_keywords(text)

    # 院校：标签 > 教育块内的「院校+专业」行 > 教育块内首个院校 > 全文首个院校
    school = ""
    school_label = _SCHOOL_LABEL_RE.search(text)
    if school_label:
        school = validate_school(school_label.group(1))
        if school:
            quotes.append(SourceQuote(field="school", quote=school_label.group(0)[:80]))

    # 很多 BOSS 简历没有「教育背景」标题，只有一行「时间 院校 专业（学历）」
    edu_match = _EDU_LINE_RE.search(edu_text or text)
    if not school and edu_match:
        school = validate_school(edu_match.group("school"))
        if school:
            quotes.append(SourceQuote(field="school", quote=edu_match.group(0)[:80]))
    if not school and edu_text:
        for candidate in _SCHOOL_RE.finditer(edu_text):
            school = validate_school(candidate.group(1))
            if school:
                quotes.append(SourceQuote(field="school", quote=candidate.group(0)[:80]))
                break
    if not school:
        for candidate in _SCHOOL_RE.finditer(text):
            school = validate_school(candidate.group(1))
            if school:
                quotes.append(SourceQuote(field="school", quote=candidate.group(0)[:80]))
                break

    if not level and edu_match and edu_match.group("degree"):
        raw = edu_match.group("degree")
        level = next((lv for kw, lv in _EDUCATION_KEYWORDS if kw in raw), "")

    # 专业：整行标签 > 教育行结构 > 后缀式（必须跟学历词）
    major = ""
    major_label = _MAJOR_LABEL_RE.search(text)
    if major_label:
        major = validate_major(major_label.group(1))
        if major:
            quotes.append(SourceQuote(field="major", quote=major_label.group(0).strip()[:80]))

    if not major and edu_match:
        major = validate_major(edu_match.group("major"))
        if major:
            quotes.append(SourceQuote(field="major", quote=edu_match.group(0)[:80]))

    if not major:
        scope = edu_text or text
        for candidate in _MAJOR_SUFFIX_RE.finditer(scope):
            major = validate_major(candidate.group(1))
            if major:
                quotes.append(SourceQuote(field="major", quote=candidate.group(0)[:80]))
                break

    return level, school, major, quotes


def _months_from_ranges(text: str) -> float:
    """从工作经历的时间区间算年限（比「15 年经验」这种自述更客观）。"""
    spans: list[tuple[int, int]] = []
    today = date.today()
    for match in _DATE_RANGE_RE.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        line = text[line_start : line_end if line_end != -1 else len(text)]
        # 教育经历的时间段不算工作年限
        if re.search(r"大学|学院|学校|本科|硕士|博士|专科|在校|就读", line):
            continue
        try:
            y1 = int(match.group("y1"))
            m1 = int(match.group("m1") or 1)
            if match.group("y2"):
                y2 = int(match.group("y2"))
                m2 = int(match.group("m2") or 12)
            else:
                y2, m2 = today.year, today.month
        except (TypeError, ValueError):
            continue
        start = y1 * 12 + (m1 - 1)
        end = y2 * 12 + (m2 - 1)
        if end < start or end - start > 480:
            continue
        spans.append((start, end))

    if not spans:
        return 0.0
    spans.sort()
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    months = sum(end - start for start, end in merged)
    return months / 12 if months > 0 else 0.0


def _years_from_filename(filename: str) -> float:
    """附件名兜底：``xxx_13年.pdf`` / ``xxx_一年以内.pdf``。"""
    name = filename or ""
    match = re.search(r"[_\-—](\d{1,2})\s*年", name)
    if match:
        return float(match.group(1))
    if "一年以内" in name or "应届" in name:
        return 0.5
    return 0.0


def _extract_years(text: str, sections: dict[str, str], filename: str) -> tuple[float, str]:
    work_text = sections.get("work") or text
    from_ranges = _months_from_ranges(work_text)

    label_years = 0.0
    match = _YEARS_LABELED_RE.search(text) or _YEARS_ALT_RE.search(text)
    if match:
        try:
            label_years = float(match.group(1))
        except ValueError:
            label_years = 0.0

    if from_ranges and label_years:
        # 自述比区间多 2 年以上，说明简历省略了早期经历，以自述为准
        years = label_years if label_years > from_ranges + 2 else from_ranges
        source = "区间+自述"
    elif from_ranges:
        years, source = from_ranges, "工作区间"
    elif label_years:
        years, source = label_years, "自述"
    else:
        years, source = _years_from_filename(filename), "附件名"

    return round(years, 1), source


def _extract_position_hint(text: str) -> str:
    match = _POSITION_HINT_RE.search(text)
    if not match:
        return ""
    value = re.split(r"[|｜\n,，;；/]", match.group(1))[0].strip(" 　:：·-—-")
    return value if 2 <= len(value) <= 30 else ""


def _name_from_standalone(text: str) -> str:
    """首屏孤立姓名行：2~4 个汉字，且下一行带「男/女/岁/电话/邮箱」等联系特征。"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines[:15]):
        compact = re.sub(r"\s+", "", line.strip(" ·•-—|"))
        if not re.fullmatch(r"[\u4e00-\u9fa5]{2,4}", compact):
            continue
        if any(word in compact for word in _NOT_NAME_WORDS):
            continue
        neighbour = lines[index + 1] if index + 1 < len(lines) else ""
        if re.search(r"男|女|岁|电话|手机|邮箱|求职|应聘|\d{11}", neighbour):
            return compact
    return ""


def _name_from_filename(filename: str) -> str:
    stem = Path(filename or "").stem
    for part in re.split(r"[_\-—\s（(）)]", stem):
        part = part.strip()
        if re.fullmatch(r"[\u4e00-\u9fa5]{2,4}", part) and not any(
            word in part for word in _NOT_NAME_WORDS
        ):
            return part
    return ""


def _extract_name(text: str, filename: str) -> str:
    match = _NAME_LABEL_RE.search(text)
    if match:
        return match.group(1).strip()
    standalone = _name_from_standalone(text)
    if standalone:
        return standalone
    return _name_from_filename(filename)


# ===================================================================== 解析器


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
        """按「先快后全」的顺序提取文本。

        2026-11-13 调整顺序（原先是 unstructured 优先）：本机实测 ``unstructured`` 的
        ``partition()`` 处理 docx 时会长时间卡住（>120s，疑似联网取模型），而解析又跑在
        60s 超时的子进程里 —— 结果是**所有 docx/pdf 简历都超时降级成空文本**，
        BOSS 直聘的 docx 附件直接被判「学历不符」淘汰。

        现在的策略：先用原生库（python-docx / pypdf）快速拿到文本；只有在它**一个字都
        没抽到**（版面复杂、扫描件）时，才把 unstructured 当兜底再试一次。
        这样正常简历 0.1s 出结果，病态文件最多浪费一次兜底超时，不会反过来拖垮正常件。
        """
        warnings: list[str] = []
        suffix = path.suffix.lower()

        if suffix in {".txt", ".md"}:
            return path.read_text(encoding="utf-8", errors="replace"), "text", 1, warnings

        if suffix == ".pdf":
            text, pages = _pdf_text(path, warnings)
            if text.strip():
                return text, "pypdf", pages, warnings
            # 原生库抽不出内容：可能是分栏/表格版式，交给 unstructured 再试一次
            fallback = _try_unstructured(path, warnings)
            if fallback.strip():
                return fallback, "unstructured", 0, warnings
            return text, "pypdf", pages, warnings

        if suffix in {".docx", ".doc"}:
            text = _docx_text(path, warnings)
            if text.strip():
                return text, "python-docx", 0, warnings
            fallback = _try_unstructured(path, warnings)
            return (fallback, "unstructured", 0, warnings) if fallback.strip() else (text, "python-docx", 0, warnings)

        # 未知扩展名：先试 unstructured，再按文本尽力读取
        text = _try_unstructured(path, warnings)
        if text.strip():
            return text, "unstructured", 0, warnings
        try:
            return path.read_text(encoding="utf-8", errors="replace"), "text-fallback", 1, warnings
        except OSError:
            return "", "unsupported", 0, warnings + [f"不支持的文件类型: {suffix}"]

    # -------------------------------------------------------------- 确定性预填
    def prefill(self, text: str, filename: str = "") -> ExtractedCandidate:
        """规则预填：LLM 不可用时它就是抽取结果，可用时作为兜底与交叉校验。"""
        normalized = normalize_text(text)
        sections = split_sections(normalized)

        email_match = _EMAIL_RE.search(normalized)
        phone_match = _PHONE_RE.search(normalized)

        level, school, major, quotes = _extract_education(normalized, sections)
        years, _years_source = _extract_years(normalized, sections, filename)
        position_hint = _extract_position_hint(normalized)
        name = _extract_name(normalized, filename)

        if position_hint:
            quotes.append(SourceQuote(field="position_hint", quote=position_hint[:80]))

        skills = [skill for skill in SKILL_DICTIONARY if skill.lower() in normalized.lower()]
        # 逐字符 PDF 合并后整份简历可能只剩几行，按句号再切一次，保住项目亮点
        highlight_text = normalized
        if _looks_char_split(text or ""):
            highlight_text = re.sub(r"[。；;]", "\n", normalized)
        highlights = _extract_highlights(highlight_text)

        confidence = 0.2
        if email_match or phone_match:
            confidence += 0.35
        if name:
            confidence += 0.15
        if school:
            confidence += 0.1
        if major or level:
            confidence += 0.1
        if position_hint:
            confidence += 0.05
        if years:
            confidence += 0.05

        return ExtractedCandidate(
            name=name,
            email=email_match.group(0).lower() if email_match else "",
            phone=phone_match.group(1) if phone_match else "",
            education_level=level,
            school=school,
            major=major,
            years_experience=years,
            skills=skills,
            project_highlights=highlights,
            summary=_first_paragraph(normalized),
            position_hint=position_hint,
            confidence=min(round(confidence, 2), 0.95),
            source_quotes=quotes,
        )


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
