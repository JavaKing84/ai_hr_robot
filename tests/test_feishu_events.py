"""飞书事件归一化测试：文本、富文本、卡片回调、token 校验。"""

from __future__ import annotations

import json

import pytest

from client.feishu import FeishuClient, content_to_text
from core.exceptions import ConfigurationError
from tests.conftest import feishu_card_event, feishu_text_event


@pytest.fixture
def feishu(settings) -> FeishuClient:
    return FeishuClient(settings)


def test_normalize_text_message(feishu: FeishuClient) -> None:
    payload = feishu_text_event(open_id="ou_mock_zhangwei", text="请通知张明，下周三下午14:00来公司面试")
    inbound = feishu.normalize_event(payload)
    assert inbound is not None
    assert inbound.channel == "feishu"
    assert inbound.sender_id == "ou_mock_zhangwei"
    assert "请通知张明" in inbound.text
    assert inbound.message_id == "om_test_1"
    assert inbound.thread_id == "oc_test_1"


def test_normalize_message_with_mention_placeholder(feishu: FeishuClient) -> None:
    payload = feishu_text_event(open_id="ou_mock_zhangwei", text="@_user_1 请通知张明，下周三下午14:00面试")
    payload["event"]["message"]["mentions"] = [
        {"key": "@_user_1", "name": "HR机器人", "id": {"open_id": "ou_bot"}}
    ]
    inbound = feishu.normalize_event(payload)
    assert inbound is not None
    assert inbound.text.startswith("HR机器人 请通知张明")


def test_normalize_post_message(feishu: FeishuClient) -> None:
    payload = feishu_text_event(open_id="ou_mock_zhangwei", text="")
    payload["event"]["message"]["message_type"] = "post"
    payload["event"]["message"]["content"] = json.dumps(
        {
            "title": "面试安排",
            "content": [[{"tag": "text", "text": "请通知张明"}, {"tag": "text", "text": "下周三下午14:00"}]],
        },
        ensure_ascii=False,
    )
    inbound = feishu.normalize_event(payload)
    assert inbound is not None
    assert "面试安排" in inbound.text
    assert "下周三下午14:00" in inbound.text


def test_normalize_card_action(feishu: FeishuClient) -> None:
    payload = feishu_card_event(
        open_id="ou_mock_zhangwei",
        value={"action": "confirm_interview", "candidate_names": ["张明"], "interview_time": "2026-11-11 14:00"},
    )
    inbound = feishu.normalize_event(payload)
    assert inbound is not None
    assert inbound.kind == "card_action"
    assert inbound.action["candidate_names"] == ["张明"]
    assert "confirm_interview" in inbound.text


def test_unknown_event_returns_none(feishu: FeishuClient) -> None:
    assert feishu.normalize_event({"header": {"event_type": "im.chat.updated_v1"}, "event": {}}) is None


def test_decode_webhook_challenge(feishu: FeishuClient) -> None:
    body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
    payload = feishu.decode_webhook(body, {})
    assert payload["challenge"] == "abc123"


def test_decode_webhook_rejects_bad_json(feishu: FeishuClient) -> None:
    with pytest.raises(ConfigurationError):
        feishu.decode_webhook(b"not-json", {})


def test_decode_webhook_checks_token(settings) -> None:
    settings.feishu_verification_token = "expected-token"
    client = FeishuClient(settings)
    body = json.dumps({"header": {"event_type": "x", "token": "wrong"}}).encode()
    with pytest.raises(ConfigurationError):
        client.decode_webhook(body, {})


def test_content_to_text_handles_empty() -> None:
    assert content_to_text("text", None) == ""
    assert content_to_text("image", json.dumps({"file_key": "k"})) == ""
