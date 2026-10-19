import json, time, hmac, hashlib, base64, requests

def gen_sign(secret, timestamp):
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def send_card(webhook, secret, title, lines):
    timestamp = str(int(time.time()))
    md = "\n".join(lines)
    body = {
        "timestamp": timestamp,
        "sign": gen_sign(secret, timestamp),
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue"
            },
            "elements": [{
                "tag": "div",
                "text": {"tag": "lark_md", "content": md}
            }]
        }
    }
    r = requests.post(webhook, json=body, timeout=10)
    print(r.json())

if __name__ == "__main__":
    with open("config.json", encoding="utf-8") as f:
        cfg = json.load(f)
    # 测试发一张卡片
    send_card(cfg["webhook"], cfg["secret"], "今日 IT 热点", [
        "**1. 华为发布最新芯片** [阅读](https://zhuanlan.zhihu.com/p/2083900254261359135)",
        "**2. 大模型价格战再次打响** [阅读](https://zhuanlan.zhihu.com/p/18669120376)"
    ])