"""pytest 公共夹具：PostgreSQL 测试库、应用容器、HTTP 客户端。

所有外部系统都跑在 mock 模式（飞书不发请求、邮件写本地 outbox、LLM 关闭），
因此测试完全离线、可重复；**只有数据库是真实 PostgreSQL**（与生产同引擎，
提前暴露 PG 才有的差异：字段长度、JSON、外键、序列）。

测试库的生命周期全自动，不需要人工建库或改任何配置：

1. 从 ``.env`` 的 ``DATABASE_URL`` 派生 ``<库名>_test``（如 ``hr_ai_robot_test``）；
2. 库不存在就自动 ``CREATE DATABASE``；
3. 每个用例开始前 ``TRUNCATE ... RESTART IDENTITY CASCADE``，等价于一个全新的库；
4. 若 PostgreSQL 没启动，会自动尝试 ``docker compose up -d postgres``，
   仍失败则打印一条可以直接照抄的启动命令后退出（不会给你一堆看不懂的堆栈）。

**三道防线，保证永远碰不到生产库**：

- ① 库名必须落在 ``*_test`` / ``*_e2e`` 上，否则直接拒绝运行；
- ② 本机 PostgreSQL 可用时，一律换成本机同名库（``hr:hr@localhost:5432``），
  绝不在共享服务器的 ``TRUNCATE`` 上打转；
- ③ 本机不可用、只能落在远程时，会打印一段醒目提示（说明会清哪个库），
  想强制本机可设 ``TEST_REQUIRE_LOCAL=1``。

可用环境变量覆盖：
- ``TEST_DATABASE_URL``：直接指定测试库（跳过派生，但库名后缀仍受①约束）；
- ``TEST_PG_AUTOSTART=0``：禁止自动拉起 docker（CI 里想自己控制时用）；
- ``TEST_REQUIRE_LOCAL=1``：测试库不在本机就直接退出，不静默降级到远程。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import make_url

from client.mailbox import InboundEmail, parse_email_bytes
from config.settings import BASE_DIR, Settings
from core.db import (
    assert_disposable_database,
    build_engine,
    derive_test_database_url,
    ensure_database_exists,
    init_models,
    prefer_local_test_target,
    truncate_all_tables,
)
from core.deps import Container, build_container, build_services
from factory.app import create_app
from repository.position import PositionRepository

CONFIG_PATH = BASE_DIR / "config" / "positions.yaml"
#: 测试用业务配置：与线上 positions.yaml 规则一致，但主管身份是 mock 值。
#: 线上那份被 HR 改成真实 open_id / 163 邮箱后，测试里硬编码的 ou_mock_* 断言会集体失效，
#: 所以测试与线上配置解耦（见 tests/fixtures/positions.test.yaml 顶部说明）。
TEST_CONFIG_PATH = Path(__file__).resolve().parent / "fixtures" / "positions.test.yaml"
TEMPLATES_DIR = BASE_DIR / "config" / "templates"


# ------------------------------------------------------- PostgreSQL 测试库准备


def _mask(url: str) -> str:
    """把连接串里的密码换成 ``***``，用于打印。"""
    return make_url(url).render_as_string(hide_password=True)


def _resolve_test_database_url() -> str:
    """确定测试库地址，三道防线保证绝不碰生产库。

    优先级：显式 ``TEST_DATABASE_URL`` > 本机同名库（可用时）> 派生的远程库。
    无论走哪条路，库名都必须以 ``_test`` / ``_e2e`` 结尾，否则直接拒绝运行。
    """
    explicit = (os.getenv("TEST_DATABASE_URL") or "").strip()
    if explicit:
        assert_disposable_database(explicit, purpose="测试")
        return explicit

    # 读 .env 里的 DATABASE_URL（环境变量 DATABASE_URL 优先级更高，由 Settings 处理）
    base_url = Settings().database_url
    if not base_url.startswith("postgresql"):
        pytest.exit(
            "测试必须使用 PostgreSQL，当前 DATABASE_URL 不是 PG：\n"
            f"  {base_url}\n"
            "请把 .env 的 DATABASE_URL 改成 "
            "postgresql+asyncpg://hr:hr@localhost:5432/hr_ai_robot，"
            "或设置环境变量 TEST_DATABASE_URL 指向测试库。",
            returncode=1,
        )

    # 第一道防线：库名后缀。生产库不可能是 *_test / *_e2e。
    derived = derive_test_database_url(base_url)
    assert_disposable_database(derived, purpose="测试")

    # 第二道防线：本机能用就绝不用远程（测试会频繁 TRUNCATE 目标库）。
    url, reason = prefer_local_test_target(derived)
    if url != derived:
        print(f"\n[conftest] 测试库：{reason} → {_mask(url)}", flush=True)
        return url

    # 第三道防线：确实要落在远程时，把风险打印出来，不静默。
    if (os.getenv("TEST_REQUIRE_LOCAL") or "").strip().lower() in {"1", "true", "yes"}:
        pytest.exit(
            f"已设置 TEST_REQUIRE_LOCAL=1，但测试库会落在远程：{_mask(derived)}\n"
            f"原因：{reason}\n"
            "请先启动本机 PostgreSQL（Docker Desktop → docker compose up -d postgres），"
            "或取消该环境变量。",
            returncode=1,
        )

    print(
        "\n"
        + "=" * 78
        + "\n[conftest] 测试库落在共享服务器上\n"
        f"           目标：{_mask(derived)}\n"
        f"           原因：{reason}\n"
        "           影响：每个用例前都会 TRUNCATE 这个库（生产库 hr_ai_robot 不受影响）\n"
        "           想跑在本机：启动 Docker Desktop 后会自动切换；\n"
        "           想强制本机：设 TEST_REQUIRE_LOCAL=1\n"
        + "=" * 78
        + "\n",
        flush=True,
    )
    return derived


def _probe(url: str) -> None:
    """连一次库（不存在则创建）；连不上直接抛异常。"""
    asyncio.run(ensure_database_exists(url))


def _try_start_postgres(url: str, attempts: int = 40, interval: float = 1.0) -> None:
    """尝试用 docker compose 拉起 postgres，并轮询到可用为止。"""
    if shutil.which("docker") is None:
        raise RuntimeError("本机找不到 docker 命令")

    result = subprocess.run(
        ["docker", "compose", "up", "-d", "postgres"],
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise RuntimeError("docker compose up -d postgres 执行失败：" + (detail[-1] if detail else "未知原因"))

    for index in range(attempts):
        try:
            _probe(url)
            return
        except Exception:  # noqa: BLE001 - 容器启动中，继续轮询
            if index == attempts - 1:
                raise
            time.sleep(interval)


def _prepare_test_database(url: str) -> None:
    """确保测试库可连接、库存在；必要时自动拉起 docker。"""
    try:
        _probe(url)
        return
    except Exception as first_error:  # noqa: BLE001
        autostart = (os.getenv("TEST_PG_AUTOSTART") or "1").strip().lower()
        if autostart in {"0", "false", "no", "off"}:
            pytest.exit(_pg_unavailable_message(url, first_error, autostart_disabled=True), returncode=1)
        try:
            _try_start_postgres(url)
            return
        except Exception as start_error:  # noqa: BLE001
            pytest.exit(
                _pg_unavailable_message(url, first_error, startup_error=start_error),
                returncode=1,
            )


def _pg_unavailable_message(
    url: str,
    error: Exception,
    *,
    autostart_disabled: bool = False,
    startup_error: Exception | None = None,
) -> str:
    lines = [
        "",
        "=" * 78,
        "测试需要 PostgreSQL，但当前连不上。",
        "=" * 78,
        f"目标库：{url}",
        f"错误：{type(error).__name__}: {error}",
    ]
    if autostart_disabled:
        lines.append("（已设置 TEST_PG_AUTOSTART=0，因此不自动启动数据库）")
    if startup_error is not None:
        lines.append(f"自动启动失败：{type(startup_error).__name__}: {startup_error}")
    lines += [
        "",
        "请按顺序做这两步，然后重新运行 pytest：",
        "  1) 确认 Docker Desktop 已启动",
        f"  2) 在 {BASE_DIR} 目录执行：docker compose up -d postgres",
        "",
        "不需要手工建库：测试会自动创建 hr_robot_test 并在每个用例前清空数据。",
        "=" * 78,
        "",
    ]
    return "\n".join(lines)


async def _create_schema(url: str) -> None:
    """会话开始时按 ORM 模型建一次表（幂等）。"""
    engine = build_engine(url)
    try:
        await init_models(engine)
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """会话级：准备 PostgreSQL 测试库并建表，返回连接串。"""
    url = _resolve_test_database_url()
    _prepare_test_database(url)
    asyncio.run(_create_schema(url))
    return url


# --------------------------------------------------------------------- 核心夹具


@pytest.fixture
def settings(tmp_path: Path, test_database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        database_url=test_database_url,
        var_dir=tmp_path / "var",
        config_path=TEST_CONFIG_PATH,
        templates_dir=TEMPLATES_DIR,
        feishu_mode="mock",
        mail_mode="mock",
        llm_mode="off",
        # 测试聚焦「发送路径」本身，关掉人工确认闸门；
        # 闸门自身的用例见 tests/test_interview_confirm_gate.py
        interview_notice_require_confirm=False,
        scheduler_enabled=False,
        inbound_poll_seconds=0,
        schedule_cron="0 9 * * *",
        # 测试库由 init_models 按 ORM 直接建表；不要在这里跑 alembic
        # （否则会拿迁移脚本去改一个刚刚 create_all 出来的库，版本表也对不上）
        auto_migrate=False,
    )


@pytest.fixture
async def container(settings: Settings) -> AsyncIterator[Container]:
    container = build_container(settings)
    # 每个用例都从「空表 + 自增 ID 归零」开始，等价于一个全新的库，
    # 用例之间互不影响，顺序可以随便打乱。
    await truncate_all_tables(container.engine)
    await init_models(container.engine)
    async with container.session_factory() as session:
        await PositionRepository(session).sync_from_config(settings.load_company_config())
        await session.commit()
        # 夹具自检：岗位没同步进来时，业务会输出「没有在招职位（配置为空）」，
        # 那个报错指向业务配置，会让人查错方向；这里提前失败并说清是夹具问题。
        if not await PositionRepository(session).list_active():
            pytest.fail(
                "测试夹具异常：岗位配置同步后岗位表仍为空。\n"
                f"  配置：{settings.config_path}\n"
                f"  库：{settings.database_url}\n"
                "通常是 positions.test.yaml 解析为空，或数据库连接指向了别的库。"
            )
    yield container
    await container.aclose()


@pytest.fixture
async def app(container: Container):
    return create_app(container.settings, container=container)


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture
async def session(container: Container):
    async with container.session_factory() as session:
        yield session


@pytest.fixture
async def services(container: Container):
    async with container.session_factory() as session:
        bundle = build_services(session, container)
        yield bundle
        await session.rollback()


# --------------------------------------------------------------------- 构造工具


def resume_text(
    *,
    name: str = "张明",
    email: str = "zhangming@example.com",
    phone: str = "13800001111",
    education: str = "本科",
    school: str = "华北电力大学",
    major: str = "计算机科学与技术",
    years: int = 6,
    skills: str = "Python、FastAPI、PostgreSQL、Redis、Docker、微服务",
    position_title: str = "高级后端工程师",
    projects: tuple[str, ...] = (
        "主导订单服务重构，QPS 提升 3 倍，支撑高并发场景",
        "设计分布式任务调度平台，负责稳定性优化",
    ),
) -> str:
    lines = [
        f"姓名：{name}",
        f"邮箱：{email}",
        f"电话：{phone}",
        f"学历：{education}",
        f"毕业院校：{school}",
        f"专业：{major}",
        f"工作年限：{years}年",
        f"技能：{skills}",
        "项目经验：",
        *[f"- {item}" for item in projects],
        f"求职意向：{position_title}",
    ]
    return "\n".join(lines)


def resume_email_bytes(
    *,
    resume: str | None = None,
    sender: str = "candidate@example.com",
    subject: str = "应聘高级后端工程师-张明",
    filename: str = "张明-高级后端工程师.txt",
    message_id: str | None = None,
) -> bytes:
    """构造一封“候选人投递简历”的原始邮件（带附件）。"""
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = sender
    message["To"] = "hr@xingye-test.com"
    message["Subject"] = subject
    message["Message-ID"] = message_id or f"<{uuid.uuid4().hex}@example.com>"
    message["Date"] = "Tue, 03 Nov 2026 09:00:00 +0800"
    message.set_content("您好，附件是我的简历，请查收。")
    message.add_attachment(
        (resume or resume_text()).encode("utf-8"),
        maintype="text",
        subtype="plain",
        filename=filename,
    )
    return message.as_bytes()


def drop_resume_email(settings: Settings, **kwargs) -> Path:
    settings.mock_inbox_dir.mkdir(parents=True, exist_ok=True)
    path = settings.mock_inbox_dir / f"resume-{uuid.uuid4().hex[:8]}.eml"
    path.write_bytes(resume_email_bytes(**kwargs))
    return path


def feishu_text_event(
    *,
    open_id: str,
    text: str,
    message_id: str = "om_test_1",
    chat_id: str = "oc_test_1",
    event_type: str = "im.message.receive_v1",
) -> dict:
    return {
        "schema": "2.0",
        "header": {
            "event_id": uuid.uuid4().hex,
            "event_type": event_type,
            "create_time": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
            "token": "",
        },
        "event": {
            "sender": {"sender_id": {"open_id": open_id}, "sender_type": "user"},
            "message": {
                "message_id": message_id,
                "chat_id": chat_id,
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "create_time": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
            },
        },
    }


def feishu_card_event(*, open_id: str, value: dict, message_id: str = "om_card_1") -> dict:
    return {
        "schema": "2.0",
        "header": {"event_type": "card.action.trigger", "create_time": "1762000000000"},
        "event": {
            "operator": {"open_id": open_id},
            "action": {"tag": "button", "value": value},
            "context": {"open_message_id": message_id, "open_chat_id": "oc_test_1"},
        },
    }


def read_outbox(settings: Settings) -> list[InboundEmail]:
    files = sorted(settings.mail_outbox_dir.glob("*.eml"))
    return [parse_email_bytes(path.read_bytes(), raw_path=str(path)) for path in files]
