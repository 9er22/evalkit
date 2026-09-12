# -*- coding: utf-8 -*-
"""一致性自检：扫"关键数字的手抄处"，跟唯一数据源 `runs/<轮次>/judged/summary.json` 比对。

为什么需要它（决策 D10）：同一组数字被抄进报告、展示页、飞书三处，靠人记必漏。
2026-09-12 一天内就连撞两次——展示页用着 D9 重算前的旧值；飞书明细表停在旧口径。
本脚本把"抄错了"变成一条命令就能查出来的事。

判据：目标文件里**必须出现**当前口径的数字（从 summary.json 现算，不写死）。
      缺哪个就报哪个——一个值缺失，等于那份文档还停在旧口径。

用法：
    python src/check_consistency.py            # 只查本地文件（离线）
    python src/check_consistency.py --feishu   # 顺带核对飞书判定明细的投影
退出码：0 = 全部一致；1 = 有不一致（已知未修项会在报告里单独标注）。
"""
import argparse
import glob
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 唯一数据源：只有这两轮有效（见 runs/轮次台账.md）
ROUNDS = {"20260908-202726": "开卷", "20260909-173030": "闭卷"}
DIMS = ["事实性", "表述与可落地", "格式遵循", "拒绝是否恰当"]

# 每个目标文件必须出现的数字组
#   dims     = 8 个维度均分（4 维 × 2 轮）
#   counts   = 各维实际计分条数（形如 "22 / 21"）
#   passrate = 通过数 / 通过率
TARGETS = {
    "reports/评测报告-R001-20260910.md": ["dims", "counts", "passrate"],
    "reports/showcase/index.html": ["dims", "passrate"],
}

# 已知未修项（有工单、有理由，不算"新问题"，但仍逐条打印）
KNOWN = {
    "reports/showcase/index.html":
        "已知未修：拒绝维口径待批 A（适用范围复审）落地后，与报告 §3.2/§4.5 一次同步。"
        "对应飞书质检记录 QC-T005-展示页（结论：打回）。"
        "\n          ⚠ 数字会随批 A 自动变对，但展示页的结论整句不会——"
        "「拒绝维降得最多」是方法错误（四维计分样本数不同，30/30/22/12 与 30/30/21/12，"
        "跌幅不可横向比较），不是口径错误，必须人工重写。"
        "见 docs/批A-落地清单.md 第 7 条。",
}


def load_source():
    """读唯一数据源，返回期望出现的字符串集合。"""
    dims, counts, passrate = set(), set(), set()
    for rid, label in ROUNDS.items():
        p = ROOT / "runs" / rid / "judged" / "summary.json"
        if not p.exists():
            sys.exit(f"[错误] 缺数据源：{p}")
        s = json.loads(p.read_text(encoding="utf-8"))
        for d in DIMS:
            dims.add(f"{s['维度均分'][d]:.2f}")
        for d in DIMS:
            counts.add(str(s["维度计分条数"][d]))
        passrate.add(f"{s['通过']}/{s['总数']}")
        # 文档里 100% 写作 "100%"，76.7% 写作 "76.7%"——:g 两种都能对上
        passrate.add(f"{s['通过率'] * 100:g}%")
    # 计分条数是"22 / 21"这种成对写法，单独合成
    pairs = set()
    for d in DIMS:
        a = json.loads((ROOT / "runs" / "20260908-202726" / "judged" / "summary.json")
                       .read_text(encoding="utf-8"))["维度计分条数"][d]
        b = json.loads((ROOT / "runs" / "20260909-173030" / "judged" / "summary.json")
                       .read_text(encoding="utf-8"))["维度计分条数"][d]
        if a != b:
            pairs.add(f"{a} / {b}")
    return dims, counts, pairs, passrate


def check_file(rel, groups, dims, counts, pairs, passrate):
    path = ROOT / rel
    if not path.exists():
        return [f"文件不存在：{rel}"]
    text = path.read_text(encoding="utf-8")
    missing = []
    if "dims" in groups:
        missing += sorted(v for v in dims if v not in text)
    if "counts" in groups:
        missing += sorted(v for v in (counts | pairs) if v not in text)
    if "passrate" in groups:
        missing += sorted(v for v in passrate if v not in text)
    return missing


def check_feishu(dims):
    """核对飞书判定明细：按当前口径投影出来的均值，是否等于 summary.json。"""
    sys.path.insert(0, str(ROOT / "src"))
    from feishu_setup import Feishu, load_env

    env = load_env()
    fs = Feishu(env["FEISHU_APP_ID"], env["FEISHU_APP_SECRET"])
    tid = "tbl0prsMXjKgsfUg"  # case判定明细

    recs, token = [], ""
    while True:
        params = "?page_size=500" + (f"&page_token={token}" if token else "")
        d = (fs.get(f"/open-apis/bitable/v1/apps/"
                    f"FjmKb4l8QavWcNsOTcLct71Znhg/tables/{tid}/records",
                    params=params).get("data") or {})
        recs.extend(d.get("items") or [])
        if not d.get("has_more"):
            break
        token = d.get("page_token", "")

    bad = []
    for rid in ROUNDS:
        s = json.loads((ROOT / "runs" / rid / "judged" / "summary.json").read_text(encoding="utf-8"))
        rows = [r["fields"] for r in recs if str(r["fields"].get("run_id")) == rid]
        for d in DIMS:
            vals = [r[d] for r in rows if isinstance(r.get(d), (int, float))]
            if not vals:
                continue
            got = sum(vals) / len(vals)
            want = s["维度均分"][d]
            if abs(got - want) > 0.005:
                bad.append(f"飞书 {rid} {d}: 投影均值 {got:.2f} ≠ 源 {want:.2f}（n={len(vals)}）")
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feishu", action="store_true", help="顺带核对飞书判定明细")
    args = ap.parse_args()

    dims, counts, pairs, passrate = load_source()
    print("唯一数据源：runs/<轮次>/judged/summary.json")
    print(f"  期望维度均分：{sorted(dims)}")
    print(f"  期望计分条数：{sorted(counts)}  成对写法：{sorted(pairs)}")
    print(f"  期望通过情况：{sorted(passrate)}\n")

    hard_fail = 0
    for rel, groups in TARGETS.items():
        missing = check_file(rel, groups, dims, counts, pairs, passrate)
        if not missing:
            print(f"  一致    {rel}")
            continue
        known = rel in KNOWN
        print(f"  {'已知项' if known else '不一致'}  {rel}  —— 缺 {len(missing)} 个值：{missing}")
        if known:
            print(f"          ↳ {KNOWN[rel]}")
        else:
            hard_fail += 1

    if args.feishu:
        print("\n飞书判定明细投影核对：")
        bad = check_feishu(dims)
        if bad:
            hard_fail += 1
            for b in bad:
                print("  不一致  " + b)
        else:
            print("  一致    明细表投影均值与 summary.json 相同")

    print()
    if hard_fail:
        print(f"✗ 有 {hard_fail} 处不一致，需要处理。")
        return 1
    print("✓ 全部一致（已知未修项除外，见上）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
