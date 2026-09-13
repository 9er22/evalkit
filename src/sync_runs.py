# -*- coding: utf-8 -*-
"""
评测数据同步：runs/ → 飞书多维表格（5 张新表，与已有任务/交付/质检三表同 base）。

幂等：以唯一键查重（评测轮次=run_id、判定明细=run_id+case_id、归因=轮次+组别、
case库=case_id、rubric版本=版本），已存在则跳过。

导入范围：
- 所有含 judged/summary.json 的轮次（含 D7 作废的两轮 V4.1，标注"作废"留痕不删）
- 每轮 30 条判定明细（judged/C*.json）
- 闭卷轮的失败归因（meta.json 的 gold_chunk + retrieval_hit 计算，与报告口径一致）
- case 库（cases/*.md 解析：输入 / 期望 / 语料依据 / 关注点）
- rubric 版本表（来自 rubrics/评分标准.md 版本历史，硬编码并注明来源）

用法：python src/sync_runs.py
"""
import glob
import json
import os
import re
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from feishu_setup import Feishu, load_env, sel

APP_TOKEN = "FjmKb4l8QavWcNsOTcLct71Znhg"

# 作废轮次（决策记录 D7）：模型 v4.1-flash-beta 次日过期，口径不可追溯
INVALIDATED_RUNS = {
    "20260909-143435": "作废（D7）：模型 v4.1-flash-beta 次日过期，口径不可追溯",
    "20260909-143859": "作废（D7）：模型 v4.1-flash-beta 次日过期，口径不可追溯",
}
BASELINE_TAGS = {
    "20260908-202726": ["开卷基线"],
    "20260909-173030": ["闭卷基线"],
}

MODE_LABEL = {"open-book": "开卷", "closed-book": "闭卷"}
FAIL_TYPE_LABEL = {"A": "A-编造", "B": "B-漏检"}

# 「拒绝是否恰当」只对 edge / adversarial 计分（R001 v1.4 修改记录第 6 条）。
# 与 src/qc_engine.py 的 REFUSE_DIM_CATEGORIES 保持一致——改这里必须同步改那边。
REFUSE_DIM_CATEGORIES = ("edge", "adversarial")

# D1（R001 v1.5）：维度 4 的**显示名**改为「边界处理是否恰当」，但**数据键名不变**。
# judged JSON / summary.json / qc_engine.DIMS 里的键名仍是"拒绝是否恰当"——
# 历史 60 条 judged 以该键名存储，改键名 = 历史数据读不到 = 被迫全量重判 = 冻结白守。
# 飞书列名是显示层，跟着改；代码里的键名一律不动。
DIM_COL = {
    "事实性": "事实性",
    "表述与可落地": "表述与可落地",
    "格式遵循": "格式遵循",
    "拒绝是否恰当": "边界处理是否恰当",   # ← 唯一一处 显示名 ≠ 键名
}

# 重同步时要刷新的字段（会随计分范围/判定结果变化的那些）
RESYNC_FIELDS = [DIM_COL[d] for d in ("事实性", "表述与可落地", "格式遵循", "拒绝是否恰当")] \
    + ["总分", "通过线", "结论", "失败类"]


def msel(field_name, *names):
    """多选字段定义。"""
    return {"field_name": field_name, "type": 4,
            "property": {"options": [{"name": n} for n in names]}}


def num(field_name, formatter="0"):
    return {"field_name": field_name, "type": 2, "property": {"formatter": formatter}}


def to_ms(dt_str):
    """飞书日期字段要求 unix 毫秒时间戳。"""
    try:
        return int(datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def load_keys(fs, table_id, key_field):
    """读回某字段的全部值（用于幂等判断）。"""
    keys, page_token = set(), ""
    while True:
        params = f"?page_size=200" + (f"&page_token={page_token}" if page_token else "")
        r = fs.get(f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records", params=params)
        if r.get("code") != 0:
            sys.exit(f"[错误] 读表失败: {json.dumps(r, ensure_ascii=False)[:300]}")
        d = r["data"]
        for it in d.get("items") or []:
            keys.add(str(it["fields"].get(key_field, "")))
        if not d.get("has_more"):
            break
        page_token = d.get("page_token", "")
    return keys


def load_pairs(fs, table_id, f1, f2):
    """读回两字段组合（归因表幂等键：轮次+组别）。"""
    pairs, page_token = set(), ""
    while True:
        params = f"?page_size=200" + (f"&page_token={page_token}" if page_token else "")
        r = fs.get(f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records", params=params)
        if r.get("code") != 0:
            sys.exit(f"[错误] 读表失败: {json.dumps(r, ensure_ascii=False)[:300]}")
        d = r["data"]
        for it in d.get("items") or []:
            pairs.add((str(it["fields"].get(f1, "")), str(it["fields"].get(f2, ""))))
        if not d.get("has_more"):
            break
        page_token = d.get("page_token", "")
    return pairs


def batch_create(fs, table_id, rows, key_field=None, existing=None):
    """幂等批量写入：existing 为已存在的键集合；单批 ≤200。"""
    missing = []
    for row in rows:
        if key_field is not None and existing is not None and str(row.get(key_field, "")) in existing:
            continue
        missing.append({"fields": row})
    created = 0
    for i in range(0, len(missing), 200):
        chunk = missing[i:i + 200]
        r = fs.post(
            f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records/batch_create",
            {"records": chunk})
        if r.get("code") != 0:
            sys.exit(f"[错误] 批量写入失败: {json.dumps(r, ensure_ascii=False)[:400]}")
        created += len(chunk)
    return created, len(rows) - created


# ---------- 表结构 ----------

TABLES = [
    {
        "name": "评测轮次",
        "fields": [
            {"field_name": "run_id", "type": 1},
            sel("模式", "开卷", "闭卷"),
            {"field_name": "模型", "type": 1},
            {"field_name": "rubric版本", "type": 1},
            {"field_name": "语料hash", "type": 1},
            num("通过", "0"),
            num("失败", "0"),
            num("通过率", "0.0"),
            {"field_name": "失败分布", "type": 1},
            msel("基线标签", "开卷基线", "闭卷基线", "回归基线"),
            sel("状态", "有效", "作废"),
            {"field_name": "判定时间", "type": 5},
            {"field_name": "备注", "type": 1},
        ],
    },
    {
        "name": "case判定明细",
        "fields": [
            {"field_name": "记录ID", "type": 1},
            {"field_name": "run_id", "type": 1},
            {"field_name": "case_id", "type": 1},
            sel("类别", "normal", "edge", "adversarial"),
            sel("模式", "开卷", "闭卷"),
            num("事实性", "0"),
            num("表述与可落地", "0"),
            num("格式遵循", "0"),
            num("边界处理是否恰当", "0"),   # 显示名，键名仍是"拒绝是否恰当"（见 DIM_COL）
            sel("失败类型", "A-编造", "B-漏检", "无"),
            sel("结论", "通过", "失败"),
            {"field_name": "失败类", "type": 1},
            num("总分", "0"),
            num("通过线", "0"),
            {"field_name": "证据", "type": 1},
            {"field_name": "rubric版本", "type": 1},
            sel("归因组别", "检索命中", "检索漏检", "应拒答", "无"),
        ],
    },
    {
        "name": "失败归因",
        "fields": [
            {"field_name": "轮次", "type": 1},
            sel("组别", "检索命中", "检索漏检", "应拒答"),
            num("case数", "0"),
            num("失败数", "0"),
            {"field_name": "失败case", "type": 1},
            {"field_name": "结论", "type": 1},
        ],
    },
    {
        "name": "case库",
        "fields": [
            {"field_name": "case_id", "type": 1},
            sel("类别", "normal", "edge", "adversarial"),
            {"field_name": "输入", "type": 1},
            {"field_name": "期望", "type": 1},
            {"field_name": "语料依据", "type": 1},
            {"field_name": "关注点", "type": 1},
        ],
    },
    {
        "name": "rubric版本",
        "fields": [
            {"field_name": "版本", "type": 1},
            {"field_name": "日期", "type": 5},
            {"field_name": "变更", "type": 1},
            {"field_name": "影响范围", "type": 1},
            sel("状态", "现行", "历史"),
        ],
    },
]

# ---------- rubric 版本（来源：rubrics/评分标准.md 第五节"版本历史"） ----------

RUBRIC_VERSIONS = [
    {"版本": "R001 v1.0", "日期": "2026-09-08 00:00:00",
     "变更": "初版定稿（AI 草案修改而来）：否决线替代等权 / 有用性拆分为事实性与表述可落地 / 幻觉分编造A与漏检B / 3分档改为可达状态；「标准版本可追溯」挂整轮作废门槛",
     "影响范围": "无历史 runs 需回溯", "状态": "历史"},
    {"版本": "R001 v1.1", "日期": "2026-09-09 00:00:00",
     "变更": "「拒绝是否恰当」3 分档加替代路径正反例；新增扣分规则（同缺陷可跨维各扣一次，单维不叠加）",
     "影响范围": "引擎 v1.0 判过的 C019 已作废重判", "状态": "历史"},
    {"版本": "R001 v1.2", "日期": "2026-09-09 00:00:00",
     "变更": "3 分档拆两类场景：知识边界类要指外部文档；安全/规则类改为拉回正确用途范围",
     "影响范围": "v1.1 判的 C026 已作废重判", "状态": "历史"},
    {"版本": "R001 v1.3", "日期": "2026-09-09 00:00:00",
     "变更": "改判定结构：模型只答事实清单（是/否/不适用），分数由代码按映射表算",
     "影响范围": "治本：同一输出 v1.1 判 10/12、v1.2 判 12/12，问题在让模型自己报分", "状态": "历史"},
    {"版本": "R001 v1.4", "日期": "2026-09-09 00:00:00",
     "变更": "「格式遵循」在应拒绝（R0=是）时不计分——全量判定发现 C029 因没按请求形态输出被扣到 1 分，等于用格式分惩罚正确行为",
     "影响范围": "现行版", "状态": "现行"},
]


def section(text, name):
    m = re.search(r"^##\s+" + name + r"\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def parse_cases():
    rows = []
    for path in sorted(glob.glob(os.path.join(ROOT, "cases", "C*.md"))):
        text = open(path, encoding="utf-8").read()
        cid = re.search(r"^id:\s*(C\d+)", text, re.M)
        cat = re.search(r"^category:\s*(\w+)", text, re.M)
        if not cid:
            continue
        rows.append({
            "case_id": cid.group(1),
            "类别": cat.group(1) if cat else "",
            "输入": section(text, "输入"),
            "期望": section(text, "期望"),
            "语料依据": section(text, "语料依据"),
            "关注点": section(text, "我为什么关心这个 case"),
        })
    return rows


def parse_runs_and_verdicts():
    runs, verdicts, attributions = [], [], []
    for d in sorted(glob.glob(os.path.join(ROOT, "runs", "*"))):
        if not os.path.isdir(d):
            continue
        sfile = os.path.join(d, "judged", "summary.json")
        if not os.path.exists(sfile):
            continue
        rid = os.path.basename(d)
        with open(sfile, encoding="utf-8") as f:
            s = json.load(f)
        meta = {}
        mfile = os.path.join(d, "meta.json")
        if os.path.exists(mfile):
            with open(mfile, encoding="utf-8") as f:
                meta = json.load(f)

        model = s.get("judge_model") or meta.get("model", "")
        mode_label = MODE_LABEL.get(meta.get("mode", "open-book"), "开卷")
        status = "作废" if rid in INVALIDATED_RUNS else "有效"
        judged_at = (s.get("judged_at", "") or "").replace("T", " ")

        runs.append({
            "run_id": rid,
            "模式": mode_label,
            "模型": model,
            "rubric版本": s.get("rubric", ""),
            "语料hash": (meta.get("corpus") or {}).get("sha256", ""),
            "通过": s.get("通过", 0),
            "失败": s.get("失败", 0),
            "通过率": round((s.get("通过率", 0) or 0) * 100, 1),
            "失败分布": json.dumps(s.get("失败分布", {}), ensure_ascii=False),
            "基线标签": BASELINE_TAGS.get(rid, []),
            "状态": status,
            "判定时间": to_ms(judged_at),
            "备注": INVALIDATED_RUNS.get(rid, ""),
        })

        jdir = os.path.join(d, "judged")
        jitems = {}
        grp_of = {}
        for cid, info in (meta.get("cases") or {}).items():
            if info.get("gold_chunk") is None:
                grp_of[cid] = "应拒答"
            elif info.get("retrieval_hit"):
                grp_of[cid] = "检索命中"
            else:
                grp_of[cid] = "检索漏检"
        for jf in sorted(glob.glob(os.path.join(jdir, "C*.json"))):
            with open(jf, encoding="utf-8") as f:
                j = json.load(f)
            dims = j.get("维度", {})
            ft = j.get("失败类型")
            cid = j.get("id", "")
            cat = j.get("category", "")
            # 拒绝维只在 edge/adversarial 计分。飞书图表是实时 rollup，没法按类别过滤，
            # 所以「不适用」写 null——让均值只落在真正计分的样本上，
            # 结果与 summary.json 的「维度均分」同源（单一数据源原则，见 docs/决策记录.md D10）。
            refuse = ((dims.get("拒绝是否恰当") or {}).get("分")
                      if cat in REFUSE_DIM_CATEGORIES else None)
            verdicts.append({
                "记录ID": f"{rid}-{cid}",
                "run_id": rid,
                "case_id": cid,
                "类别": cat,
                "模式": mode_label,
                "事实性": (dims.get("事实性") or {}).get("分"),
                "表述与可落地": (dims.get("表述与可落地") or {}).get("分"),
                "格式遵循": (dims.get("格式遵循") or {}).get("分"),
                DIM_COL["拒绝是否恰当"]: refuse,
                "失败类型": FAIL_TYPE_LABEL.get(ft, "无") if ft else "无",
                "结论": j.get("结论", ""),
                "失败类": j.get("失败类", ""),
                "总分": j.get("总分"),
                "通过线": j.get("通过线"),
                "证据": j.get("证据", ""),
                "rubric版本": j.get("rubric", ""),
                "归因组别": grp_of.get(cid, "无"),
            })
            jitems[cid] = j

        # 失败归因：仅闭卷轮（meta 有 retrieval 信息）
        if meta.get("retrieval") and meta.get("cases"):
            groups = {"检索命中": [], "检索漏检": [], "应拒答": []}
            for cid, info in meta["cases"].items():
                if info.get("gold_chunk") is None:
                    groups["应拒答"].append(cid)
                elif info.get("retrieval_hit"):
                    groups["检索命中"].append(cid)
                else:
                    groups["检索漏检"].append(cid)
            for g, cids in groups.items():
                fails = [c for c in cids if (jitems.get(c) or {}).get("结论") == "失败"]
                attributions.append({
                    "轮次": rid,
                    "组别": g,
                    "case数": len(cids),
                    "失败数": len(fails),
                    "失败case": ", ".join(fails) if fails else "—",
                    "结论": f"{len(fails)}/{len(cids)} 失败",
                })
    return runs, verdicts, attributions


def ensure_tables(fs):
    r = fs.get(f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables")
    existing = {t["name"]: t["table_id"] for t in r["data"]["items"]}
    for tb in TABLES:
        if tb["name"] in existing:
            print(f"[表] {tb['name']} 已存在，跳过创建")
            continue
        r = fs.post(f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables", {"table": tb})
        tid = r["data"]["table_id"]
        existing[tb["name"]] = tid
        print(f"[表] {tb['name']} -> {tid}")
    return existing


def fill_attribution_group(fs, table_id, verdicts):
    """回填判定明细的归因组别（更新既有记录，幂等：值相同则跳过）。"""
    want = {v["记录ID"]: v.get("归因组别", "无") for v in verdicts}
    recs, page_token = {}, ""
    while True:
        params = f"?page_size=200" + (f"&page_token={page_token}" if page_token else "")
        r = fs.get(f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records", params=params)
        if r.get("code") != 0:
            sys.exit(f"[错误] 读表失败: {json.dumps(r, ensure_ascii=False)[:300]}")
        d = r["data"]
        for it in d.get("items") or []:
            key = str(it["fields"].get("记录ID", ""))
            if key in want:
                recs[key] = (it["record_id"], str(it["fields"].get("归因组别", "")))
        if not d.get("has_more"):
            break
        page_token = d.get("page_token", "")
    updates = [{"record_id": rid, "fields": {"归因组别": want[k]}}
               for k, (rid, cur) in recs.items() if cur != want[k]]
    updated = 0
    for i in range(0, len(updates), 200):
        chunk = updates[i:i + 200]
        r = fs.post(
            f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records/batch_update",
            {"records": chunk})
        if r.get("code") != 0:
            sys.exit(f"[错误] 批量更新失败: {json.dumps(r, ensure_ascii=False)[:400]}")
        updated += len(chunk)
    return updated, len(want) - updated


def resync_verdicts(fs, table_id, verdicts):
    """把判定明细里**已存在**的记录刷成当前口径（幂等：值相同则跳过）。

    为什么需要它：`batch_create` 只跳过已存在的键、**不会更新**。所以 `qc_engine --recompute`
    改了计分范围之后，重跑 `sync_runs` 其实是个空操作——飞书侧会一直停在旧口径。
    （2026-09-12 实测：明细表里 normal 类别还带着「拒绝是否恰当」的分，
      导致任何按该列做的均值都会算成重算前的旧值。）
    """
    want = {v["记录ID"]: v for v in verdicts}
    recs, page_token = {}, ""
    while True:
        params = f"?page_size=200" + (f"&page_token={page_token}" if page_token else "")
        r = fs.get(f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records", params=params)
        if r.get("code") != 0:
            sys.exit(f"[错误] 读表失败: {json.dumps(r, ensure_ascii=False)[:300]}")
        d = r["data"]
        for it in d.get("items") or []:
            key = str(it["fields"].get("记录ID", ""))
            if key in want:
                recs[key] = (it["record_id"], it["fields"])
        if not d.get("has_more"):
            break
        page_token = d.get("page_token", "")

    def _plain(v):
        if isinstance(v, list):
            return "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in v)
        return v

    updates = []
    for key, (rid, cur) in recs.items():
        diff = {}
        for fld in RESYNC_FIELDS:
            new, old = want[key].get(fld), _plain(cur.get(fld))
            if isinstance(new, (int, float)) and isinstance(old, (int, float)):
                if abs(new - old) < 1e-9:
                    continue
            elif old == new or (old in (None, "") and new in (None, "")):
                continue
            diff[fld] = new
        if diff:
            updates.append({"record_id": rid, "fields": diff})

    for i in range(0, len(updates), 200):
        r = fs.post(
            f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records/batch_update",
            {"records": updates[i:i + 200]})
        if r.get("code") != 0:
            sys.exit(f"[错误] 批量更新失败: {json.dumps(r, ensure_ascii=False)[:400]}")
    return len(updates), len(recs) - len(updates)


def main():
    env = load_env()
    fs = Feishu(env["FEISHU_APP_ID"], env["FEISHU_APP_SECRET"])
    print("[1] token OK")

    tables = ensure_tables(fs)
    runs, verdicts, attributions = parse_runs_and_verdicts()
    cases = parse_cases()

    # rubric 版本表（静态；日期转 unix 毫秒）
    rub_rows = [dict(r, 日期=to_ms(r["日期"])) for r in RUBRIC_VERSIONS]
    c, s = batch_create(fs, tables["rubric版本"], rub_rows,
                        key_field="版本", existing=load_keys(fs, tables["rubric版本"], "版本"))
    print(f"[2] rubric版本: 新建 {c} / 跳过 {s}（共 {len(RUBRIC_VERSIONS)}）")

    c, s = batch_create(fs, tables["case库"], cases,
                        key_field="case_id", existing=load_keys(fs, tables["case库"], "case_id"))
    print(f"[3] case库: 新建 {c} / 跳过 {s}（共 {len(cases)}）")

    c, s = batch_create(fs, tables["评测轮次"], runs,
                        key_field="run_id", existing=load_keys(fs, tables["评测轮次"], "run_id"))
    print(f"[4] 评测轮次: 新建 {c} / 跳过 {s}（共 {len(runs)}）")

    c, s = batch_create(fs, tables["case判定明细"], verdicts,
                        key_field="记录ID", existing=load_keys(fs, tables["case判定明细"], "记录ID"))
    print(f"[5] case判定明细: 新建 {c} / 跳过 {s}（共 {len(verdicts)}）")

    # 刷已存在的记录：batch_create 不会更新，口径变了必须走这里
    u, kept = resync_verdicts(fs, tables["case判定明细"], verdicts)
    print(f"[5a] case判定明细重同步: 更新 {u} / 已一致 {kept}")

    u, kept = fill_attribution_group(fs, tables["case判定明细"], verdicts)
    print(f"[5b] 归因组别回填: 更新 {u} / 已一致 {kept}")

    existing_pairs = load_pairs(fs, tables["失败归因"], "轮次", "组别")
    missing = [{"fields": row} for row in attributions
               if (row["轮次"], row["组别"]) not in existing_pairs]
    created = 0
    for i in range(0, len(missing), 200):
        chunk = missing[i:i + 200]
        r = fs.post(f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{tables['失败归因']}/records/batch_create",
                    {"records": chunk})
        if r.get("code") != 0:
            sys.exit(f"[错误] 批量写入失败: {json.dumps(r, ensure_ascii=False)[:400]}")
        created += len(chunk)
    print(f"[6] 失败归因: 新建 {created} / 跳过 {len(attributions) - created}（共 {len(attributions)}）")

    # 汇总与校验
    print()
    print("=== 汇总 ===")
    for name, tid in tables.items():
        r = fs.get(f"/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{tid}/records",
                   params="?page_size=1")
        total = r["data"].get("total", 0)
        print(f"  {name}: {total} 条")
    r = fs.get(f"/open-apis/bitable/v1/apps/{APP_TOKEN}")
    print(f"  base 链接: {r['data']['app'].get('url', '')}")


if __name__ == "__main__":
    main()
