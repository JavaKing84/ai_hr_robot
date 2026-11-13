"""演练：造三份跨部门简历，走真实导入（含真实筛选），产出待派发名单。

为什么必须落成文件而不是 `python - <<PY`：
简历解析在 Windows 上走 spawn 子进程，子进程要能重新导入主模块；
stdin（`-`）方式没有 __main__ 文件路径，子进程会崩。这是运行方式问题，
不是业务逻辑问题，所以固定用脚本文件跑。

筛选在 ingest_resume_bytes 内部就完成（IngestOutcome.screening），
不重复调 screen_all，避免筛两遍。

用法：
    ./.venv/Scripts/python.exe scripts/demo_ingest.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 直接 `python scripts/demo_ingest.py` 时 sys.path[0] 是 scripts/，项目根不在里面，
# 所以先补项目根，否则 `import core` 直接 ModuleNotFoundError。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.deps import build_container, build_services, set_container
from repository.candidate import CandidateRepository

CYCLE_ID = 202611140500  # 本轮演练批次，与历史批次分开

RESUMES: list[tuple[str, str, str]] = [
    (
        "backend_senior",
        "孙浩然-高级后端工程师.txt",
        """孙浩然
求职意向：高级后端工程师 / 服务端开发
电话：137-0000-8899    邮箱：sunhaoran.dev@example.com
教育背景：北京理工大学  计算机科学与技术  本科  2013.09-2017.06
工作经历：
2020.06-至今  某电商中台公司  高级后端工程师
  负责订单与库存微服务，使用 Java + Spring Cloud + MySQL + Redis，日订单 200 万；
  主导下单链路重构，接口 P99 从 800ms 降到 120ms；
  用 Docker + Kubernetes 完成服务容器化，发布效率提升 3 倍。
2017.07-2020.05  某金融科技公司  后端开发工程师
  负责支付对账服务，Python + FastAPI + PostgreSQL，日均处理 50 万笔；
  建设自动化测试与灰度发布流程，线上事故下降 60%。
技能：Java、Spring、Spring Cloud、Python、FastAPI、PostgreSQL、MySQL、Redis、Docker、Kubernetes、微服务、分布式
""",
    ),
    (
        "product_manager",
        "赵敏-产品经理.txt",
        """赵敏
求职意向：产品经理（B 端 / 企业服务）
电话：138-0000-1234    邮箱：zhaomin.pm@example.com
教育背景：北京邮电大学  信息管理与信息系统  本科  2015.09-2019.06
工作经历：
2021.03-至今  某企业服务 SaaS 公司  产品经理
  负责 B 端工单与流程中台，从 0 到 1 完成需求分析、PRD 撰写与版本规划；
  主导竞品分析，推动核心流程重构，客户留存提升 18%；
  搭建数据分析看板（SQL + 埋点），用数据驱动版本优先级。
2019.07-2021.02  某互联网公司  产品助理 → 产品经理
  负责用户研究与需求池管理，输出 PRD 30+ 份，参与中台能力建设。
技能：需求分析、PRD、竞品分析、数据分析、用户研究、Axure、SQL、版本规划
""",
    ),
    (
        "marketing_specialist",
        "周晓-市场运营.txt",
        """周晓
求职意向：市场运营 / 内容运营
电话：139-0000-5678    邮箱：zhouxiao.mkt@example.com
教育背景：北京城市学院  市场营销  大专  2018.09-2021.06
工作经历：
2023.04-至今  某消费品牌  新媒体运营
  负责小红书 + 抖音内容运营与活动策划，单条内容曝光 120 万，涨粉 10 万+；
  管理 20 个社群，策划裂变活动，转化率提升 23%；
  投放预算 30 万/月，持续优化 ROI 至 1:4.2。
2021.07-2023.03  某广告公司  运营专员
  负责 SEO 与内容投放，输出数据周报，复盘活动效果。
技能：内容运营、活动策划、社群运营、新媒体、投放、SEO、数据分析、短视频
""",
    ),
]


async def main() -> None:
    container = set_container(build_container())
    print(f"mail_mode={container.settings.mail_mode}  feishu_mode={container.settings.feishu_mode}")
    print(f"cycle_id={CYCLE_ID}\n")

    created_ids: list[int] = []
    async with container.session_factory() as session:
        services = build_services(session, container)

        for code, filename, text in RESUMES:
            outcome = await services.ingestion.ingest_resume_bytes(
                text.encode("utf-8"),
                filename,
                position_code=code,
                source="manual",
                cycle_id=CYCLE_ID,
            )
            cand = outcome.candidate
            scr = outcome.screening
            print(
                f"[ingest] {filename}\n"
                f"         id={getattr(cand, 'id', None)} created={outcome.created} "
                f"dup={outcome.is_duplicate} pos={outcome.position_code}\n"
                f"         screening={None if scr is None else ('通过' if scr.passed else '未通过', scr.score, '硬性未过:' + ','.join(scr.hard_failed) if scr.hard_failed else '')}\n"
                f"         note={outcome.note}"
            )
            if cand and cand.id:
                created_ids.append(cand.id)
        await session.commit()

        print("\n[最终状态]")
        repo = CandidateRepository(session)
        for cid in created_ids:
            c = await repo.get(cid)
            if c:
                await session.refresh(c)
                print(
                    f"  {c.id}  {c.name:<6} {c.position_code:<22} {c.status:<16} "
                    f"score={c.score:<6} dept={c.department}"
                )

    await container.engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
