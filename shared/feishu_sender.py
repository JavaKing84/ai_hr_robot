
import requests
from shared.config import FEISHU_CONFIG

def get_tenant_access_token():
    url = FEISHU_CONFIG["host"] + "/open-apis/auth/v3/tenant_access_token/internal"

    body = {
        "app_id": FEISHU_CONFIG["app_id"],
        "app_secret": FEISHU_CONFIG["app_secret"],
    }

    resp = requests.post(url, json=body)
    data = resp.json()

    if data.get("code") == 0:
        return data["tenant_access_token"]
    else:
        print("[错误] 获取 token 失败：", data)
        return ""

def send_text_to_chat(chat_id, text_content):
    token = get_tenant_access_token()
    if token == "":
        print("[错误] 拿不到 token，无法发消息")
        return False

    url = FEISHU_CONFIG["host"] + "/open-apis/im/v1/messages?receive_id_type=chat_id"

    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    }

    import json
    body = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text_content}, ensure_ascii=False),
    }

    resp = requests.post(url, headers=headers, json=body)
    data = resp.json()

    if data.get("code") == 0:
        print("[成功] 消息已发送")
        return True
    else:
        print("[错误] 发送失败：", data)
        return False

if __name__ == "__main__":
    test_chat_id = FEISHU_CONFIG["chat_id"]
    if test_chat_id.startswith("PLACEHOLDER"):
        print("请先在 shared/config.py 里把 chat_id 改成真实的群 ID")
    else:
        send_text_to_chat(test_chat_id, "你好，这是飞书消息对接测试")
