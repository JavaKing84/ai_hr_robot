import json
import time
import threading
import schedule
import requests
from bs4 import BeautifulSoup
import lark_oapi as lark
from lark_oapi.api.im.v1 import *

# ====================== 读取配置文件 ======================
def load_config():
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)

cfg = load_config()
APP_ID = cfg["app_id"]
APP_SECRET = cfg["app_secret"]
WEBHOOK_URL = cfg["webhook"]
WEBHOOK_SECRET = cfg["secret"]
DEFAULT_KEYWORD = cfg["search_keyword"]
CRON_TIME_LIST = cfg["cron_time_list"]

# 初始化飞书客户端
client = lark.Client.builder() \
    .app_id(APP_ID) \
    .app_secret(APP_SECRET) \
    .log_level(lark.LogLevel.INFO) \
    .build()

# ====================== 新闻搜索&打分模块 ======================
def search_top3_news(keyword):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    url = f"https://cn.bing.com/search?q={keyword}"
    resp = requests.get(url, headers=headers)
    soup = BeautifulSoup(resp.text, "html.parser")
    items = soup.select("li.b_algo")
    news_list = []
    for item in items[:10]:
        try:
            title_tag = item.select_one("h2 a")
            title = title_tag.get_text(strip=True)
            link = title_tag["href"]
            abstract = item.select_one("p").get_text(strip=True) if item.select_one("p") else ""
            score = len(title) + len(abstract)*0.3
            news_list.append({"title": title, "link": link, "abstract": abstract, "score": score})
        except Exception as e:
            continue
    news_list.sort(key=lambda x:x["score"], reverse=True)
    top3 = news_list[:3]
    out_text = f"【🔥 关键词：{keyword} 热度TOP3新闻】\n"
    for idx, n in enumerate(top3,1):
        out_text += f"\n{idx}. {n['title']}\n{n['abstract']}\n{n['link']}\n"
    return out_text

# Webhook单向推送（定时任务）
def webhook_post_msg(text):
    data = {"msg_type":"text", "content":{"text":text}}
    requests.post(WEBHOOK_URL, json=data)

def cron_task():
    print(f"定时任务触发，搜索关键词：{DEFAULT_KEYWORD}")
    result = search_top3_news(DEFAULT_KEYWORD)
    webhook_post_msg(result)

# ====================== 飞书消息处理函数 ======================
def send_msg_to_user(receive_id, text):
    req = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .request_body(CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type("text")
            .content(json.dumps({"text": text}))
            .build()) \
        .build()
    resp = client.im.v1.message.create(req)
    if not resp.success():
        print(f"发送消息失败：{resp.msg}, {resp.error}")

# 消息事件回调函数
def handle_message_event(event: P2ImMessageReceiveV1):
    msg = event.event.message
    chat_id = msg.chat_id
    content_json = json.loads(msg.content)
    user_text = content_json.get("text","").strip()
    print(f"收到用户消息：{user_text}")

    # 直接查询默认新闻
    if user_text == "news" or "发新闻" in user_text or "查新闻" in user_text or "来新闻" in user_text:
        reply = search_top3_news(DEFAULT_KEYWORD)
    # 自定义关键词搜索
    elif user_text.startswith("搜索"):
        kw = user_text.replace("搜索","").strip()
        if not kw:
            reply = "请输入关键词，例如：搜索 新能源"
        else:
            reply = search_top3_news(kw)
    elif user_text.startswith("search "):
        kw = user_text.replace("search ","").strip()
        if not kw:
            reply = "请输入关键词，例如：search 新能源"
        else:
            reply = search_top3_news(kw)
    elif user_text == "help" or user_text == "帮助":
        reply = """🤖 AI新闻助手指令菜单：
news / 发新闻 / 查新闻 → 使用默认关键词查询TOP3新闻
搜索xxx / search xxx → 自定义关键词搜索（例：搜索 芯片）
help / 帮助 → 查看帮助
定时任务会在config配置时间自动推送新闻"""
    else:
        reply = f"收到消息：{user_text}\n输入 help 查看新闻指令"
    send_msg_to_user(chat_id, reply)


# ====================== Schedule定时任务线程 ======================
def schedule_thread_run():
    for tm in CRON_TIME_LIST:
        schedule.every().day.at(tm).do(cron_task)
    print(f"定时任务已注册，推送时间：{CRON_TIME_LIST}")
    while True:
        schedule.run_pending()
        time.sleep(10)

# ====================== 程序入口 ======================
if __name__ == "__main__":
    # 启动定时后台线程
    t = threading.Thread(target=schedule_thread_run, daemon=True)
    t.start()
    print("定时任务线程启动成功")

    # 长连接事件处理器：长连接模式，两个参数传空字符串""
    event_handler = lark.EventDispatcherHandler.builder("", "")\
        .register_p2_im_message_receive_v1(handle_message_event)\
        .build()

    # 新建WS长连接客户端
    ws_client = lark.ws.Client(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO
    )

    print("启动飞书长连接监听，等待用户消息...")
    ws_client.start()