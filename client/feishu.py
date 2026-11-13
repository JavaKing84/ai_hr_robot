"""飞书客户端：官方 SDK（lark_oapi）负责 token / 加解密 / 接口调用，本文件只做封装与归一化。

两种模式：
- ``FEISHU_MODE=live``：真实调用开放平台接口（发送消息、上传文件、下载资源）；
- ``FEISHU_MODE=mock``：不发真实请求，返回合成 message_id，由出站留痕记录在 messages 表。

入站事件统一转成 ``schema.message.InboundMessage``（借鉴通道归一架构，不复制第三方代码）。
"""

from __future__ import annotations

import asyncio
import json
import secrets
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

    # ------------------------------------------------------------ 出站：统一入口
    # 群机器人用 webhook 发消息、不需要 app 凭证；上传文件仍走 app 身份（im/v1/files）。
    async def push_text(self, open_id: str, text: str) -> FeishuSendResult:
        """按 ``FEISHU_PUSH_TARGET`` 决定发群还是发个人私聊。"""
        if self.settings.feishu_group_push:
            return await self.send_group_text(text)
        return await self.send_text(open_id, text)

    async def push_file(
        self, open_id: str, file_path: str | Path, *, file_name: str | None = None
    ) -> FeishuSendResult:
        if self.settings.feishu_group_push:
            return await self.send_group_file(file_path, file_name=file_name)
        return await self.send_file(open_id, file_path, file_name=file_name)

    # --------------------------------------------------------- 出站：群机器人
    async def send_group_text(self, text: str, *, at_all: bool = False) -> FeishuSendResult:
        """把文本发到群里。

        优先用**应用身份**（``im/v1/messages`` 发到 ``FEISHU_CHAT_ID``），与压缩包同一条
        通道——只要机器人在群里、应用有 ``im:message:send_as_bot`` 就能发，不再依赖
        群里那个「自定义机器人 webhook」（2026-11-13 该 webhook 被停用导致文字
        全部 19007 Bot Not Enabled，而文件却能发）。没配 ``FEISHU_CHAT_ID`` 时才回退 webhook。

        群机器人只认 ``msg_type`` 与 ``content``，@人 用 ``<at user_id="ou_xxx"></at>``
        片段拼在文本里；配置了 ``FEISHU_MENTION_OPEN_IDS`` 就 @ 指定人，否则不发 @。
        """
        mentions = self.settings.feishu_mention_list
        content_text = text
        if at_all:
            content_text = f'<at user_id="all">所有人</at> {text}'
        elif mentions:
            prefix = " ".join(f'<at user_id="{oid}"></at>' for oid in mentions)
            content_text = f"{prefix} {text}"

        if not self.settings.feishu_live:
            message_id = f"mock-feishu-group-{uuid.uuid4().hex[:16]}"
            logger.info(
                "feishu.mock.send_group_text",
                extra=log_extra(chars=len(content_text), message_id=message_id),
            )
            return FeishuSendResult(ok=True, mode="mock", message_id=message_id)

        chat_id = (self.settings.feishu_chat_id or "").strip()
        if chat_id:
            return await self._send_chat_text(chat_id, content_text)

        if not self.settings.feishu_webhook_url:
            return FeishuSendResult(
                ok=False, mode=self.mode, error="缺少 FEISHU_CHAT_ID 与 FEISHU_WEBHOOK_URL"
            )
        return await self._post_webhook(
            {"msg_type": "text", "content": {"text": content_text}}, kind="text"
        )

    async def _send_chat_text(self, chat_id: str, text: str) -> FeishuSendResult:
        """应用身份给群发文本（不阻塞事件循环：SDK 同步调用放到线程池）。"""

        def _call() -> Any:
            from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

            request = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("text")
                    .content(json.dumps({"text": text}))
                    .build()
                )
                .build()
            )
            return self._sdk().im.v1.message.create(request)

        try:
            response = await asyncio.to_thread(_call)
            if not response.success():
                error = f"code={response.code} msg={response.msg}"
                logger.warning("feishu.send_chat_text.rejected", extra=log_extra(error=error))
                return FeishuSendResult(ok=False, mode="live", error=error)
            return FeishuSendResult(
                ok=True, mode="live", message_id=getattr(response.data, "message_id", "") or ""
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("feishu.send_chat_text.failed", extra=log_extra(error=str(exc)))
            return FeishuSendResult(ok=False, mode="live", error=str(exc))

    async def send_group_file(
        self, file_path: str | Path, *, file_name: str | None = None
    ) -> FeishuSendResult:
        """把简历压缩包发到群里。

        自定义机器人 webhook **不支持** file 消息，必须用应用身份先
        ``im/v1/files`` 上传拿 file_key，再 ``im/v1/messages`` 发到 chat_id。
        没配 ``FEISHU_CHAT_ID`` 时降级为「文本提示 + 本地路径」，绝不静默丢文件。
        """
        path = Path(file_path)
        if not path.exists():
            return FeishuSendResult(ok=False, mode=self.mode, error=f"文件不存在: {path}")
        name = file_name or path.name

        if not self.settings.feishu_live:
            message_id = f"mock-feishu-group-file-{uuid.uuid4().hex[:16]}"
            logger.info(
                "feishu.mock.send_group_file",
                extra=log_extra(file=name, bytes=path.stat().st_size),
            )
            return FeishuSendResult(ok=True, mode="mock", message_id=message_id)

        chat_id = (self.settings.feishu_chat_id or "").strip()
        if not chat_id:
            await self.send_group_text(
                f"（简历压缩包已生成，但未配置 FEISHU_CHAT_ID，无法上传到群）{name}"
            )
            return FeishuSendResult(
                ok=False,
                mode="live",
                error="自定义机器人 webhook 不支持文件消息，且未配置 FEISHU_CHAT_ID",
            )

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
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
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
                error = f"code={response.code} msg={response.msg}"
                logger.warning("feishu.send_group_file.rejected", extra=log_extra(error=error))
                await self._notify_file_failed(name, error)
                return FeishuSendResult(ok=False, mode="live", error=error)
            return FeishuSendResult(
                ok=True, mode="live", message_id=getattr(response.data, "message_id", "") or ""
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("feishu.send_group_file.failed", extra=log_extra(error=str(exc)))
            await self._notify_file_failed(name, str(exc))
            return FeishuSendResult(ok=False, mode="live", error=str(exc))

    async def _notify_file_failed(self, file_name: str, error: str) -> None:
        """文件没能进群时，在群里说一句人话，别让 HR 以为简历包发了。

        最常见的失败是应用没开 ``im:resource:upload``（飞书返回 99991672）。
        这时群里至少要有「压缩包已生成在哪、要开哪个权限」这两条信息。
        """
        try:
            await self.send_group_text(
                f"⚠️ 简历压缩包「{file_name}」没能上传到群：{error[:200]}\n"
                "（压缩包已生成在服务器 var/packages 目录；若是权限问题，请为应用开通 "
                "im:resource:upload 后重试）"
            )
        except Exception as exc:  # noqa: BLE001 - 通知失败不能再抛，避免覆盖原始错误
            logger.warning("feishu.file_failed_notice.failed", extra=log_extra(error=str(exc)))

    async def _post_webhook(self, payload: dict[str, Any], *, kind: str) -> FeishuSendResult:
        """POST 到群机器人 webhook 并归一化返回。

        飞书的成功响应有两种形状：``{"code": 0, "msg": "success"}``
        （新接口）与 ``{"StatusCode": 0, "StatusMessage": "success"}``（老接口），
        两个都要认，否则会把成功判成失败并触发补发，导致群里收到重复消息。
        """
        import httpx

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(self.settings.feishu_webhook_url, json=payload)
            data = response.json() if response.content else {}
        except Exception as exc:  # noqa: BLE001 - 网络异常统一收口
            logger.warning("feishu.webhook.failed", extra=log_extra(kind=kind, error=str(exc)))
            return FeishuSendResult(ok=False, mode="live", error=str(exc))

        code = data.get("code", data.get("StatusCode"))
        if response.status_code // 100 == 2 and code in (0, None):
            message_id = f"group-{uuid.uuid4().hex[:12]}"
            logger.info("feishu.webhook.sent", extra=log_extra(kind=kind, message_id=message_id))
            return FeishuSendResult(ok=True, mode="live", message_id=message_id)
        error = f"code={code} msg={data.get('msg') or data.get('StatusMessage')}"
        logger.warning("feishu.webhook.rejected", extra=log_extra(kind=kind, error=error))
        return FeishuSendResult(ok=False, mode="live", error=error)

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
        """解析 webhook 原始请求体：先验来源，再解密。

        2026-11-14 加固（原来「token 为空就整体跳过校验」，等于生产环境不设防）：
        1. 配了 ``FEISHU_ENCRYPT_KEY`` → 校验 ``X-Lark-Signature``
           （``sha256(timestamp + nonce + encrypt_key + body)``），签名不符直接拒；
        2. 配了 ``FEISHU_VERIFICATION_TOKEN`` → payload 里的 token **必须存在且相等**
           （原来只判「两边都有值时才比」，缺 token 就能过）；
        3. 两者都没配 → dev 环境放行并告警，非 dev 直接拒收。
        """
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigurationError("飞书回调请求体不是合法 JSON") from exc

        self._verify_signature(body, headers or {})

        if "encrypt" in payload:
            payload = self._decrypt(payload["encrypt"])

        self._verify_token(payload)
        return payload

    def _verify_signature(self, body: bytes, headers: Mapping[str, str]) -> None:
        """校验飞书请求签名（只有配了 encrypt_key 才有意义）。"""
        encrypt_key = (self.settings.feishu_encrypt_key or "").strip()
        if not encrypt_key or self.settings.feishu_verify_mode == "off":
            return

        lowered = {str(k).lower(): v for k, v in headers.items()}
        signature = (lowered.get("x-lark-signature") or "").strip()
        timestamp = (lowered.get("x-lark-request-timestamp") or "").strip()
        nonce = (lowered.get("x-lark-request-nonce") or "").strip()
        if not signature:
            raise ConfigurationError("飞书回调缺少 X-Lark-Signature（已配置 ENCRYPT_KEY，必须验签）")

        import hashlib

        expected = hashlib.sha256(
            (timestamp + nonce + encrypt_key).encode("utf-8") + body
        ).hexdigest()
        if not secrets.compare_digest(expected, signature):
            raise ConfigurationError("飞书回调签名校验失败")

    def _verify_token(self, payload: dict[str, Any]) -> None:
        """校验 verification token。"""
        mode = self.settings.feishu_verify_mode
        if mode == "off":
            return

        expected = (self.settings.feishu_verification_token or "").strip()
        token = str(payload.get("token") or (payload.get("header") or {}).get("token") or "")

        if expected:
            if not token or not secrets.compare_digest(token, expected):
                raise ConfigurationError("飞书回调 verification token 不匹配（或缺失）")
            return

        # 一个都没配：非 dev 环境拒收，避免「上线即裸奔」
        if mode == "strict" or self.settings.environment.lower() not in {"dev", "development", "local", "test"}:
            raise ConfigurationError(
                "飞书回调未配置 FEISHU_VERIFICATION_TOKEN / FEISHU_ENCRYPT_KEY，"
                "非 dev 环境拒绝接收（可在 .env 补 token，或显式设 FEISHU_VERIFY_MODE=off）"
            )
        logger.warning("feishu.webhook_unverified", extra=log_extra(reason="dev 环境且未配置 token"))

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

    # ------------------------------------------------- 入站：主动拉群消息
    # 为什么要有这条通道：Webhook 要求飞书服务器能回连到本机。开发机、内网机器、
    # 没做穿透的部署全都收不到 ``im.message.receive_v1``——机器人发了消息，主管回了
    # 话，系统却完全听不见，界面上看着就是「消息发出去了，但没人应」。
    # 主动拉取不依赖入站网络：只要应用有 ``im:message:readonly``（或 ``im:message``）
    # 且机器人在群里，就能把群里的新消息读回来，效果与 webhook 一致。
    async def list_chat_messages(
        self,
        chat_id: str,
        *,
        start_time: int | None = None,
        page_size: int = 20,
    ) -> list[dict[str, Any]]:
        """拉取群消息，按时间正序返回归一后的 dict（机器人自己发的不在里面）。"""
        chat_id = (chat_id or "").strip()
        if not chat_id or not self.settings.feishu_live:
            return []

        def _call() -> Any:
            from lark_oapi.api.im.v1 import ListMessageRequest

            builder = (
                ListMessageRequest.builder()
                .container_id_type("chat")
                .container_id(chat_id)
                .sort_type("ByCreateTimeDesc")
                .page_size(max(1, min(page_size, 50)))
            )
            if start_time:
                builder = builder.start_time(str(int(start_time)))
            return self._sdk().im.v1.message.list(builder.build())

        try:
            response = await asyncio.to_thread(_call)
        except Exception as exc:  # noqa: BLE001 - 拉取失败只影响本轮，别把定时任务打挂
            logger.warning("feishu.list_messages.failed", extra=log_extra(error=str(exc)))
            return []

        if not response.success():
            # 常见原因：应用没开 im:message:readonly，或机器人不在该群
            logger.warning(
                "feishu.list_messages.rejected",
                extra=log_extra(error=f"code={response.code} msg={response.msg}"),
            )
            return []

        items = getattr(response.data, "items", None) or []
        collected: list[dict[str, Any]] = []
        for item in items:
            normalized = self._normalize_chat_message(item)
            if normalized is not None:
                collected.append(normalized)
        collected.sort(key=lambda it: it["create_time_ms"])
        return collected

    def _normalize_chat_message(self, item: Any) -> dict[str, Any] | None:
        """把 ``im.v1.messages.list`` 的单条结果转成与 webhook 同形状的 dict。"""
        sender = getattr(item, "sender", None)
        sender_type = (getattr(sender, "sender_type", "") or "").lower()
        # sender_type=app 就是机器人自己（也包括本应用发出去的分发包）：读回来再处理
        # 会自己回自己，必须挡掉。
        if sender_type == "app":
            return None

        message_id = getattr(item, "message_id", "") or ""
        if not message_id:
            return None

        msg_type = getattr(item, "msg_type", "") or "text"
        body = getattr(item, "body", None)
        content = getattr(body, "content", "") if body is not None else ""
        raw_mentions = getattr(item, "mentions", None) or []
        mentions = [
            {"key": getattr(m, "key", "") or "", "name": getattr(m, "name", "") or ""}
            for m in raw_mentions
        ]
        text = content_to_text(msg_type, content, mentions)
        # 群里 @机器人 后文本形如「@HR助手 确认 1,3」，把开头的 @昵称 剥掉，
        # 否则下游按「第一句是不是确认」判断会全部落到兜底分支。
        text = _strip_leading_mentions(text, mentions)

        try:
            create_ms = int(getattr(item, "create_time", "0") or 0)
        except (TypeError, ValueError):
            create_ms = 0

        return {
            "message_id": message_id,
            "sender_id": getattr(sender, "id", "") or "",
            "sender_type": sender_type,
            "msg_type": msg_type,
            "content": content,
            "text": text,
            "mentions": mentions,
            "create_time_ms": create_ms,
            "received_at": _event_time(create_ms),
            "chat_id": getattr(item, "chat_id", "") or "",
        }

    def to_inbound_message(self, item: dict[str, Any]) -> InboundMessage:
        """把拉取结果转成内部 ``InboundMessage``，与 webhook 分支共用同一条解析管道。"""
        return InboundMessage(
            channel="feishu",
            sender_id=item.get("sender_id", ""),
            text=item.get("text", ""),
            thread_id=item.get("chat_id", "") or self.settings.feishu_chat_id,
            message_id=item.get("message_id", ""),
            kind="text",
            received_at=item.get("received_at") or datetime.now(timezone.utc).replace(tzinfo=None),
            raw_event={"source": "poll", "msg_type": item.get("msg_type", "")},
        )


def _strip_leading_mentions(text: str, mentions: list[dict[str, Any]]) -> str:
    """去掉开头的 ``@某人`` 前缀（只按已知 mention 名字剥，避免误删正文里的 @）。"""
    out = text.lstrip()
    names = [m.get("name") for m in mentions if m.get("name")]
    changed = True
    while changed:
        changed = False
        for name in names:
            prefix = f"@{name}"
            if out.startswith(prefix):
                out = out[len(prefix) :].lstrip(" :：,，、")
                changed = True
                break
    return out


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
