"""邮件客户端：SMTP 发送（aiosmtplib，非阻塞）+ IMAP 轮询收信。

两种模式：
- ``MAIL_MODE=live``：真实 SMTP/IMAP；
- ``MAIL_MODE=mock``：发信写 ``var/outbox/mail/*.eml``；收信读 ``var/mock_inbox/*.eml``
  （把一封 .eml 丢进目录即可模拟“候选人回信”），便于离线联调与演示。

线程追踪相关头部（Message-ID / In-Reply-To / References）全部由本模块生成与解析。
"""

from __future__ import annotations

import asyncio
import email
import hashlib
import imaplib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable

import aiosmtplib

from config.settings import Settings
from core.exceptions import MailboxUnavailable
from core.logging import get_logger, log_extra
from core.threading import normalize_message_id, parse_reference_header

logger = get_logger(__name__)

_ALLOWED_ATTACHMENT_SUFFIXES = (".pdf", ".doc", ".docx", ".txt", ".md", ".zip", ".png", ".jpg", ".jpeg")


@dataclass
class MailAttachment:
    filename: str
    content_type: str = "application/octet-stream"
    data: bytes = b""
    path: str = ""


@dataclass
class InboundEmail:
    message_id: str
    subject: str
    from_address: str
    from_name: str = ""
    to_addresses: list[str] = field(default_factory=list)
    cc_addresses: list[str] = field(default_factory=list)
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)
    body_text: str = ""
    body_html: str = ""
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    attachments: list[MailAttachment] = field(default_factory=list)
    raw_path: str = ""


@dataclass
class SendResult:
    ok: bool
    mode: str
    message_id: str = ""
    error: str = ""
    saved_path: str = ""


class MailboxClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------ 基础
    @property
    def mode(self) -> str:
        return self.settings.mail_mode

    @property
    def domain(self) -> str:
        address = self.settings.hr_mailbox or "hr@example.com"
        return address.split("@")[-1] if "@" in address else "example.com"

    def new_message_id(self) -> str:
        return f"<{uuid.uuid4().hex}.{uuid.uuid4().hex[:8]}@{self.domain}>"

    def reply_address(self, token: str) -> str:
        domain = self.settings.hr_mailbox.split("@")[-1] if "@" in self.settings.hr_mailbox else self.domain
        return f"reply+{token}@{domain}"

    # ------------------------------------------------------------------ 发信
    async def send(
        self,
        *,
        to: str | Iterable[str],
        subject: str,
        body_text: str,
        body_html: str = "",
        message_id: str | None = None,
        in_reply_to: str = "",
        references: Iterable[str] = (),
        reply_to: str = "",
        attachments: Iterable[MailAttachment] = (),
        cc: Iterable[str] = (),
    ) -> SendResult:
        recipients = [to] if isinstance(to, str) else list(to)
        recipients = [addr for addr in recipients if addr]
        if not recipients:
            return SendResult(ok=False, mode=self.mode, error="收件人为空")

        msg_id = message_id or self.new_message_id()
        message = self._build_message(
            to=recipients,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            message_id=msg_id,
            in_reply_to=in_reply_to,
            references=list(references),
            reply_to=reply_to,
            attachments=list(attachments),
            cc=list(cc),
        )

        if self.mode == "off":
            return SendResult(ok=False, mode="off", message_id=msg_id, error="MAIL_MODE=off，邮件未发送")
        if not self.settings.mail_live:
            return await self._mock_send(message, msg_id)
        return await self._live_send(message, msg_id)

    def _build_message(
        self,
        *,
        to: list[str],
        subject: str,
        body_text: str,
        body_html: str = "",
        message_id: str = "",
        in_reply_to: str = "",
        references: list[str] | None = None,
        reply_to: str = "",
        attachments: list[MailAttachment] | None = None,
        cc: list[str] | None = None,
    ) -> EmailMessage:
        message = EmailMessage()
        message["From"] = f"{self.settings.hr_sender_name} <{self.settings.hr_mailbox}>"
        message["To"] = ", ".join(to)
        if cc:
            message["Cc"] = ", ".join(cc)
        message["Subject"] = subject
        message["Date"] = format_datetime(datetime.now(timezone.utc))
        if message_id:
            message["Message-ID"] = message_id
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
        if references:
            message["References"] = " ".join(references)
        if reply_to:
            message["Reply-To"] = reply_to

        message.set_content(body_text or "")
        if body_html:
            message.add_alternative(body_html, subtype="html")

        for attachment in attachments or []:
            if not attachment.data:
                continue
            maintype, _, subtype = (attachment.content_type or "application/octet-stream").partition("/")
            message.add_attachment(
                attachment.data,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=attachment.filename or "attachment.bin",
            )
        return message

    async def _mock_send(self, message: EmailMessage, message_id: str) -> SendResult:
        self.settings.mail_outbox_dir.mkdir(parents=True, exist_ok=True)
        slug = hashlib.sha1(message_id.encode()).hexdigest()[:12]
        filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{slug}.eml"
        path = self.settings.mail_outbox_dir / filename
        path.write_bytes(message.as_bytes())
        logger.info(
            "mail.mock.sent",
            extra=log_extra(to=message["To"], subject=message["Subject"], path=str(path)),
        )
        return SendResult(ok=True, mode="mock", message_id=message_id, saved_path=str(path))

    async def _live_send(self, message: EmailMessage, message_id: str) -> SendResult:
        if not self.settings.smtp_host:
            return SendResult(ok=False, mode="live", message_id=message_id, error="缺少 SMTP_HOST")
        try:
            await aiosmtplib.send(
                message,
                hostname=self.settings.smtp_host,
                port=self.settings.smtp_port,
                username=self.settings.smtp_user or None,
                password=self.settings.smtp_password or None,
                use_tls=self.settings.smtp_use_ssl,
                start_tls=self.settings.smtp_start_tls and not self.settings.smtp_use_ssl,
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001 - 统一收口成可重试错误
            logger.warning("mail.live.send_failed", extra=log_extra(error=str(exc)))
            return SendResult(ok=False, mode="live", message_id=message_id, error=str(exc))
        logger.info("mail.live.sent", extra=log_extra(to=message["To"], subject=message["Subject"]))
        return SendResult(ok=True, mode="live", message_id=message_id)

    # ------------------------------------------------------------------ 收信
    async def poll_inbox(self) -> list[InboundEmail]:
        """拉取新邮件；mock 模式读本地目录，live 模式用 IMAP UID 增量拉取。"""
        if self.mode == "off":
            return []
        if not self.settings.mail_live:
            return self._mock_poll()
        return await asyncio.to_thread(self._live_poll)

    def _mock_poll(self) -> list[InboundEmail]:
        inbox = self.settings.mock_inbox_dir
        inbox.mkdir(parents=True, exist_ok=True)
        state_path = inbox / ".state.json"
        state: dict[str, Any] = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                state = {}
        processed: dict[str, float] = state.get("processed", {})

        results: list[InboundEmail] = []
        for path in sorted(inbox.glob("*.eml")):
            mtime = path.stat().st_mtime
            if processed.get(path.name) == mtime:
                continue
            results.append(parse_email_bytes(path.read_bytes(), raw_path=str(path)))
            processed[path.name] = mtime

        state_path.write_text(json.dumps({"processed": processed}, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("mail.mock.polled", extra=log_extra(count=len(results)))
        return results

    #: RFC 2971 客户端身份声明：网易系邮箱 SELECT 前强制要求，否则报 Unsafe Login。
    _IMAP_ID_ARGS = '("name" "ai-hr-robot" "version" "1.0" "vendor" "ai-hr")'

    @classmethod
    def _send_imap_id(cls, client: imaplib.IMAP4) -> None:
        """登录后、SELECT 前声明客户端身份（网易邮箱必需）。"""
        imaplib.Commands.setdefault("ID", ("AUTH", "SELECTED"))
        try:
            typ, dat = client._simple_command("ID", cls._IMAP_ID_ARGS)
            client._untagged_response(typ, dat, "ID")
            logger.debug("mail.imap.id_sent", extra=log_extra(status=typ))
        except Exception as exc:  # noqa: BLE001 - 握手失败不应中断收信
            logger.debug("mail.imap.id_failed", extra=log_extra(error=str(exc)))

    def _live_poll(self) -> list[InboundEmail]:
        if not self.settings.imap_host:
            raise MailboxUnavailable("缺少 IMAP_HOST，无法收信")
        state_path = self.settings.var_dir / "imap_state.json"
        state: dict[str, Any] = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                state = {}
        last_uid = int(state.get("last_uid", 0))

        client_cls = imaplib.IMAP4_SSL if self.settings.imap_use_ssl else imaplib.IMAP4
        results: list[InboundEmail] = []
        try:
            with client_cls(self.settings.imap_host, self.settings.imap_port) as client:
                client.login(self.settings.imap_user, self.settings.imap_password)
                self._send_imap_id(client)
                status, _ = client.select(self.settings.imap_inbox or "INBOX")
                if status != "OK":
                    raise MailboxUnavailable(
                        f"IMAP 选择信箱失败（{status}）：请确认已开启 IMAP、imap_inbox 名称正确"
                    )
                status, data = client.uid("search", None, f"UID {last_uid + 1}:*")
                if status != "OK":
                    raise MailboxUnavailable(f"IMAP search 失败: {status}")
                uids = [int(u) for u in (data[0] or b"").split() if u.isdigit() and int(u) > last_uid]
                for uid in sorted(uids)[: self.settings.imap_poll_limit]:
                    status, payload = client.uid("fetch", str(uid), "(RFC822)")
                    if status != "OK" or not payload:
                        continue
                    raw = next((part[1] for part in payload if isinstance(part, tuple)), b"")
                    if raw:
                        results.append(parse_email_bytes(raw))
                    last_uid = max(last_uid, uid)
        except imaplib.IMAP4.error as exc:
            raise MailboxUnavailable(f"IMAP 连接/认证失败: {exc}") from exc
        except OSError as exc:
            raise MailboxUnavailable(f"IMAP 网络错误: {exc}") from exc

        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"last_uid": last_uid}, ensure_ascii=False), encoding="utf-8")
        logger.info("mail.live.polled", extra=log_extra(count=len(results), last_uid=last_uid))
        return results


# --------------------------------------------------------------------- 解析


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 - 头部编码异常不应中断整封信
        return value


def _get_body_text(message: email.message.EmailMessage) -> tuple[str, str]:
    text_body = ""
    html_body = ""
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            if content_type == "text/plain" and not text_body:
                text_body = _decode_part(part)
            elif content_type == "text/html" and not html_body:
                html_body = _decode_part(part)
    else:
        if message.get_content_type() == "text/html":
            html_body = _decode_part(message)
        else:
            text_body = _decode_part(message)
    if not text_body and html_body:
        text_body = _html_to_text(html_body)
    return text_body.strip(), html_body.strip()


def _decode_part(part: email.message.Message) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError):
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")


def _html_to_text(html: str) -> str:
    import re

    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _collect_attachments(message: email.message.EmailMessage) -> list[MailAttachment]:
    attachments: list[MailAttachment] = []
    for part in message.walk():
        filename = part.get_filename()
        if not filename:
            continue
        filename = _decode_header(filename)
        if not filename.lower().endswith(_ALLOWED_ATTACHMENT_SUFFIXES):
            continue
        data = part.get_payload(decode=True) or b""
        if not data:
            continue
        attachments.append(
            MailAttachment(
                filename=filename,
                content_type=part.get_content_type() or "application/octet-stream",
                data=data,
            )
        )
    return attachments


def parse_email_bytes(raw: bytes, *, raw_path: str = "") -> InboundEmail:
    """把原始 RFC822 字节解析成 ``InboundEmail``（供 IMAP / 本地 .eml / 测试共用）。"""
    message = email.message_from_bytes(raw, policy=policy.default)
    body_text, body_html = _get_body_text(message)

    received_at = datetime.now(timezone.utc).replace(tzinfo=None)
    date_header = message.get("Date")
    if date_header:
        try:
            parsed = parsedate_to_datetime(str(date_header))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            received_at = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError):
            pass

    from_header = _decode_header(message.get("From"))
    from_address, from_name = _split_address(from_header)

    return InboundEmail(
        message_id=normalize_message_id(message.get("Message-ID")) or f"<local-{uuid.uuid4().hex[:12]}@local>",
        subject=_decode_header(message.get("Subject")),
        from_address=from_address,
        from_name=from_name,
        to_addresses=_split_address_list(message.get("To")),
        cc_addresses=_split_address_list(message.get("Cc")),
        in_reply_to=normalize_message_id(message.get("In-Reply-To")) or "",
        references=parse_reference_header(message.get("References")),
        body_text=body_text,
        body_html=body_html,
        received_at=received_at,
        attachments=_collect_attachments(message),
        raw_path=raw_path,
    )


def _split_address(value: str) -> tuple[str, str]:
    if not value:
        return "", ""
    if "<" in value and ">" in value:
        name = value.split("<")[0].strip().strip('"')
        address = value.split("<")[1].split(">")[0].strip()
        return address.lower(), name
    return value.strip().lower(), ""


def _split_address_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [addr for addr in (_split_address(part.strip())[0] for part in str(value).split(",")) if addr]


def build_outbound_message(settings: Settings, **kwargs: Any) -> EmailMessage:
    """给测试 / 脚本用的便捷函数，避免重复拼装 MailboxClient。"""
    return MailboxClient(settings)._build_message(**kwargs)


def read_eml_files(directory: Path) -> list[InboundEmail]:
    return [parse_email_bytes(path.read_bytes(), raw_path=str(path)) for path in sorted(directory.glob("*.eml"))]


__all__ = [
    "InboundEmail",
    "MailAttachment",
    "MailboxClient",
    "SendResult",
    "build_outbound_message",
    "parse_email_bytes",
    "read_eml_files",
]
