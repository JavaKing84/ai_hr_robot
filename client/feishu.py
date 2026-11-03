"""飞书客户端：官方 SDK（lark_oapi）负责 token / 加解密 / 接口调用，本文件只做封装与归一化。

两种模式：
- ``FEISHU_MODE=live``：真实调用开放平台接口（发送消息、上传文件、下载资源）；
- ``FEISHU_MODE=mock``：不发真实请求，返回合成 message_id，由出站留痕记录在 messages 表。

入站事件统一转成 ``schema.message.InboundMessage``（借鉴通道归一架构，不复制第三方代码）。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from config.settings import Settings
from core.exceptions import ConfigurationError, ExternalServiceError
from core.logging import get_logger, log_extra
from schema.message import Attachment, InboundMessage

logger = get_logger(__name__)


@dataclass
class FeishuSendResult:
    ok: bool
    mode: str
    message_id: str = ""
    error: str = ""


class FeishuClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Any = None

    # ------------------------------------------------------------------ SDK
    @property
    def mode(self) -> str:
        return self.settings.feishu_mode

    def _sdk(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.settings.feishu_live:
            raise ConfigurationError("飞书客户端处于 mock 模式")
        if not (self.settings.feishu_app_id and self.settings.feishu_app_secret):
            raise ConfigurationError("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET，无法使用 live 模式")
        try:
            import lark_oapi as lark
        except ImportError as exc:  # pragma: no cover - 取决于运行环境
            raise ConfigurationError("未安装 larksuite-oapi，无法使用 live 模式") from exc

        self._client = (
            lark.Client.builder()
            .app_id(self.settings.feishu_app_id)
            .app_secret(self.settings.feishu_app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )
        return self._client

    # ------------------------------------------------------------ 出站：文本
    async def send_text(self, open_id: str, text: str) -> FeishuSendResult:
        """给主管发纯文本消息（不阻塞事件循环：SDK 同步调用放到线程池）。"""
        if not open_id:
            return FeishuSendResult(ok=False, mode=self.mode, error="缺少 open_id")
        if not self.settings.feishu_live:
            message_id = f"mock-feishu-{uuid.uuid4().hex[:16]}"
            logger.info(
                "feishu.mock.send_text",
                extra=log_extra(open_id=open_id, chars=len(text), message_id=message_id),
            )
            return FeishuSendResult(ok=True, mode="mock", message_id=message_id)

        def _call() -> Any:
            from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

            request = (
                CreateMessageRequest.builder()
                .receive_id_type("open_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(open_id)
                    .msg_type("text")
                    .content(json.dumps({"text": text}, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            return self._sdk().im.v1.message.create(request)

        try:
            response = await asyncio.to_thread(_call)
            if not response.success():
                return FeishuSendResult(
                    ok=False,
                    mode="live",
                    error=f"code={response.code} msg={response.msg}",
                )
            message_id = getattr(response.data, "message_id", "") or ""
            return FeishuSendResult(ok=True, mode="live", message_id=message_id)
        except Exception as exc:  # noqa: BLE001 - 外部系统异常统一收口
            logger.warning("feishu.send_text.failed", extra=log_extra(error=str(exc)))
            return FeishuSendResult(ok=False, mode="live", error=str(exc))

    # ------------------------------------------------------------ 出站：文件
    async def send_file(
        self, open_id: str, file_path: str | Path, *, file_name: str | None = None
    ) -> FeishuSendResult:
        """把简历压缩包作为文件消息发给主管。"""
        path = Path(file_path)
        if not path.exists():
            return FeishuSendResult(ok=False, mode=self.mode, error=f"文件不存在: {path}")
        name = file_name or path.name

        if not self.settings.feishu_live:
            message_id = f"mock-feishu-file-{uuid.uuid4().hex[:16]}"
            logger.info(
                "feishu.mock.send_file",
                extra=log_extra(open_id=open_id, file=name, bytes=path.stat().st_size),
            )
            return FeishuSendResult(ok=True, mode="mock", message_id=message_id)

        def _call() -> Any:
            from lark_oapi.api.im.v1 import (
                CreateFileRequest,
                CreateFileRequestBody,
                CreateMessageRequest,
                CreateMessageRequestBody,
            )

            with path.open("rb") as handle:
                upload_request = (
                    CreateFileRequest.builder()
                    .request_body(
                        CreateFileRequestBody.builder()
                        .file_type("stream")
                        .file_name(name)
                        .file(handle)
                        .build()
                    )
                    .build()
                )
                upload_response = self._sdk().im.v1.file.create(upload_request)
            if not upload_response.success():
                raise ExternalServiceError(
                    f"飞书文件上传失败 code={upload_response.code} msg={upload_response.msg}"
                )
            file_key = upload_response.data.file_key
            request = (
                CreateMessageRequest.builder()
                .receive_id_type("open_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(open_id)
                    .msg_type("file")
                    .content(json.dumps({"file_key": file_key}))
                    .build()
                )
                .build()
            )
            return self._sdk().im.v1.message.create(request)

        try:
            response = await asyncio.to_thread(_call)
            if not response.success():
                return FeishuSendResult(
                    ok=False, mode="live", error=f"code={response.code} msg={response.msg}"
                )
            return FeishuSendResult(
                ok=True, mode="live", message_id=getattr(response.data, "message_id", "") or ""
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("feishu.send_file.failed", extra=log_extra(error=str(exc)))
            return FeishuSendResult(ok=False, mode="live", error=str(exc))

    # ---------------------------------------------------------- 入站：解密
    def decode_webhook(self, body: bytes, headers: Mapping[str, str] | None = None) -> dict[str, Any]:
        """解析 webhook 原始请求体：处理 encrypt 加密体并校验 verification token。"""
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigurationError("飞书回调请求体不是合法 JSON") from exc

        if "encrypt" in payload:
            payload = self._decrypt(payload["encrypt"])

        token = payload.get("token") or (payload.get("header") or {}).get("token") or ""
        expected = self.settings.feishu_verification_token
        if expected and token and token != expected:
            raise ConfigurationError("飞书回调 verification token 不匹配")
        return payload

    def _decrypt(self, encrypted: str) -> dict[str, Any]:
        """使用官方 SDK 的 AES 解密工具（不自行实现加解密算法）。"""
        try:
            from lark_oapi.core.utils import AESCipher  # type: ignore

            plain = AESCipher(self.settings.feishu_encrypt_key).decrypt_string(encrypted)
            return json.loads(plain)
        except Exception as exc:  # noqa: BLE001
            raise ConfigurationError(
                "无法解密飞书回调（需要 FEISHU_ENCRYPT_KEY，且当前 lark_oapi 版本需提供 AESCipher）"
            ) from exc

    def event_type(self, payload: dict[str, Any]) -> str:
        header = payload.get("header") or {}
        return str(header.get("event_type") or payload.get("type") or "")

    # ---------------------------------------------------------- 入站：归一
    def normalize_event(self, payload: dict[str, Any]) -> InboundMessage | None:
        """把飞书事件转成内部 ``InboundMessage``；不认识的事件返回 None。"""
        event_type = self.event_type(payload)
        event = payload.get("event") or {}
        header = payload.get("header") or {}
        received_at = _event_time(header.get("create_time") or event.get("create_time"))

        if event_type == "im.message.receive_v1":
            return self._normalize_message_event(event, received_at, payload)
        if event_type == "card.action.trigger":
            return self._normalize_card_action(event, received_at, payload)
        return None

    def _normalize_message_event(
        self, event: dict[str, Any], received_at: datetime, payload: dict[str, Any]
    ) -> InboundMessage | None:
        message = event.get("message") or {}
        sender = event.get("sender") or {}
        sender_id = ((sender.get("sender_id") or {}).get("open_id")) or ""
        message_type = message.get("message_type") or "text"
        mentions = message.get("mentions") or []
        text = content_to_text(message_type, message.get("content"), mentions)
        if not sender_id:
            return None

        attachments: list[Attachment] = []
        if message_type in {"file", "image", "media"}:
            content = _loads(message.get("content"))
            attachments.append(
                Attachment(
                    filename=content.get("file_name", ""),
                    content_type=message_type,
                    size=int(content.get("file_size") or 0),
                )
            )

        return InboundMessage(
            channel="feishu",
            sender_id=sender_id,
            sender_name=_sender_name(sender),
            text=text,
            thread_id=message.get("chat_id", ""),
            message_id=message.get("message_id", ""),
            kind="text",
            received_at=received_at,
            attachments=attachments,
            raw_event=payload,
            reply_to_message_id=message.get("parent_id") or message.get("root_id") or "",
        )

    def _normalize_card_action(
        self, event: dict[str, Any], received_at: datetime, payload: dict[str, Any]
    ) -> InboundMessage | None:
        operator = event.get("operator") or {}
        open_id = operator.get("open_id") or ((operator.get("user_id") or {}).get("open_id")) or ""
        if not open_id:
            return None
        action = event.get("action") or {}
        value = action.get("value") or {}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = {"text": value}
        if not isinstance(value, dict):
            value = {"text": str(value)}

        text = (
            value.get("text")
            or value.get("comment")
            or value.get("message")
            or json.dumps(value, ensure_ascii=False)
        )
        return InboundMessage(
            channel="feishu",
            sender_id=open_id,
            sender_name=operator.get("name", ""),
            text=str(text),
            thread_id=(event.get("context") or {}).get("open_chat_id", ""),
            message_id=(event.get("context") or {}).get("open_message_id", ""),
            kind="card_action",
            received_at=received_at,
            raw_event=payload,
            action=dict(value),
            reply_to_message_id=(event.get("context") or {}).get("open_message_id", ""),
        )

    async def download_resource(self, message_id: str, file_key: str, *, resource_type: str = "file") -> bytes:
        """下载主管在飞书里附带的文件（如额外简历）。mock 模式返回空字节。"""
        if not self.settings.feishu_live:
            logger.info("feishu.mock.download", extra=log_extra(message_id=message_id, file_key=file_key))
            return b""

        def _call() -> bytes:
            from lark_oapi.api.im.v1 import GetMessageResourceRequest

            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(file_key)
                .type(resource_type)
                .build()
            )
            response = self._sdk().im.v1.message_resource.get(request)
            if not response.success():
                raise ExternalServiceError(
                    f"飞书资源下载失败 code={response.code} msg={response.msg}"
                )
            return response.file.read()

        return await asyncio.to_thread(_call)


# --------------------------------------------------------------------- 工具


def _loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def content_to_text(message_type: str, content: Any, mentions: list[dict[str, Any]] | None = None) -> str:
    """从飞书消息 content 中提取可读文本；支持 text / post 两种常见类型。"""
    data = _loads(content)
    if message_type == "text":
        text = str(data.get("text", ""))
    elif message_type == "post":
        text = _flatten_post(data)
    else:
        text = str(data.get("text", "")) if data else ""

    for mention in mentions or []:
        key = mention.get("key")
        name = mention.get("name")
        if key and name:
            text = text.replace(key, name)
    return text.strip()


def _flatten_post(data: dict[str, Any]) -> str:
    """飞书富文本 post：{"title": "...", "content": [[{"tag":"text","text":"..."}]]}"""
    lines: list[str] = []
    if data.get("title"):
        lines.append(str(data["title"]))
    content = data.get("content")
    if isinstance(content, dict):  # 多语言包裹
        content = next(iter(content.values()), [])
    for paragraph in content or []:
        if not isinstance(paragraph, list):
            continue
        line = "".join(str(node.get("text", "")) for node in paragraph if isinstance(node, dict))
        if line:
            lines.append(line)
    return "\n".join(lines)


def _sender_name(sender: dict[str, Any]) -> str:
    sender_id = sender.get("sender_id") or {}
    return str(sender.get("sender_name") or sender_id.get("name") or "")


def _event_time(raw: Any) -> datetime:
    """飞书 create_time 是毫秒时间戳字符串；缺失则用当前时间。"""
    if raw:
        try:
            return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError):
            pass
    return datetime.now(timezone.utc).replace(tzinfo=None)
