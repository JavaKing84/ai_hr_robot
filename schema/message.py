"""入站消息归一化模型（借鉴 AstrBot / LangBot 的“通道归一”架构思想，不复制代码）。

飞书事件、邮件、卡片回调都先转成 ``InboundMessage``，再送进统一的解析管道，
业务层因此不感知具体渠道的字段差异。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ChannelName = Literal["feishu", "email"]
MessageKind = Literal["text", "card_action", "email", "file"]


class Attachment(BaseModel):
    filename: str = ""
    path: str = ""
    size: int = 0
    content_type: str = ""


class InboundMessage(BaseModel):
    """统一入站消息对象。"""

    channel: ChannelName
    sender_id: str = Field(description="飞书 open_id，或邮件发件人地址")
    sender_name: str = ""
    text: str = ""
    thread_id: str = Field(default="", description="飞书 chat_id / 邮件线程根 Message-ID")
    message_id: str = ""
    kind: MessageKind = "text"
    received_at: datetime
    attachments: list[Attachment] = Field(default_factory=list)
    raw_event: dict[str, Any] = Field(default_factory=dict)

    # 飞书：被回复的消息 / 卡片动作；邮件：邮件头部
    reply_to_message_id: str = ""
    action: dict[str, Any] = Field(default_factory=dict)

    subject: str = ""
    in_reply_to: str = ""
    references: list[str] = Field(default_factory=list)
    to_addresses: list[str] = Field(default_factory=list)
    cc_addresses: list[str] = Field(default_factory=list)

    @property
    def is_email(self) -> bool:
        return self.channel == "email"
