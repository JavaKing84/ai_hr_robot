import json
import lark_oapi as lark
from lark_oapi.api.im.v1 import *
import threading
import time
import schedule
import os

import news 

APP_ID = os.getenv("APP_ID")   
APP_SECRET = os.getenv("APP_SECRET")

# 这是你要定时推送的目标群聊 ID（群聊以 oc_ 开头）
# 获取方法：把机器人拉进群，在群里 @ 它发个消息，去终端日志找 message.chat_id
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")


client = lark.Client.builder() \
    .app_id(APP_ID) \
    .app_secret(APP_SECRET) \
    .build()


def send_message(chat_id: str, msg_type: str, content_dict: dict, is_reply: bool = False, message_id: str = ""):
    """统一的发送消息函数，支持 text 和 post 富文本"""
    content_json = json.dumps(content_dict)
    
    if is_reply and message_id:
       
        request = ReplyMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(ReplyMessageRequestBody.builder()
                .content(content_json)
                .msg_type(msg_type)
                .build()) \
            .build()
        response = client.im.v1.message.reply(request)
    else:
       
        request = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(msg_type)
                .content(content_json)
                .build()) \
            .build()
        response = client.im.v1.message.create(request)

    if not response.success():
        print(f"[失败] 发送消息错误: Code={response.code}, Msg={response.msg}")
    else:
        print("[成功] 消息发送成功")


def format_news_as_post(news_list):
    
    if not news_list:
        return None
        
    post_content = []
    for idx, item in enumerate(news_list):
        post_content.append([
            {"tag": "text", "text": f"{idx + 1}. "},
            {"tag": "text", "text": item['title']}
        ])
        post_content.append([
            {"tag": "a", "text": "点击查看原文", "href": item['link']}
        ])
        post_content.append([{"tag": "text", "text": "\n"}])

    return {
        "post": {
            "zh_cn": {
                "title": "IT 科技新闻早报",
                "content": post_content
            }
        }
    }


def handle_message(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    try:

        print(f'[收到消息] 原始数据: {lark.JSON.marshal(data, indent=4)}')

        event = data.event
        message = event.message
        msg_type = message.message_type
        
        if msg_type != "text":
            return
            
        text = json.loads(message.content).get("text", "").strip()
        print(f"收到用户消息: {text}")

        if "新闻" in text or "资讯" in text:
            print("正在抓取新闻，请稍候...")
            news_list = news.get_daily_news()
            
            if not news_list:
                reply_text = "抱歉，暂时没有抓取到相关新闻。"
                send_message(message.chat_id, "text", {"text": reply_text}, is_reply=True, message_id=message.message_id)
            else:
                post_dict = format_news_as_post(news_list)
                send_message(message.chat_id, "post", post_dict, is_reply=True, message_id=message.message_id)
                
        elif "你好" in text or "hi" in text.lower():
            reply_text = "你好！我是 IT 科技新闻机器人。发送“新闻”给我，我就能帮你抓取最新资讯。"
            send_message(message.chat_id, "text", {"text": reply_text}, is_reply=True, message_id=message.message_id)
            
        else:
            reply_text = f"我收到你的消息了：“{text}”。如果你需要看新闻，请发送“新闻”两个字。"
            send_message(message.chat_id, "text", {"text": reply_text}, is_reply=True, message_id=message.message_id)

    except Exception as e:
        print(f"处理消息时发生错误: {e}")

def auto_push_news():
  
    print(f"\n[{time.strftime('%H:%M:%S')}] 定时任务触发，开始主动推送新闻...")
    
    if "oc_" not in TARGET_CHAT_ID:
        print("[警告] TARGET_CHAT_ID 未正确配置，跳过定时推送。")
        return

    news_list = news.get_daily_news()
    if not news_list:
        print("未抓取到新闻，取消推送。")
        return

    post_dict = format_news_as_post(news_list)
    send_message(chat_id=TARGET_CHAT_ID, msg_type="post", content_dict=post_dict)

def run_scheduler():
    """后台定时线程"""
    schedule.every().day.at("19:23").do(auto_push_news)
    print("定时调度服务已启动，等待 19:23 触发...")
    
    while True:
        schedule.run_pending()
        time.sleep(1)


def main():
    print("正在启动飞书机器人长连接...")
    
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

   
    event_handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(handle_message) \
        .build()

    wsClient = lark.ws.Client(
        APP_ID, 
        APP_SECRET, 
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO
    )
    
    print("启动成功。\n1. 去飞书发“新闻”可以实时回复。\n2. 程序会在每天 19:23 自动向群组推送。")
    wsClient.start()

if __name__ == "__main__":
    main()