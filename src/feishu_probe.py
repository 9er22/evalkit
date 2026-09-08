# -*- coding: utf-8 -*-
"""
飞书权限探测：拿 tenant_access_token，逐个调核心 API，
收集"缺哪个权限"的真实报错（错误码 99991672 及类似），
输出需要开通的权限清单。

用法：python src/feishu_probe.py
"""
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env():
    env = {}
    p = os.path.join(ROOT, ".env")
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def die(msg):
    print(f"[错误] {msg}")
    sys.exit(1)


def post(url, payload, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode("utf-8", "replace")[:500]}
    except urllib.error.URLError as e:
        die(f"网络失败：{e.reason}")


def get(url, token):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode("utf-8", "replace")[:500]}
    except urllib.error.URLError as e:
        die(f"网络失败：{e.reason}")


def main():
    env = load_env()
    app_id = env.get("FEISHU_APP_ID", "")
    app_secret = env.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        die(".env 里缺 FEISHU_APP_ID / FEISHU_APP_SECRET")

    # 1. 拿 tenant_access_token（这一步不需要任何权限）
    r = post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
             {"app_id": app_id, "app_secret": app_secret})
    if r.get("code") != 0:
        die(f"获取 token 失败：{json.dumps(r, ensure_ascii=False)[:300]}")
    token = r["tenant_access_token"]
    print(f"[1] tenant_access_token 获取成功（{r.get('expire')} 秒有效）——应用凭证正确\n")

    # 2. 逐个探测核心 API，收集缺权限的报错
    probes = [
        ("多维表格·创建 base（bitable 核心读写）",
         "POST", "https://open.feishu.cn/open-apis/bitable/v1/apps",
         {"name": "evalkit-权限探测-可删除"}),
        ("云空间·根目录 meta（drive 基础）",
         "GET", "https://open.feishu.cn/open-apis/drive/explorer/v2/root_folder/meta", None),
        ("通讯录·列用户（解析任务负责人用）",
         "GET", "https://open.feishu.cn/open-apis/contact/v3/users?user_id_type=open_id&page_size=1", None),
        ("云文档·创建 docx（交付物文档用）",
         "POST", "https://open.feishu.cn/open-apis/docx/v1/documents",
         {"title": "evalkit-权限探测-可删除"}),
        ("消息·发消息（机器人通知用）",
         "POST", "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
         {"receive_id": "ou_probe", "msg_type": "text", "content": json.dumps({"text": "probe"})}),
    ]

    results = []
    for label, method, url, payload in probes:
        if method == "POST":
            r = post(url, payload, token)
        else:
            r = get(url, token)
        code = r.get("code", r.get("_http", "?"))
        msg = r.get("msg", "")
        body = r.get("_body", "")
        results.append((label, code, msg or body[:200]))
        print(f"[探测] {label}")
        print(f"       code={code}  {msg[:180]}")
        if body and not msg:
            print(f"       body: {body[:180]}")
        print()
        time.sleep(0.3)

    print("=" * 60)
    print("汇总：code=0 表示该权限已可用；其余报错对应的权限需要开通")
    print("=" * 60)


if __name__ == "__main__":
    main()
