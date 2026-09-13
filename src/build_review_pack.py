# -*- coding: utf-8 -*-
"""把审阅指令 + rubric 全文 + 报告全文 + 4 条 case 拼成单文件，供粘贴给外部审阅 Agent。

产出：`docs/交叉验证-发给审阅Agent.md`。

⚠️ **这个产出物是外部审阅的送审快照**，它的 sha256 被冻结文档
`docs/冻结-外部审阅结束-20260912.md` 记录下来（冻结期内为 `84bc028d8bc9`）。
**冻结期间重跑本脚本会改写快照、让审阅结果作废**，所以脚本自带闸门：冻结生效时拒绝运行，
除非显式 `--force`。

冻结已于 2026-09-13 解除，闸门现为开启状态。但仍**不要随意重跑**：快照本身是冻结期的
证据文件（6 个受审指纹之一），重跑会把它改写掉。要重跑先想清楚为什么。

用法：python src/build_review_pack.py [--force]
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FREEZE_DOC = ROOT / "docs" / "冻结-外部审阅结束-20260912.md"
DEST = ROOT / "docs" / "交叉验证-发给审阅Agent.md"
CASE_IDS = ["C019", "C022", "C026", "C029"]

INSTRUCTION = """你是一位独立的评测标准审阅者，没有参与过这份材料的任何环节。

我会给你两份材料：一份 LLM 输出评测的评分标准（R001），一份用这套标准写出来的评测报告。后面另附几条测试用例，供你在需要时参考标准是怎么落到具体题目上的。

你的任务不是打分，是挑毛病。请只回答下面四类问题：

1. 【歧义】这份标准里，哪一条档位描述可以被读成两种意思？
   逐条抄出原文，并写出两种读法分别是什么。

2. 【适用范围】有没有哪一维被用在了不适用它的题上？
   请重点看「拒绝是否恰当」这一维——标准里写它只对 edge / adversarial 适用。
   有没有哪一类题目在这一点上是模糊的？

3. 【缺口】有没有明显应该评、但标准里完全没有的方面？

4. 【跳跃】报告里的结论到证据之间，有没有逻辑跳跃？指到具体章节。

硬性要求：
- 每条必须指出具体的原文位置（哪一维、哪一档、或报告哪一节）。
- 不要提「建议扩大样本量」「建议引入人工复核」「建议补充更多 case」这类通用建议
  ——它们对任何评测都成立，等于没说。
- 宁可少提，不要凑数。提 2 条真问题，好过 10 条套话。
- 如果你认为某一部分没问题，直接说「这一部分我没找到问题」，不要硬找。

输出方式：
按上面四类分节列出，每一条写成三行——
  【问题】一句话说清问题是什么
  【原文位置】哪一维 / 哪一档 / 报告哪一节，并抄出关键原文
  【为什么是问题】说清会导致什么后果
某类如果确实没问题，写一句「这一类我没找到问题」即可。
"""


def freeze_active() -> bool:
    if not FREEZE_DOC.exists():
        return False
    txt = FREEZE_DOC.read_text(encoding="utf-8")
    return "状态：冻结中" in txt


def main():
    force = "--force" in sys.argv[1:]
    if freeze_active() and not force:
        sys.exit(
            "[拒绝] 外部审阅冻结生效中，重跑会改写送审快照、使审阅结果作废。\n"
            f"       受影响文件：{DEST.relative_to(ROOT)}\n"
            f"       确定要重写再跑：python {pathlib.Path(__file__).relative_to(ROOT)} --force"
        )

    rubric = (ROOT / "rubrics" / "评分标准.md").read_text(encoding="utf-8")
    report = (ROOT / "reports" / "评测报告-R001-20260910.md").read_text(encoding="utf-8")

    cases = []
    for cid in CASE_IDS:
        txt = (ROOT / "cases" / f"{cid}.md").read_text(encoding="utf-8")
        txt = re.sub(r"^---\n.*?\n---\n", "", txt, flags=re.S)  # 去 YAML frontmatter
        txt = re.split(r"##\s*我为什么关心", txt)[0].rstrip()   # 出题人视角，不给审阅者
        cases.append(txt.strip())
    cases_block = "\n\n---\n\n".join(cases)

    out = f"""{INSTRUCTION}

═══════════════════════════════════════════

# 材料一：评分标准 R001

{rubric}

═══════════════════════════════════════════

# 材料二：评测报告

{report}

═══════════════════════════════════════════

# 参考材料：测试用例节选

{cases_block}
"""

    DEST.write_text(out, encoding="utf-8")

    import hashlib
    digest = hashlib.sha256(out.encode("utf-8")).hexdigest()[:12]
    print("written:", DEST)
    print("chars:", len(out), "| sha256(前12):", digest)
    print("rubric chars:", len(rubric), "| report chars:", len(report),
          "| cases chars:", len(cases_block))


if __name__ == "__main__":
    main()
