"""pytest 公共夹具：临时 SQLite、应用容器、HTTP 客户端。

所有外部系统都跑在 mock 模式（飞书不发请求、邮件写本地 outbox、LLM 关闭），
因此测试完全离线、可重复。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from client.mailbox import InboundEmail, parse_email_bytes
from config.settings import BASE_DIR, Settings
from core.db import init_models
from core.deps import Container, build_container, build_services
from factory.app import create_app
from repository.position import PositionRepository

CONFIG_PATH = BASE_DIR / "config" / "positions.yaml"
#: 测试用业务配置：与线上 positions.yaml 规则一致，但主管身份是 mock 值。
#: 线上那份被 HR 改成真实 open_id / 163 邮箱后，测试里硬编码的 ou_mock_* 断言会集体失效，
#: 所以测试与线上配置解耦（见 tests/fixtures/positions.test.yaml 顶部说明）。
TEST_CONFIG_PATH = Path(__file__).resolve().parent / "fixtures" / "positions.test.yaml"
TEMPLATES_DIR = BASE_DIR / "config" / "templates"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
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
    )


@pytest.fixture
async def container(settings: Settings) -> AsyncIterator[Container]:
    container = build_container(settings)
    await init_models(container.engine)
    async with container.session_factory() as session:
        await PositionRepository(session).sync_from_config(settings.load_company_config())
        await session.commit()
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
