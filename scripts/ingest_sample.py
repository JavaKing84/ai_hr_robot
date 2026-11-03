"""导入模拟简历（演示与联调用，数据明显虚构）。

用法：
    python scripts/ingest_sample.py            # 写入 var/mock_inbox/*.eml，等下一轮定时任务收取
    python scripts/ingest_sample.py --direct   # 直接走入库管道（不等轮询）
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from email.message import EmailMessage
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts._bootstrap import bootstrap  # noqa: E402

from core.db import init_models
from core.deps import build_services
from core.logging import configure_logging
from repository.position import PositionRepository


def _resume(sample: dict) -> str:
    lines = [
        f"姓名：{sample['name']}",
        f"邮箱：{sample['email']}",
        f"电话：{sample['phone']}",
        f"学历：{sample['education']}",
        f"毕业院校：{sample['school']}",
        f"专业：{sample['major']}",
        f"工作年限：{sample['years']}年",
        f"技能：{sample['skills']}",
        "项目经验：",
        *[f"- {item}" for item in sample["projects"]],
        f"求职意向：{sample['position']}",
    ]
    return "\n".join(lines)


SAMPLES: list[dict] = [
    {
        "name": "张明",
        "email": "zhangming@example.com",
        "phone": "13800001111",
        "education": "本科",
        "school": "华北电力大学",
        "major": "计算机科学与技术",
        "years": 6,
        "skills": "Python、FastAPI、PostgreSQL、Redis、Docker、微服务",
        "projects": ("主导订单服务重构，QPS 提升 3 倍，支撑高并发场景", "设计分布式任务调度平台，负责稳定性优化"),
        "position": "高级后端工程师",
    },
    {
        "name": "李四",
        "email": "lisi@example.com",
        "phone": "13800002222",
        "education": "本科",
        "school": "北京邮电大学",
        "major": "软件工程",
        "years": 5,
        "skills": "Python、Java、MySQL、Redis、微服务",
        "projects": ("负责订单系统性能优化，接口耗时下降 60%", "主导微服务拆分与灰度发布"),
        "position": "高级后端工程师",
    },
    {
        "name": "王芳",
        "email": "wangfang@example.com",
        "phone": "13800003333",
        "education": "大专",
        "school": "某职业技术学院",
        "major": "电子商务",
        "years": 2,
        "skills": "PS、Excel",
        "projects": ("负责门店运营数据整理",),
        "position": "高级后端工程师",
    },
    {
        "name": "赵敏",
        "email": "zhaomin@example.com",
        "phone": "13800004444",
        "education": "硕士",
        "school": "清华大学",
        "major": "人工智能",
        "years": 3,
        "skills": "PyTorch、TensorFlow、深度学习、NLP、自然语言处理、大模型、Transformer",
        "projects": (
            "负责简历解析算法上线，召回率提升 18%",
            "主导推荐系统召回优化，落地深度学习模型",
            "负责自然语言处理方向研究，在顶会发表论文一篇",
        ),
        "position": "算法工程师",
    },
    {
        "name": "陈静",
        "email": "chenjing@example.com",
        "phone": "13800005555",
        "education": "本科",
        "school": "中国人民大学",
        "major": "信息管理与信息系统",
        "years": 4,
        "skills": "需求分析、竞品分析、PRD、数据分析、SQL、用户研究",
        "projects": ("从 0 到 1 搭建 B 端招聘管理平台", "主导版本规划，客户续费率提升 12%"),
        "position": "产品经理",
    },
    {
        "name": "周涛",
        "email": "zhoutao@example.com",
        "phone": "13800006666",
        "education": "大专",
        "school": "北京城市学院",
        "major": "市场营销",
        "years": 2,
        "skills": "内容运营、活动策划、社群、投放、短视频、数据分析",
        "projects": ("负责社群拉新，三个月涨粉 5 万", "负责双十一投放活动，GMV 破 200 万"),
        "position": "市场运营专员",
    },
]


def _email_bytes(sample: dict) -> bytes:
    message = EmailMessage()
    message["From"] = f"{sample['name']} <{sample['email']}>"
    message["To"] = "hr@xingye-test.com"
    message["Subject"] = f"应聘{sample['position']}-{sample['name']}"
    message["Message-ID"] = f"<{uuid.uuid4().hex}@example.com>"
    message.set_content("您好，附件是我的简历，请查收。")
    message.add_attachment(
        _resume(sample).encode("utf-8"),
        maintype="text",
        subtype="plain",
        filename=f"{sample['name']}-{sample['position']}.txt",
    )
    return message.as_bytes()


async def main(direct: bool) -> None:
    container = bootstrap()
    settings = container.settings
    await init_models(container.engine)

    if not direct:
        settings.mock_inbox_dir.mkdir(parents=True, exist_ok=True)
        for sample in SAMPLES:
            path = settings.mock_inbox_dir / f"sample-{sample['name']}.eml"
            path.write_bytes(_email_bytes(sample))
        print(f"已写入 {len(SAMPLES)} 封模拟投递邮件到 {settings.mock_inbox_dir}")
        print("下一步：POST /api/v1/recruitment/run 触发一轮闭环")
        await container.aclose()
        return

    async with container.session_factory() as session:
        await PositionRepository(session).sync_from_config(settings.load_company_config())
        await session.commit()
        services = build_services(session, container)
        for sample in SAMPLES:
            outcome = await services.ingestion.ingest_resume_bytes(
                _resume(sample).encode("utf-8"),
                f"{sample['name']}-{sample['position']}.txt",
                source="sample",
            )
            status = outcome.candidate.status if outcome.candidate else outcome.note
            print(f"{sample['name']}｜{sample['position']}｜{status}")
    await container.aclose()


if __name__ == "__main__":
    configure_logging()
    parser = argparse.ArgumentParser(description="导入模拟简历")
    parser.add_argument("--direct", action="store_true", help="直接入库，不走邮箱轮询")
    args = parser.parse_args()
    asyncio.run(main(args.direct))
