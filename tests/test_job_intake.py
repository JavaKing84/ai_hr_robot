"""招聘需求接入（``service/job_intake.py``）的单元测试。

覆盖三件事：

1. **模型不可用时的兜底**——LLM 挂了 / 没配，仍然要落一个能用的岗位，而不是报错；
2. **模型输出的清洗**——非法字段、"层级"式的错值、越界阈值都要被剔掉或夹住；
3. **写盘语义**——同一岗位重复录入必须覆盖同一个文件，不能越写越多。

这些都是「主管在飞书里发一句话」的必经路径，出错时表现是「岗位没建成 / 建成了两个」，
在群里看就是机器人不吭声，所以必须有测试兜住。
"""

from __future__ import annotations

import yaml

import pytest

from config.settings import Settings
from schema.config import PositionConfig
from service import job_intake
from service.job_intake import JobIntakeError, JobIntakeService, describe

#: 一段有代表性的 JD：含学历、年限、技能三项，规则解析能抓到前两项。
JD_TEXT = """高级后端工程师
岗位职责：负责招聘系统后端服务的设计与开发，保障高并发下的稳定性。
任职要求：
1. 本科及以上学历，计算机相关专业；
2. 3 年以上后端开发经验，熟悉 Python；
3. 熟悉 PostgreSQL、Redis，有消息队列使用经验。
"""


class StubLLM:
    """只实现 job_intake 用到的两个方法，用来模拟模型返回。"""

    def __init__(self, payload=None, *, vision_text: str = "", error: Exception | None = None) -> None:
        self.payload = payload
        self.vision_text = vision_text
        self.error = error
        self.seen_system: str | None = None

    async def complete_json(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.seen_system = kwargs.get("system")
        return self.payload

    async def complete_vision(self, **kwargs) -> str:
        if self.error is not None:
            raise self.error
        return self.vision_text


def _service(settings: Settings, llm=None) -> JobIntakeService:
    return JobIntakeService(settings=settings, llm=llm)


# ------------------------------------------------------------------ 兜底与输入校验


async def test_short_text_is_rejected(settings: Settings) -> None:
    """太短的消息不该被硬转成岗位，要回一句让人补。"""
    with pytest.raises(JobIntakeError):
        await _service(settings).parse("招人")


async def test_rule_fallback_extracts_education_and_years(settings: Settings) -> None:
    """没有模型时，学历与年限这两个最硬的门槛必须抽出来。"""
    position = await _service(settings, llm=None).parse(JD_TEXT)

    fields = {c.field for c in position.screening.hard_conditions}
    assert "education_level" in fields
    assert "years_experience" in fields
    assert position.screening.threshold == 55
    # 兜底也要把岗位名落下来，否则后面按岗位归位简历时没得匹配
    assert position.title
    assert position.department == "技术部"


async def test_llm_failure_falls_back_to_rules(settings: Settings) -> None:
    """模型抛异常时不能把异常抛给主管，要静默降级成规则解析。"""
    llm = StubLLM(error=TimeoutError("模拟超时"))
    position = await _service(settings, llm=llm).parse(JD_TEXT)

    assert {c.field for c in position.screening.hard_conditions} >= {"education_level"}


async def test_llm_without_title_falls_back_and_keeps_extra_fields(settings: Settings) -> None:
    """模型只吐了阈值这类零散字段时，用规则解析补齐岗位名，但保留模型给的阈值。"""
    llm = StubLLM({"threshold": 72})
    position = await _service(settings, llm=llm).parse(JD_TEXT)

    assert position.title  # 由规则解析补上
    assert position.screening.threshold == 72  # 模型给的值不被覆盖


# ------------------------------------------------------------------ 模型输出清洗


async def test_nested_payload_is_unwrapped(settings: Settings) -> None:
    """模型偶尔把结果包一层 {"position": {...}}，要拆平。"""
    llm = StubLLM(
        {
            "position": {
                "title": "数据分析师",
                "department": "产品部",
                "headcount": 2,
                "threshold": 65,
                "hard_conditions": [
                    {"field": "education_level", "operator": "gte", "value": "本科", "label": "学历本科及以上"}
                ],
            }
        }
    )
    position = await _service(settings, llm=llm).parse(JD_TEXT)

    assert position.title == "数据分析师"
    assert position.department == "产品部"
    assert position.headcount == 2


async def test_single_condition_payload_becomes_hard_condition(settings: Settings) -> None:
    """模型只返回一条规则（有 field 没 title）时，不能把它当成岗位名丢掉。"""
    llm = StubLLM({"field": "years_experience", "operator": "gte", "value": 5, "label": "5 年以上"})
    position = await _service(settings, llm=llm).parse(JD_TEXT)

    assert "years_experience" in {c.field for c in position.screening.hard_conditions}


async def test_illegal_field_and_operator_are_dropped(settings: Settings) -> None:
    """模型编出来的字段/操作符（如 age、like）必须被剔除，不能进筛选规则。"""
    llm = StubLLM(
        {
            "title": "测试岗",
            "hard_conditions": [
                {"field": "age", "operator": "lte", "value": 30},
                {"field": "education_level", "operator": "like", "value": "本科"},
                {"field": "education_level", "operator": "gte", "value": "本科"},
            ],
        }
    )
    position = await _service(settings, llm=llm).parse(JD_TEXT)

    assert [c.field for c in position.screening.hard_conditions] == ["education_level"]


async def test_condition_without_value_is_dropped(settings: Settings) -> None:
    """没有取值的条件等于没有条件，留着只会把所有人判掉。"""
    llm = StubLLM(
        {
            "title": "测试岗",
            "hard_conditions": [
                {"field": "major", "operator": "contains_any", "value": ""},
                {"field": "skills", "operator": "contains_any", "value": []},
                {"field": "skills", "operator": "contains_any", "value": ["Python"]},
            ],
        }
    )
    position = await _service(settings, llm=llm).parse(JD_TEXT)

    assert len(position.screening.hard_conditions) == 1
    assert position.screening.hard_conditions[0].value == ["Python"]


@pytest.mark.parametrize("field", ["major", "school", "skills", "summary"])
async def test_in_operator_is_converted_to_contains_any(settings: Settings, field: str) -> None:
    """「专业 in [计算机]」会把「计算机科学与技术」判成不符合，必须改成包含语义。"""
    llm = StubLLM(
        {
            "title": "测试岗",
            "hard_conditions": [{"field": field, "operator": "in", "value": ["计算机"]}],
        }
    )
    position = await _service(settings, llm=llm).parse(JD_TEXT)

    assert position.screening.hard_conditions[0].operator == "contains_any"


async def test_weighted_without_positive_weight_is_dropped(settings: Settings) -> None:
    """权重为 0 或负数的加分项没有任何作用，留着只会占位。"""
    llm = StubLLM(
        {
            "title": "测试岗",
            "weighted": [
                {"field": "skills", "operator": "contains_any", "value": ["Python"], "weight": 0},
                {"field": "skills", "operator": "contains_any", "value": ["Go"], "weight": -5},
                {"field": "skills", "operator": "contains_any", "value": ["Redis"], "weight": 40},
            ],
        }
    )
    position = await _service(settings, llm=llm).parse(JD_TEXT)

    assert len(position.screening.weighted) == 1
    assert position.screening.weighted[0].weight == 40


@pytest.mark.parametrize(("raw", "expected"), [(150, 100), (-20, 0), ("70.5", 70), ("abc", 55)])
async def test_threshold_is_clamped(settings: Settings, raw, expected) -> None:
    """阈值越界或不是数字时，夹到 0~100（非法值退回默认 55）。"""
    llm = StubLLM({"title": "测试岗", "threshold": raw})
    position = await _service(settings, llm=llm).parse(JD_TEXT)

    assert position.screening.threshold == pytest.approx(expected)


async def test_headcount_accepts_string_number(settings: Settings) -> None:
    """模型常把人数写成 "2"，不能因此整条需求失败。"""
    llm = StubLLM({"title": "测试岗", "headcount": "2"})
    position = await _service(settings, llm=llm).parse(JD_TEXT)

    assert position.headcount == 2


# ------------------------------------------------------------------ 写盘语义


async def test_slug_is_stable_for_chinese_title() -> None:
    """中文标题的 code 必须跨进程稳定，否则「同 code 覆盖」不成立。

    这里直接比对两次调用的结果——若退回内置 hash()，同一进程内也会一致，
    所以额外断言它不是纯数字哈希（内置实现对 str 返回 int，格式化后位数不定）。
    """
    first = job_intake._slug("高级后端工程师")
    second = job_intake._slug("高级后端工程师")

    assert first == second
    assert first.startswith("position_")
    assert len(first.split("_")[-1]) == 8  # md5 前 8 位，定长


def test_slug_keeps_ascii_readable() -> None:
    """纯英文标题保留可读的文件名，不要被哈希掉。"""
    assert job_intake._slug("Backend Engineer II") == "backend_engineer_ii"


async def test_apply_writes_yaml_file(settings: Settings, tmp_path, monkeypatch) -> None:
    """落盘要写出可被 yaml 解析的文件，且内容能反序列化回 PositionConfig。"""
    draft_dir = tmp_path / "positions.d"
    monkeypatch.setattr(job_intake, "DRAFT_DIR", draft_dir)

    position = await _service(settings, llm=None).parse(JD_TEXT)
    path = _service(settings).apply(position)

    assert path.exists()
    assert path.parent == draft_dir
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert PositionConfig.model_validate(payload).title == position.title


async def test_apply_overwrites_same_code(settings: Settings, tmp_path, monkeypatch) -> None:
    """同一岗位录两次只能留一个文件——这是 apply() 的覆盖语义。"""
    draft_dir = tmp_path / "positions.d"
    monkeypatch.setattr(job_intake, "DRAFT_DIR", draft_dir)
    service = _service(settings)

    first = service.apply(await service.parse(JD_TEXT))
    second = service.apply(await service.parse(JD_TEXT))

    assert first == second
    assert len(list(draft_dir.glob("*.yaml"))) == 1


# ------------------------------------------------------------------ 回复文案与截图


async def test_describe_mentions_title_department_and_threshold(settings: Settings) -> None:
    """确认文案要能让主管核对关键规则，缺了就等于没确认。"""
    position = await _service(settings, llm=None).parse(JD_TEXT)
    text = describe(position)

    assert position.title in text
    assert position.department in text
    assert "55" in text


async def test_parse_image_without_llm_explains_how_to_proceed(settings: Settings) -> None:
    """没配模型时要回一句能照做的话，而不是抛栈。"""
    with pytest.raises(JobIntakeError) as excinfo:
        await _service(settings, llm=None).parse_image(b"fake-png")

    assert "文字" in str(excinfo.value)


async def test_parse_image_uses_vision_then_text_pipeline(settings: Settings) -> None:
    """截图先转录成文字，再走同一套文本解析——两条入口的结论必须一致。"""
    llm = StubLLM(vision_text=JD_TEXT)
    position = await _service(settings, llm=llm).parse_image(b"fake-png", mime="image/jpeg")

    assert {c.field for c in position.screening.hard_conditions} >= {"education_level"}
