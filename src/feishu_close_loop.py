# -*- coding: utf-8 -*-
"""
飞书闭环回写（T001：rubric 定稿的交付 → 质检 → 状态复写）：
1. 交付表新建 D001（rubric 三件套，R001 v1.0）
2. 质检结果表新建 QC001（R001 试评：3 条通过 + 整轮可追溯 3 分）
3. 任务表 T001 状态复写：待领取 -> 已完成
幂等：D001 已存在则跳过创建，状态照常复写。
用法：python src/feishu_close_loop.py
"""
import json
import os
import sys
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


def main():
    env = load_env()
    fs = Feishu(env["FEISHU_APP_ID"], env["FEISHU_APP_SECRET"])
    now_ms = int(time.time() * 1000)

    # ---------- 1. 交付记录 ----------
    d001 = find_record(fs, TBL_DELIVER, "交付ID", "D001")
    if not d001:
        r = fs.post(f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{TBL_DELIVER}/records", {"fields": {
            "交付ID": "D001", "任务ID": "T001", "交付人": "AI-业务程序", "类型": "文档",
            "内容链接": {"text": "GitHub 9er22/evalkit rubrics/", "link": "https://github.com/9er22/evalkit/tree/main/rubrics"},
            "内容摘要": "R001 评分标准定稿：AI 草案 4 毛病逐条修复（否决线替代等权 / 有用性拆分为事实性与表述可落地 / 幻觉分编造A与漏检B / 3分档改为可达状态）；「标准版本可追溯」定稿并挂整轮作废门槛；3 条 case 试评全部可判。",
            "版本号": "R001 v1.0", "交付时间": now_ms}})
        d001 = r["data"]["record"]["record_id"]
        print(f"[1] 交付记录 D001 已创建")
    else:
        print(f"[1] 交付记录 D001 已存在，跳过")

    # ---------- 2. 质检记录 ----------
    qc001 = find_record(fs, TBL_QC, "质检ID", "QC001")
    if not qc001:
        r = fs.post(f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{TBL_QC}/records", {"fields": {
            "质检ID": "QC001", "任务ID": "T001", "交付ID": "D001",
            "质检引擎": "R001 人工试评（AI-业务程序执行）", "rubric版本": "R001 v1.0",
            "评分": 3.00, "结论": "通过",
            "打回理由": "",
            "质检时间": now_ms}})
        print(f"[2] 质检记录 QC001 已创建（通过）")
    else:
        print(f"[2] 质检记录 QC001 已存在，跳过")

    # ---------- 3. 状态复写 ----------
    t001 = find_record(fs, TBL_TASK, "任务ID", "T001")
    if not t001:
        sys.exit("[错误] 任务表里找不到 T001")
    fs.put(f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{TBL_TASK}/records/{t001}",
           {"fields": {"状态": "已完成"}})
    print(f"[3] T001 状态已复写：已完成")

    print("闭环完成：交付 D001 -> 质检 QC001(通过) -> 任务 T001 已完成")


if __name__ == "__main__":
    main()
