import os

from dotenv import load_dotenv

load_dotenv()

APP_ID = os.environ.get("FEISHU_APP_ID")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET")

if not APP_ID or not APP_SECRET:
    raise ValueError("缺少飞书凭证：请复制 .env.example 为 .env，并填入 FEISHU_APP_ID 和 FEISHU_APP_SECRET")

FEISHU_CONFIG = {
    "app_id": APP_ID,
    "app_secret": APP_SECRET,
    "chat_id": "oc_edc7b9bb60860ab6549dad0c0533b4b4",
    "host": "https://open.feishu.cn",
}

NEWS_CONFIG = {
    "rss_feeds": [
        "https://36kr.com/feed",
        "https://www.jiqizhixin.com/rss",
        "https://feeds.arstechnica.com/arstechnica/technology-lab",
        "https://techcrunch.com/category/artificial-intelligence/feed/",
    ],
    "important_keywords": ["AI", "大模型", "芯片", "机器人"],
    "top_n": 3,
}
