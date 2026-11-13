"""飞书 Webhook：事件接收与分流。

路由范式借鉴 ``Dify-FeiShu-bot`` 的「一个 bot 一个 router + /webhook」写法，
事件解析、解密与签名相关能力交给官方 SDK（``client/feishu.py`` 封装）。

支持的 event_type：
- ``im.message.receive_v1``：主管在飞书里自由文本回复面试安排
- ``card.action.trigger``：主管点击卡片按钮（与自由文本进入同一个解析入口）
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request

from core.deps import Services, get_services
from core.logging import get_logger, log_extra
from schema.candidate import FeishuWebhookResponse

logger = get_logger(__name__)

router = APIRouter(prefix="/feishu", tags=["feishu"])


@router.post("/webhook", response_model=FeishuWebhookResponse, summary="飞书事件回调")
async def feishu_webhook(request: Request, services: Services = Depends(get_services)) -> Any:
    client = services.container.feishu
    raw = await request.body()
    payload = client.decode_webhook(raw, dict(request.headers))

    # 飞书后台配置回调地址时的挑战应答
    if payload.get("type") == "url_verification" or "challenge" in payload:
        return {"challenge": payload.get("challenge", "")}

    event_type = client.event_type(payload)
    inbound = client.normalize_event(payload)
    if inbound is None:
        logger.info("feishu.event_ignored", extra=log_extra(event_type=event_type))
        return FeishuWebhookResponse(ok=True, event_type=event_type, handled=False)

    logger.info(
        "feishu.event_received",
        extra=log_extra(event_type=event_type, sender=inbound.sender_id, chars=len(inbound.text)),
    )

    # 飞书 3 秒内没收到 200 会重投同一条事件。重复处理会让主管收到两遍请示、
    # 候选人收到两封改期确认——按 (channel, message_id) 在入口挡掉。
    if inbound.message_id and await services.messages.seen_inbound("feishu", inbound.message_id):
        logger.info("feishu.duplicate_skipped", extra=log_extra(message_id=inbound.message_id))
        return FeishuWebhookResponse(
            ok=True, event_type=event_type, handled=False, detail={"reason": "重复回调"}
        )

    # 图片消息（主管发招聘需求截图）：取 image_key → 下载 → 交给招聘需求通道。
    image_key = _image_key(payload)
    if image_key and not inbound.text:
        image = await client.download_resource(inbound.message_id, image_key, resource_type="image")
        if image:
            outcome = await services.feedback.handle_image(inbound, image)
            return FeishuWebhookResponse(
                ok=True,
                event_type=event_type,
                handled=outcome.handled,
                detail={"action": outcome.action, **outcome.detail, "reply": outcome.reply_text[:200]},
            )

    outcome = await services.feedback.handle(inbound)
    return FeishuWebhookResponse(
        ok=True,
        event_type=event_type,
        handled=outcome.handled,
        detail={"action": outcome.action, **outcome.detail, "reply": outcome.reply_text[:200]},
    )


def _image_key(payload: dict[str, Any]) -> str:
    """飞书图片消息的 image_key 在 event.message.content 里（JSON 字符串）。"""
    message = (payload.get("event") or {}).get("message") or {}
    if (message.get("message_type") or "") != "image":
        return ""
    raw = message.get("content") or "{}"
    try:
        return str(json.loads(raw).get("image_key") or "")
    except (TypeError, ValueError):
        return ""
