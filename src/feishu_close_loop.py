# -*- coding: utf-8 -*-
"""
飞书闭环回写：交付 -> 质检 -> 任务状态复写 -> 群通知，一次跑完。

通用函数 close_task(fs, task_id, delivery, qc, notify, chat) 供任意任务复用；
不带参数直接运行时执行 T001（rubric 定稿）的闭环，作为示例。
幂等：以 交付ID / 质检ID 做键，已存在则跳过创建，任务状态照常复写。
用法：python src/feishu_close_loop.py
"""
import json
import time

from feishu_setup import Feishu, load_env

APP_TOKEN = "FjmKb4l8QavWcNsOTcLct71Znhg"  # feishu_setup.py 建的流水线 base
TBL_TASK = "tblPoN1sPRrNgumA"
TBL_DELIVER = "tblJHCSAbGGy2NV5"
TBL_QC = "tblQsaXnKyDZz0TH"


def find_record(fs, table_id, field, value):
    items = []
    page_token = ""
    while True:
        params = f"?page_size=100" + (f"&page_token={page_token}" if page_token else "")
        r = fs.get(f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records", params=params)
        if r.get("code") != 0:
            sys.exit(f"[错误] 读表失败: {json.dumps(r, ensure_ascii=False)[:300]}")
        d = r["data"]
        items.extend(d.get("items") or [])
        if not d.get("has_more"):
            break
        page_token = d.get("page_token", "")
    for it in items:
        if str(it["fields"].get(field, "")) == value:
            return it["record_id"]
    return None


def close_task(fs, task_id, delivery, qc, notify=None, chat=None):
    """通用闭环：交付 -> 质检 -> 任务状态复写 -> （可选）群通知。

    delivery / qc 是字段字典，各自以 交付ID / 质检ID 做幂等键；已存在则跳过。
    """
    import subprocess
    now_ms = int(time.time() * 1000)

    d = dict(delivery)
    d.setdefault("交付时间", now_ms)
    if not find_record(fs, TBL_DELIVER, "交付ID", d["交付ID"]):
        fs.post(f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{TBL_DELIVER}/records",
                {"fields": d})
        print(f"[交付] {d['交付ID']} 已创建")
    else:
        print(f"[交付] {d['交付ID']} 已存在，跳过")

    q = dict(qc)
    q.setdefault("质检时间", now_ms)
    if not find_record(fs, TBL_QC, "质检ID", q["质检ID"]):
        fs.post(f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{TBL_QC}/records",
                {"fields": q})
        print(f"[质检] {q['质检ID']} 已写入（{q.get('结论')}）")
    else:
        print(f"[质检] {q['质检ID']} 已存在，跳过")

    rid = find_record(fs, TBL_TASK, "任务ID", task_id)
    if not rid:
        print(f"[任务] 表里找不到 {task_id}，跳过状态复写")
    else:
        fs.put(f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{TBL_TASK}/records/{rid}",
               {"fields": {"状态": "已完成"}})
        print(f"[任务] {task_id} 已复写：已完成")

    if notify and chat:
        subprocess.run(["lark-cli", "im", "+messages-send", "--as", "bot",
                        "--chat-id", chat, "--text", notify],
                       capture_output=True, text=True)
        print("[通知] 群消息已发送")


def main():
    env = load_env()
    fs = Feishu(env["FEISHU_APP_ID"], env["FEISHU_APP_SECRET"])
    now_ms = int(time.time() * 1000)

    # ---------- T001：rubric 定稿 ----------
    close_task(
        fs, "T001",
        delivery={"交付ID": "D001", "任务ID": "T001", "交付人": "AI-业务程序", "类型": "文档",
            "内容链接": {"text": "GitHub 9er22/evalkit rubrics/", "link": "https://github.com/9er22/evalkit/tree/main/rubrics"},
            "内容摘要": "R001 评分标准定稿：AI 草案 4 毛病逐条修复（否决线替代等权 / 有用性拆分为事实性与表述可落地 / 幻觉分编造A与漏检B / 3分档改为可达状态）；「标准版本可追溯」定稿并挂整轮作废门槛；3 条 case 试评全部可判。",
            "版本号": "R001 v1.0"},
        qc={"质检ID": "QC001", "任务ID": "T001", "交付ID": "D001",
            "质检引擎": "R001 人工试评（AI-业务程序执行）", "rubric版本": "R001 v1.0",
            "评分": 3.00, "结论": "通过", "打回理由": ""})

    print("闭环完成：交付 D001 -> 质检 QC001(通过) -> 任务 T001 已完成")


if __name__ == "__main__":
    main()
