
import sys
import os
_sys_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _sys_root not in sys.path:
    sys.path.insert(0, _sys_root)

import uvicorn
from fastapi import FastAPI

from shared.config import FEISHU_CONFIG
from shared.news_fetcher import get_top_news
from shared.feishu_sender import send_text_to_chat

app = FastAPI(
    title="IT科技新闻机器人",
    description="demo3：抓新闻 + 筛选 + 拼日报 + 推送到飞书讨论组的完整机器人服务",
)

def build_daily_report():
    top_news = get_top_news()

    lines = ["今日 IT 科技新闻速递："]
    for i, news in enumerate(top_news):
        num = i + 1
        lines.append("")
        lines.append("{}. {}".format(num, news["title"]))
        lines.append("   {}".format(news["link"]))

    return top_news, "\n".join(lines)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/send_daily_news")
def send_daily_news():
    top_news, report = build_daily_report()

    if len(top_news) == 0:
        return {"code": -1, "message": "没有抓到新闻，本次不发送"}

    chat_id = FEISHU_CONFIG["chat_id"]
    success = send_text_to_chat(chat_id, report)

    result = {
        "code": 0 if success else -1,
        "message": "新闻已推送到飞书讨论组" if success else "发送失败，请看服务端日志",
        "news_count": len(top_news),
        "report": report,
    }
    return result

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8003)
