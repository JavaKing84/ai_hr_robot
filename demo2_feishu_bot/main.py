
import sys
import os
_sys_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _sys_root not in sys.path:
    sys.path.insert(0, _sys_root)

import json
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from shared.config import FEISHU_CONFIG
from shared.feishu_sender import get_tenant_access_token, send_text_to_chat
from shared.news_fetcher import get_top_news

app = FastAPI(
    title="飞书消息对接服务",
    description="demo2：查询机器人所在群、发送文本消息、发送每日新闻",
)

class SendTextRequest(BaseModel):
    text: str
    chat_id: str = ""

@app.get("/chats")
def list_chats():
    token = get_tenant_access_token()
    if token == "":
        return {"code": -1, "message": "获取 token 失败，请检查 app_id / app_secret"}

    import requests
    url = FEISHU_CONFIG["host"] + "/open-apis/im/v1/chats"
    headers = {
        "Authorization": "Bearer " + token,
    }
    resp = requests.get(url, headers=headers)
    data = resp.json()

    chat_list = []
    if data.get("code") == 0:
        items = data.get("data", {}).get("items", [])
        for item in items:
            one_chat = {
                "name": item.get("name", "未命名"),
                "chat_id": item.get("chat_id", ""),
            }
            chat_list.append(one_chat)
        return {"code": 0, "chats": chat_list}
    else:
        return {"code": data.get("code"), "message": data.get("msg")}

@app.post("/send_text")
def send_text(request: SendTextRequest):
    chat_id = request.chat_id
    if chat_id == "":
        chat_id = FEISHU_CONFIG["chat_id"]

    success = send_text_to_chat(chat_id, request.text)

    if success:
        return {"code": 0, "message": "消息已发送"}
    else:
        return {"code": -1, "message": "发送失败，请看服务端日志"}

@app.post("/send_daily_news")
def send_daily_news():
    top_news = get_top_news()

    lines = ["今日 IT 科技新闻速递："]
    for i, news in enumerate(top_news):
        num = i + 1
        lines.append("")
        lines.append("{}. {}".format(num, news["title"]))
        lines.append("   {}".format(news["link"]))
    message = "\n".join(lines)

    chat_id = FEISHU_CONFIG["chat_id"]
    success = send_text_to_chat(chat_id, message)

    return {
        "code": 0 if success else -1,
        "message": "新闻已发送" if success else "发送失败",
        "news_count": len(top_news),
        "report": message,
    }

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8002)
