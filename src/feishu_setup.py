# -*- coding: utf-8 -*-
"""
飞书流水线初始化（阶段 A）：
1. 清理权限探测时创建的残留文件
2. 创建正式多维表格「EvalKit 交付流水线」
3. 建三张表：任务表 / 交付表 / 质检结果表
4. 播种真实任务（dogfooding：用本项目自己的节点当任务）
5. 把租户内用户加为协作者（full_access）
6. 机器人给用户发看板链接

用法：python src/feishu_setup.py
幂等性：重复运行会再建一个新 base，残留的手动删或以后加清理逻辑。
"""
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env():
    env = {}
    with open(os.path.join(ROOT, ".env"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def die(msg):
    print(f"[错误] {msg}")
    sys.exit(1)


class Feishu:
    def __init__(self, app_id, app_secret):
        self.token = self._get_token(app_id, app_secret)

    def _get_token(self, app_id, app_secret):
        r = self._req("POST", "/open-apis/auth/v3/tenant_access_token/internal",
                      {"app_id": app_id, "app_secret": app_secret})
        if r.get("code") != 0:
            die(f"获取 token 失败：{json.dumps(r, ensure_ascii=False)[:300]}")
        return r["tenant_access_token"]

    def _req(self, method, path, payload=None, params=""):
        url = f"https://open.feishu.cn{path}{params}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if getattr(self, "token", None):
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            die(f"{method} {path} -> HTTP {e.code}\n{body[:600]}")
        except urllib.error.URLError as e:
            die(f"{method} {path} 网络失败：{e.reason}")

    def get(self, path, params=""):
        return self._req("GET", path, params=params)

    def post(self, path, payload=None, params=""):
        return self._req("POST", path, payload, params)

    def put(self, path, payload=None, params=""):
        return self._req("PUT", path, payload, params)

    def delete(self, path, params=""):
        return self._req("DELETE", path, params=params)


def sel(field_name, *names):
    """单选字段定义。"""
    return {"field_name": field_name, "type": 3,
            "property": {"options": [{"name": n} for n in names]}}


def main():
    env = load_env()
    fs = Feishu(env["FEISHU_APP_ID"], env["FEISHU_APP_SECRET"])
    print("[1] token OK")

    # ---------- 清理探测残留 ----------
    root = fs.get("/open-apis/drive/explorer/v2/root_folder/meta")
    root_token = root["data"]["token"]
    children = fs.get("/open-apis/drive/v1/files",
                      params="?folder_token=" + root_token + "&page_size=50")
    cleaned = 0
    for f in (children.get("data") or {}).get("files", []):
        if f["name"].startswith("evalkit-权限探测"):
            t = "bitable" if f["type"] == "bitable" else "docx"
            fs.delete(f"/open-apis/drive/v1/files/{f['token']}", params=f"?type={t}")
            cleaned += 1
            print(f"    已删除残留: {f['name']} ({t})")
    print(f"[2] 清理探测残留 x{cleaned}")

    # ---------- 创建正式 base ----------
    r = fs.post("/open-apis/bitable/v1/apps", {"name": "EvalKit 交付流水线"})
    app = r["data"]["app"]
    app_token, base_url = app["app_token"], app["url"]
    print(f"[3] base 已创建: {app_token}")
    print(f"    {base_url}")

    # ---------- 三张表 ----------
    tables = [
        {
            "name": "任务表",
            "fields": [
                {"field_name": "任务ID", "type": 1},
                {"field_name": "标题", "type": 1},
                {"field_name": "描述与验收标准", "type": 1},
                sel("状态", "待领取", "进行中", "待质检", "已通过", "已打回", "已完成"),
                sel("优先级", "P0", "P1", "P2"),
                {"field_name": "负责人", "type": 1},
                {"field_name": "打回次数", "type": 2, "property": {"formatter": "0"}},
                {"field_name": "截止时间", "type": 5},
            ],
        },
        {
            "name": "交付表",
            "fields": [
                {"field_name": "交付ID", "type": 1},
                {"field_name": "任务ID", "type": 1},
                {"field_name": "交付人", "type": 1},
                sel("类型", "文本", "文档", "链接", "代码"),
                {"field_name": "内容链接", "type": 15},
                {"field_name": "内容摘要", "type": 1},
                {"field_name": "版本号", "type": 1},
                {"field_name": "交付时间", "type": 5},
            ],
        },
        {
            "name": "质检结果表",
            "fields": [
                {"field_name": "质检ID", "type": 1},
                {"field_name": "任务ID", "type": 1},
                {"field_name": "交付ID", "type": 1},
                {"field_name": "质检引擎", "type": 1},
                {"field_name": "rubric版本", "type": 1},
                {"field_name": "评分", "type": 2, "property": {"formatter": "0.00"}},
                sel("结论", "通过", "打回"),
                {"field_name": "打回理由", "type": 1},
                {"field_name": "质检时间", "type": 5},
            ],
        },
    ]

    table_ids = {}
    for tb in tables:
        r = fs.post(f"/open-apis/bitable/v1/apps/{app_token}/tables", {"table": tb})
        d = r["data"]
        table_ids[tb["name"]] = d["table_id"]
        print(f"[4] 表已建: {tb['name']} -> {d['table_id']}")

    # ---------- 播种真实任务 ----------
    now_ms = int(time.time() * 1000)
    day_ms = 86400 * 1000
    tasks = [
        {"任务ID": "T001", "标题": "节点2：rubric 定稿",
         "描述与验收标准": "修改 AI 初版草案的 4 个已知毛病；强化「标准版本可追溯」维度；新增「事实正确与表述分离」要求。产出：rubrics/评分标准.md + rubrics/修改记录.md + 3-5 个 case 试评样例。",
         "状态": "待领取", "优先级": "P0", "负责人": "AI-业务程序", "打回次数": 0,
         "截止时间": now_ms + 2 * day_ms},
        {"任务ID": "T002", "标题": "阶段A：飞书三张表 + 读写链路",
         "描述与验收标准": "创建流水线 base，三张表字段齐全，读写 API 跑通，协作者可见。验收：本条任务所在表即为本任务的产出。",
         "状态": "进行中", "优先级": "P0", "负责人": "AI-业务程序", "打回次数": 0,
         "截止时间": now_ms},
        {"任务ID": "T003", "标题": "质检引擎接入：runner + rubric 打分闭环",
         "描述与验收标准": "交付物进入质检队列后由 LLM 按 rubric 打分，打回时写明理由并回写任务状态。依赖：T001 完成。",
         "状态": "待领取", "优先级": "P1", "负责人": "AI-业务程序", "打回次数": 0,
         "截止时间": now_ms + 4 * day_ms},
    ]
    r = fs.post(f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_ids['任务表']}/records/batch_create",
                {"records": [{"fields": t} for t in tasks]})
    print(f"[5] 播种任务 x{len(r['data']['records'])}")

    # ---------- 找出租户内的用户 ----------
    users = fs.get("/open-apis/contact/v3/users",
                   params="?user_id_type=open_id&page_size=50")
    items = (users.get("data") or {}).get("items", [])
    people = [(u["open_id"], u.get("name", "?")) for u in items]
    print(f"[6] 租户用户 x{len(people)}: {', '.join(n for _, n in people)}")

    # ---------- 加协作者 + 发消息 ----------
    for open_id, name in people:
        p = fs.post(f"/open-apis/drive/v1/permissions/{app_token}/members",
                    {"member_type": "openid", "member_id": open_id, "perm": "full_access"},
                    params="?type=bitable&need_notification=true")
        ok = p.get("code") == 0
        print(f"[7] 协作者 {name}: {'full_access 已授' if ok else json.dumps(p, ensure_ascii=False)[:200]}")

        text = (f"【EvalKit 交付流水线】看板已就绪\n"
                f"三张表：任务 / 交付 / 质检结果，已播种 3 个真实任务。\n"
                f"打开看板：{base_url}\n"
                f"本条消息由流水线机器人自动发送——这就是『实时把控』的第一块拼图。")
        m = fs.post("/open-apis/im/v1/messages",
                    {"receive_id": open_id, "msg_type": "text",
                     "content": json.dumps({"text": text})},
                    params="?receive_id_type=open_id")
        ok = m.get("code") == 0
        print(f"[8] 消息 -> {name}: {'已发送' if ok else json.dumps(m, ensure_ascii=False)[:200]}")

    print()
    print("=== 汇总 ===")
    print(json.dumps({"app_token": app_token, "url": base_url, "tables": table_ids},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
