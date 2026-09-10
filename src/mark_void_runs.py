# -*- coding: utf-8 -*-
"""给不进统计的轮次打 作废.md 标记，并生成 runs/轮次台账.md。

标记文件是机器可检查的：qc_engine 会跳过带 作废.md 的轮次，
避免"最新一轮"选到只有 1 条 case 的冒烟轮。
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"

# 状态 -> 原因
PLAN = {
    # --- 用户 2026-09-10 指示：只跑了 1 条 case，没跑完 ---
    "20260910-195358": ("不完整", "只跑了 C001 一条，未跑完、未判定。不得进入任何统计。"),
    "20260910-205028": ("不完整", "只跑了 C001 一条，未跑完、未判定。不得进入任何统计。"),
    # --- 决策记录 D7：模型次日失效、口径不可追溯 ---
    "20260909-143435": ("作废", "闭卷轮，模型 v4.1-flash-beta（次日失效），与开卷轮非同一模型，"
                                "两变量同时变，归因不成立。见 docs/决策记录.md D7。"),
    "20260909-143859": ("作废", "开卷对比轮，同上。见 docs/决策记录.md D7。"),
    "20260908-202433": ("作废", "开卷轮，模型 v4.1-flash-beta（次日失效，今天不可复现）。"
                                "同 D7 口径，且从未判定。"),
    # --- 废弃模型别名 ---
    "20260908-195301": ("作废", "模型为废弃别名 deepseek-chat（官方 2026-04-24 公告废弃），"
                                "且从未判定。"),
    # --- 冒烟/中断，从未判定 ---
    "20260908-195224": ("不完整", "冒烟测试，1 条 case，从未判定。"),
    "20260908-200338": ("不完整", "冒烟测试，1 条 case，从未判定。"),
    "20260908-200351": ("不完整", "冒烟测试，1 条 case，模型为已过期的 v4.1 内测名。"),
    "20260908-200407": ("不完整", "冒烟测试，1 条 case，从未判定。"),
    "20260908-203946": ("不完整", "冒烟测试，1 条 case，模型为已过期的 v4.1 内测名。"),
    "20260908-203951": ("不完整", "冒烟测试，1 条 case，模型为已过期的 v4.1 内测名。"),
    "20260908-202334": ("不完整", "跑到 20 条中断，缺 meta.json，从未判定。"),
}

VALID = {
    "20260908-202726": ("开卷（全文注入 5 万字语料）", "有效。R001 v1.4 全量判定，30/30。"),
    "20260909-173030": ("闭卷（top-3 检索片段 + 3 干扰片段）", "有效。R001 v1.4 全量判定，23/30。"),
}


def main():
    for rid, (status, reason) in PLAN.items():
        d = RUNS / rid
        if not d.exists():
            print(f"[跳过] {rid} 不存在")
            continue
        meta = {}
        p = d / "meta.json"
        if p.exists():
            meta = json.loads(p.read_text(encoding="utf-8"))
        n_raw = len(list((d / "raw").glob("C*.json"))) if (d / "raw").exists() else 0
        n_judged = len(list((d / "judged").glob("C*.json"))) if (d / "judged").exists() else 0
        txt = f"""# 本轮次{status}，不进入任何统计

- 轮次 ID：{rid}
- 状态：**{status}**
- 原因：{reason}
- 模型：`{meta.get('model', '（缺 meta.json）')}`
- 产出：raw {n_raw} 条 / judged {n_judged} 条（完整轮次应为 30 条）

**为什么保留而不是删掉**：删掉即销毁证据，别人无法复核"为什么它不算"。
留痕 + 明确标注，才有资格把它干净地排除在统计之外。

**机器可检查**：`src/qc_engine.py` 会跳过含本文件的轮次（含"取最新一轮"的默认行为）。
"""
        (d / "作废.md").write_text(txt, encoding="utf-8")
        print(f"[{status}] {rid}")

    # 台账
    rows = []
    for d in sorted(p for p in RUNS.iterdir() if p.is_dir()):
        meta = {}
        p = d / "meta.json"
        if p.exists():
            meta = json.loads(p.read_text(encoding="utf-8"))
        n_raw = len(list((d / "raw").glob("C*.json"))) if (d / "raw").exists() else 0
        n_judged = len(list((d / "judged").glob("C*.json"))) if (d / "judged").exists() else 0
        rid = d.name
        if rid in VALID:
            status, note = "**有效**", VALID[rid][1]
            cond = VALID[rid][0]
        else:
            status, reason = PLAN.get(rid, ("作废", "（未登记，按作废处理）"))
            note = reason
            cond = "闭卷" if meta.get("mode") == "closed-book" else "开卷"
        rows.append((rid, cond, meta.get("model", "（缺 meta.json）"),
                     f"{n_raw}/{n_judged}", status, note))

    lines = [
        "# 轮次台账",
        "",
        "**只有标注「有效」的轮次可以进入任何统计、图表或报告数字。**",
        "其余轮次在各自目录下有 `作废.md`（机器可检查，`qc_engine.py` 会跳过）。",
        "",
        "| 轮次 ID | 条件 | 模型 | raw/judged | 状态 | 说明 |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| `{r[0]}` | {r[1]} | `{r[2]}` | {r[3]} | {r[4]} | {r[5]} |")
    lines += [
        "",
        "## 规则",
        "",
        "1. 判定或统计前先查本表。带 `作废.md` 的轮次，`qc_engine` 会直接拒绝（除 `--force`）。",
        "2. 新作废一轮：在 `src/mark_void_runs.py` 的 `PLAN` 里登记并重跑，同时更新本表。",
        "3. **不删数据**。作废 = 留痕 + 排除，删除 = 销毁证据。",
    ]
    (RUNS / "轮次台账.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n台账已生成：{RUNS / '轮次台账.md'}")


if __name__ == "__main__":
    main()
