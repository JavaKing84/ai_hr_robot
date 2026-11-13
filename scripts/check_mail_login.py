"""邮箱登录自检：只做连接与认证，不改数据、不发邮件。

用途：跑闭环之前先确认 163 授权码还能用。
退出码：0 = IMAP 与 SMTP 都正常；1 = 至少一个失败。
"""

from __future__ import annotations

import imaplib
import smtplib
import ssl
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_env(path: Path) -> dict[str, str]:
    """读取 .env，返回键值对（不打印敏感值）。"""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def check_imap(host: str, port: int, user: str, password: str) -> tuple[bool, str]:
    """按 163 要求发送 ID 握手后登录，并 SELECT INBOX。"""
    imaplib.Commands.setdefault("ID", ("NONAUTH", "AUTH", "SELECTED"))
    client = None
    try:
        client = imaplib.IMAP4_SSL(host, port, timeout=20)
        typ, _ = client._simple_command(
            "ID", '("name" "ai-hr-robot" "version" "1.0" "vendor" "ai-hr")'
        )
        client._untagged_response(typ, _, "ID")
        typ, data = client.login(user, password)
        if typ != "OK":
            return False, f"LOGIN 被拒绝：{data}"
        client.select("INBOX")
        typ, data = client.uid("search", None, "UID 1:*")
        count = len((data[0] or b"").split()) if typ == "OK" and data else 0
        return True, f"登录成功，收件箱可检索（UID 1:* 命中 {count} 封）"
    except imaplib.IMAP4.error as exc:
        return False, f"IMAP 认证失败：{exc}"
    except Exception as exc:  # noqa: BLE001 - 自检脚本要兜住网络异常
        return False, f"IMAP 连接异常：{type(exc).__name__}: {exc}"
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass


def check_smtp(host: str, port: int, user: str, password: str) -> tuple[bool, str]:
    """只做 SMTP 登录，不发送任何邮件。"""
    server = None
    try:
        server = smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context())
        server.login(user, password)
        return True, "登录成功（未发送邮件）"
    except smtplib.SMTPAuthenticationError as exc:
        return False, f"SMTP 认证失败：{exc.smtp_code} {exc.smtp_error!r}"
    except Exception as exc:  # noqa: BLE001
        return False, f"SMTP 连接异常：{type(exc).__name__}: {exc}"
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass


def main() -> int:
    env = load_env(ROOT / ".env")
    user = env.get("IMAP_USER", "")
    password = env.get("IMAP_PASSWORD", "")
    imap_host = env.get("IMAP_HOST", "imap.163.com")
    imap_port = int(env.get("IMAP_PORT", "993") or 993)
    smtp_host = env.get("SMTP_HOST", "smtp.163.com")
    smtp_port = int(env.get("SMTP_PORT", "465") or 465)

    print("=" * 56)
    print(" 邮箱登录自检（只验证，不收信、不发信）")
    print("=" * 56)
    print(f"账号：{user or '(未配置)'}")
    print(f"授权码：{'*' * len(password)}（长度 {len(password)}）")
    print()

    if not user or not password:
        print("[失败] .env 里 IMAP_USER / IMAP_PASSWORD 没填。")
        return 1

    ok_imap, msg_imap = check_imap(imap_host, imap_port, user, password)
    print(f"[{'OK  ' if ok_imap else '失败'}] IMAP {imap_host}:{imap_port} —— {msg_imap}")

    ok_smtp, msg_smtp = check_smtp(smtp_host, smtp_port, user, password)
    print(f"[{'OK  ' if ok_smtp else '失败'}] SMTP {smtp_host}:{smtp_port} —— {msg_smtp}")

    print()
    if ok_imap and ok_smtp:
        print("结论：邮箱通道正常，可以直接跑「一键跑完整闭环.bat」。")
        return 0

    print("结论：邮箱认证被拒，闭环会在第一步就断掉（表现为飞书没反应）。")
    print("排查顺序：")
    print("  1) 浏览器登录 mail.163.com，看是否有安全验证/短信验证提示，先完成验证；")
    print("  2) 设置 → POP3/SMTP/IMAP，确认 IMAP/SMTP 服务为「已开启」；")
    print("  3) 该页「授权密码」若不确定，点重置，拿到新的 16 位码写回 .env 的 IMAP_PASSWORD；")
    print("  4) 短时间连续登录失败会被 163 临时限制，等 30~60 分钟再试，别反复重试。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
