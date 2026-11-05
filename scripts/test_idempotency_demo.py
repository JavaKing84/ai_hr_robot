"""幂等防重（简历入库去重）手动验证脚本 —— 纯标准库，无需安装任何第三方包。

怎么用：
  1) 先启动服务：  .venv\\Scripts\\python main.py
  2) 再跑本脚本：  .venv\\Scripts\\python scripts\\test_idempotency_demo.py

重要：跑之前请先停掉服务、删除 var/hr_robot.db。
（因为之前用“无邮箱/无电话”的简历测过，库里已留下脏数据，会让本测试的总数为 5 而非 1。）

本脚本验证的是“简历入库去重”：同一份简历投 N 次，候选人表只留 1 条。
注意：这不是 §4-A.1 的“消息发送幂等”（那个按 Message-ID 防重，走面试通知/主管分发链路）。
"""

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"

# 关键：简历里必须带「邮箱 + 电话」！
# 系统按 email/phone 去重（见 repository/candidate.py:17 find_duplicate），
# 没有联系方式 = 没有去重键 = 每次投递都被当成“新简历”。
RESUME_TEXT = (
    "姓名：王小明\n"
    "邮箱：wangxm@test.com\n"
    "电话：13800138000\n"
    "应聘岗位：后端开发\n"
    "学历：本科\n"
    "专业：计算机科学与技术\n"
    "经验：3年\n"
    "技能：熟悉 Python、FastAPI、PostgreSQL\n"
)


def _post_ingest() -> dict:
    """调用 /ingest/text 投递一份简历，返回服务端 JSON。"""
    payload = json.dumps({"text": RESUME_TEXT, "filename": "王小明.txt"}).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/api/v1/recruitment/ingest/text",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_candidates(name: str) -> dict:
    """调用 /candidates 按姓名查询，返回候选人列表（数据库的真相）。"""
    url = f"{BASE}/api/v1/recruitment/candidates?name={name}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    print("=== 第 1 步：连发 5 次同一份简历（1 次新建 + 4 次重复投递）===")
    for i in range(5):
        try:
            d = _post_ingest()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            print(f"  第{i + 1}次 -> HTTP 错误 {e.code}: {body}")
            continue
        dup = d.get("is_duplicate")
        cid = d.get("candidate_id")
        print(
            f"  第{i + 1}次 -> is_duplicate={dup}  "
            f"candidate_id={cid}  status={d.get('status')}  message={d.get('message')}"
        )

    print("\n=== 第 2 步：查候选人列表（数据库真相）===")
    try:
        c = _get_candidates("王小明")
    except urllib.error.HTTPError as e:
        print(f"  查询失败：{e.code}")
        return
    total = c.get("total")
    print(f"  王小明 总数 = {total}  （期望 1）")
    for it in c.get("items", []):
        print(f"    id={it['id']} name={it['name']} email={it.get('email')} status={it['status']}")

    print("\n=== 验收结论 ===")
    if total == 1:
        print("  ✅ 幂等防重生效：5 次相同投递只保留 1 条记录")
    else:
        print(
            f"  ❌ 异常：期望 1 条，实际 {total} 条"
            "（检查简历是否带邮箱/电话，以及 var/hr_robot.db 是否清空过）"
        )


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as e:
        print(f"连不上服务：{e}。请先启动：.venv\\Scripts\\python main.py")
        sys.exit(1)
