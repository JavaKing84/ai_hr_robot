"""群机器人推送渠道（2026-11-13 改动）。

原渠道是「按主管 open_id 发私聊」，现在默认走群机器人 webhook：
- 文本：POST webhook，msg_type=text；
- 文件：webhook 不支持，必须用应用身份 + chat_id 上传；
- 没配 chat_id 时降级为文本提示，不静默丢文件；
- mock 模式下两个入口都不发真实请求。
"""

from __future__ import annotations

import pytest

from client.feishu import FeishuClient
from config.settings import Settings


def _settings(**kwargs) -> Settings:
    base = dict(
        _env_file=None,
        feishu_mode="live",
        feishu_app_id="cli_test",
        feishu_app_secret="secret",
        feishu_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
        feishu_push_target="group",
    )
    base.update(kwargs)
    return Settings(**base)


@pytest.mark.asyncio
async def test_group_push_uses_webhook(monkeypatch) -> None:
    client = FeishuClient(_settings(feishu_mention_open_ids="ou_a,ou_b"))
    captured: dict = {}

    async def fake_post(payload, *, kind):
        captured["payload"] = payload
        from client.feishu import FeishuSendResult

        return FeishuSendResult(ok=True, mode="live", message_id="group-x")

    monkeypatch.setattr(client, "_post_webhook", fake_post)
    result = await client.push_text("ou_supervisor", "本轮通过 3 份简历")

    assert result.ok and result.message_id == "group-x"
    payload = captured["payload"]
    assert payload["msg_type"] == "text"
    text = payload["content"]["text"]
    # 群模式下 @ 指定人 + 正文都在同一条 text 里
    assert '<at user_id="ou_a">' in text and '<at user_id="ou_b">' in text
    assert text.endswith("本轮通过 3 份简历")


@pytest.mark.asyncio
async def test_group_push_falls_back_to_user_when_target_user(monkeypatch) -> None:
    client = FeishuClient(_settings(feishu_push_target="user"))
    called: list[str] = []

    async def fake_send_text(open_id, text):
        called.append(open_id)
        from client.feishu import FeishuSendResult

        return FeishuSendResult(ok=True, mode="mock", message_id="mock")

    monkeypatch.setattr(client, "send_text", fake_send_text)
    result = await client.push_text("ou_supervisor", "hi")

    assert result.ok and called == ["ou_supervisor"]


@pytest.mark.asyncio
async def test_group_file_without_chat_id_degrades(monkeypatch, tmp_path) -> None:
    path = tmp_path / "简历.zip"
    path.write_bytes(b"PK\x03\x04")
    client = FeishuClient(_settings(feishu_chat_id=""))

    notices: list[str] = []

    async def fake_group_text(text, *, at_all=False):
        notices.append(text)
        from client.feishu import FeishuSendResult

        return FeishuSendResult(ok=True, mode="live", message_id="g")

    monkeypatch.setattr(client, "send_group_text", fake_group_text)
    result = await client.send_group_file(path)

    assert result.ok is False
    assert "FEISHU_CHAT_ID" in result.error
    # 关键：文件没上传成功，但必须在群里说一句，不能静默丢简历包
    assert notices and "简历.zip" in notices[0]


@pytest.mark.asyncio
async def test_mock_mode_never_calls_network(monkeypatch) -> None:
    client = FeishuClient(_settings(feishu_mode="mock"))

    async def boom(*args, **kwargs):
        raise AssertionError("mock 模式不应该发网络请求")

    monkeypatch.setattr(client, "_post_webhook", boom)
    result = await client.push_text("ou_x", "hi")
    assert result.ok and result.mode == "mock"
