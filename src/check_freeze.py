"""冻结指纹核对（2026-09-12 外部审阅窗口，2026-09-13 解除）。

外部审阅者拿到的是 rubric / 报告的一份快照。冻结期内若受审文件被改动，
送审包即失效、审阅结果作废。本脚本把受审文件的当前指纹与冻结时的登记值
对比，一眼看出有没有人动过。

⚠ 冻结已于 2026-09-13 解除（见 `docs/冻结-外部审阅结束-20260912.md`）。
下方 FROZEN 表是**冻结期的证据**，登记值（如 `6fcedc045304`）**永不更新**——
解除后 rubric 已 bump v1.5，本脚本改报"冻结已解除"、不再校验指纹。
v1.5 之后的新指纹登记在 `reports/交叉验证-R001-20260913.md`。

用法：python src/check_freeze.py
退出码：0 = 全部一致（或已解除冻结）；1 = 有文件被改动。
"""
import hashlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# ⚠ 这是"冻结进行中"的哨兵文件，**不是**当前文档路径。
# 2026-09-13 解除冻结时，文档已改名为 `冻结-外部审阅结束-20260912.md`（留痕不删）。
# 本常量刻意**不跟着改名**：找不到它 ＝ 冻结已解除。若"修正"成新文件名，本脚本会永远处于冻结态。
FREEZE_DOC = ROOT / "docs" / "冻结-外部审阅中-20260912.md"

# 冻结时登记（2026-09-12）。不写 commit 哈希：重写历史会让它失效，
# 而指纹算的是文件内容，与哈希无关——这才是本脚本能在重写后仍然有效的原因。
# 需要 HEAD 就用 `git log`，commit 哈希的唯一源是 git 本身（决策 D10）。
FROZEN = {
    "rubrics/评分标准.md": "bf8d9849cbfa",
    "rubrics/_judge-prompt.md": "6fcedc045304",
    "rubrics/修改记录.md": "6c7ba2107efb",
    "reports/评测报告-R001-20260910.md": "c14c392e8831",
    "src/qc_engine.py": "d3bd8b6b7ffa",
    "docs/交叉验证-发给审阅Agent.md": "84bc028d8bc9",
}


def sha12(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


def main() -> int:
    if not FREEZE_DOC.exists():
        print("冻结已解除（找不到冻结说明文档），无需核对。")
        return 0

    print(f"冻结中 · 说明见 {FREEZE_DOC.relative_to(ROOT)}\n")
    bad = []
    for rel, want in FROZEN.items():
        p = ROOT / rel
        if not p.exists():
            print(f"  缺失    {rel}（登记 {want}）")
            bad.append(rel)
            continue
        got = sha12(p)
        mark = "一致" if got == want else "已改动"
        if got != want:
            bad.append(rel)
        print(f"  {mark}    {rel}   {want} -> {got}")

    print()
    if bad:
        print(f"⚠ {len(bad)} 个文件与冻结登记不一致：")
        for rel in bad:
            print(f"    - {rel}")
        print("\n处置：先查清是谁改的、为什么改、影不影响审阅结论，再决定该轮审阅结果是否可用。")
        return 1
    print("全部一致：送审版本未被改动，审阅结果可回填。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
