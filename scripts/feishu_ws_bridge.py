"""飞书长连接桥接：把 WS 事件转发到本项目的 webhook，复用现有业务代码。

用途：本地开发时无需公网地址 / 内网穿透，也能收到主管在飞书里的真实回复。
前置：飞书开放平台「事件订阅」需选择「使用长连接接收事件」，
      并订阅 im.message.receive_v1，且应用版本已发布。

长连接收到消息 -> 组装成标准 2.0 事件 -> POST http://127.0.0.1:8000/api/v1/feishu/webhook
之后的处理逻辑（识别主管、解析面试名单、回消息、发面试通知）全部走项目原有代码。
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

# 允许 `python scripts/feishu_ws_bridge.py` 直接运行（把仓库根加入 import 路径）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import scripts._bootstrap  # noqa: F401,E402  # 把仓库根加入 sys.path

from config.settings import get_settings
from lark_oapi.core.enum import LogLevel
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from lark_oapi.ws import Client

LOCAL_WEBHOOK = "http://127.0.0.1:8000/api/v1/feishu/webhook"


def _attr(obj: object, *names: str, default: object = None) -> object:
    """安全取多级属性，任意一级为 None 就返回默认值。"""
    cur = obj
    for name in names:
        cur = getattr(cur, name, None) if cur is not None else None
    return cur if cur is not None else default


def on_message(data: object) -> None:
    """长连接收到 im.message.receive_v1 时的回调。"""
    try:
        header = getattr(data, "header", None)
        sender = _attr(data, "event", "sender")
        message = _attr(data, "event", "message")
        open_id = _attr(sender, "sender_id", "open_id", default="")
        text = _attr(message, "content", default="{}")

        payload = {
            "schema": "2.0",
            "header": {
                "event_id": getattr(header, "event_id", "") or "ws-event",
                "event_type": "im.message.receive_v1",
                "create_time": getattr(header, "create_time", "") or "",
                "token": "",
                "app_id": getattr(header, "app_id", "") or "",
                "tenant_key": getattr(header, "tenant_key", "") or "",
            },
            "event": {
                "sender": {
                    "sender_id": {"open_id": open_id},
                    "sender_type": getattr(sender, "sender_type", "") or "user",
                    "tenant_key": getattr(sender, "tenant_key", "") or "",
                },
                "message": {
                    "message_id": getattr(message, "message_id", "") or "",
                    "chat_id": getattr(message, "chat_id", "") or "",
                    "message_type": getattr(message, "message_type", "") or "text",
                    "content": text,
                    "create_time": str(getattr(message, "create_time", "") or ""),
                    "mentions": [],
                },
            },
        }
        print(f"[WS] message from {open_id}: {text}", flush=True)

        request = urllib.request.Request(
            LOCAL_WEBHOOK,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            print(f"[WS] webhook -> {response.read().decode('utf-8')}", flush=True)
    except Exception as exc:  # noqa: BLE001 - 单条事件失败不能拖垮长连接
        print(f"[WS] forward failed: {type(exc).__name__}: {exc}", flush=True)


def main() -> int:
    settings = get_settings()
    if not (settings.feishu_app_id and settings.feishu_app_secret):
        print("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET", file=sys.stderr)
        return 1

    handler = (
        EventDispatcherHandler.builder(
            settings.feishu_encrypt_key or "",
            settings.feishu_verification_token or "",
        )
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    print("[WS] connecting to Feishu... (keep this window open)", flush=True)
    client = Client(
        settings.feishu_app_id,
        settings.feishu_app_secret,
        event_handler=handler,
        log_level=LogLevel.INFO,
        auto_reconnect=True,
    )
    client.start()  # 阻塞运行，直到手动关闭窗口
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
