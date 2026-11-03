"""外部系统客户端（飞书 / 邮件 / LLM / 简历解析）。"""

from client.feishu import FeishuClient, FeishuSendResult
from client.llm import LLMClient
from client.mailbox import InboundEmail, MailAttachment, MailboxClient, SendResult, parse_email_bytes
from client.resume_parser import ParsedResume, ResumeParser

__all__ = [
    "FeishuClient",
    "FeishuSendResult",
    "InboundEmail",
    "LLMClient",
    "MailAttachment",
    "MailboxClient",
    "ParsedResume",
    "ResumeParser",
    "SendResult",
    "parse_email_bytes",
]
