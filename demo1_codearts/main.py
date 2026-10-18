
import sys
import os
_sys_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _sys_root not in sys.path:
    sys.path.insert(0, _sys_root)

import uvicorn
from fastapi import FastAPI

from shared.news_fetcher import get_top_news

app = FastAPI(
    title="IT新闻抓取服务",
    description="demo1：抓取 RSS 新闻源，按关键词打分，返回最重要的几条",
)

@app.get("/news")
def get_news():
    top_news = get_top_news()

    result = {
        "total": len(top_news),
        "news": top_news,
    }
    return result

@app.get("/run_daily_task")
def run_daily_task():
    top_news = get_top_news()

    lines = ["【IT 科技新闻日报】"]
    for i, news in enumerate(top_news):
        num = i + 1
        lines.append("")
        lines.append("{}. {}".format(num, news["title"]))
        lines.append("   {}".format(news["link"]))

    daily_report = "\n".join(lines)

    return {
        "message": "任务执行完成",
        "news_count": len(top_news),
        "report": daily_report,
    }

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8001)
