#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""对一轮跑测结果做机器初筛，找出"值得人去看"的那些 case。

不做评分（那是 rubric 的事），只做三件机器擅长的事：
1. 幻觉嫌疑：回答里出现 snake_case 标识符，但语料里没有 → 可能是编的 API
2. 弃权检测：回答是否说了"文档没有讲到"（对 edge/adversarial 是正确行为，对 normal 是失败）
3. 长度异常：过短可能不完整，过长可能冗余

用法：
    python src/analyze_runs.py                    # 分析最新一轮
    python src/analyze_runs.py runs/20260908-xxx  # 指定轮次
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "research" / "corpus" / "CORPUS.md"
RUNS = ROOT / "runs"
CASES = ROOT / "cases"

# 语料里真实存在的标识符，回答里出现的标识符必须在这里面
IDENT_RE = re.compile(r"\b[a-z_][a-z0-9_]{5,}\b")
# 中文回答里这些词不算 API
STOPWORDS = {
    "example", "examples", "default", "defaults", "optional", "required",
    "python", "fastapi", "pydantic", "import", "return", "string",
    "integer", "boolean", "parameter", "parameters", "function", "request",
    "response", "document", "documentation", "decorator", "decorators",
    "keyword", "keywords", "content", "message", "messages", "current",
    "different", "another", "instead", "because", "should", "would",
    "cannot", "without", "through", "between", "however", "therefore",
}


NEGATION_RE = re.compile(
    r"(没有讲到|没有提到|没有涉及|文档中?没有|不存在|并未|不是|不包含|未提及|不支持)")


def is_denied(answer: str, word: str, window: int = 40) -> bool:
    """判断一个标识符是否出现在否定语境里（"文档没有讲到 X"）。

    这种不是幻觉，恰恰是正确行为——但纯关键词匹配会把它们混为一谈。
    """
    for m in re.finditer(re.escape(word), answer):
        s = max(0, m.start() - window)
        ctx = answer[s:m.end() + window]
        if NEGATION_RE.search(ctx):
            return True
    return False


def corpus_idents() -> set:
    text = CORPUS.read_text(encoding="utf-8")
    return set(IDENT_RE.findall(text)) | STOPWORDS


def main():
    if len(sys.argv) > 1:
        run_dir = Path(sys.argv[1])
    else:
        dirs = sorted(RUNS.glob("*"))
        if not dirs:
            print("没有找到任何跑测结果")
            return
        run_dir = dirs[-1]

    known = corpus_idents()
    raw_dir = run_dir / "raw"
    files = sorted(raw_dir.glob("C*.json"))
    print(f"分析 {run_dir.name}，共 {len(files)} 个 case")
    print("=" * 70)

    suspicious = []
    abstained = []
    short_answers = []

    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        cid, cat, ans = d["id"], d["category"], d["answer"]

        # 1. 幻觉嫌疑
        #    关键：出现在否定语境里的不算幻觉。
        #    "文档没有讲到 min_length" 和 "你可以用 min_length 校验" 在词面上
        #    都出现了 min_length，但前者是正确弃权、后者才是编造。
        #    纯关键词匹配分不清这两者——这是自动判定的硬边界，rubric 必须处理。
        found = set(IDENT_RE.findall(ans))
        unknown = set()
        denied = set()
        for w in found:
            if w in known:
                continue
            if any(w.startswith(p) for p in ("http", "www_", "docs_")):
                continue
            if is_denied(ans, w):
                denied.add(w)
            else:
                unknown.add(w)

        # 2. 弃权检测
        gave_up = bool(re.search(r"没有讲到|没有提到|没有涉及|文档中没有|这份文档没有", ans))

        # 3. 长度
        n = len(ans)

        flags = []
        if unknown:
            flags.append(f"幻觉嫌疑:{','.join(sorted(unknown)[:4])}")
            suspicious.append((cid, cat, sorted(unknown)))
        if gave_up:
            flags.append("弃权")
            abstained.append((cid, cat))
        if n < 80:
            flags.append(f"过短({n}字)")
            short_answers.append((cid, cat, n))

        mark = "  ".join(flags) if flags else "—"
        print(f"{cid} [{cat:11s}] {n:4d}字  {mark}")

    print("=" * 70)
    print(f"幻觉嫌疑 {len(suspicious)} 个 | 弃权 {len(abstained)} 个 | 过短 {len(short_answers)} 个")
    print()
    print("分类统计：", dict(Counter(
        json.loads(f.read_text(encoding="utf-8"))["category"] for f in files)))

    if abstained:
        print()
        print("弃权的 case（对 edge/adversarial 通常是对的，对 normal 是失败）：")
        for cid, cat in abstained:
            verdict = "✓ 预期" if cat in ("edge", "adversarial") else "✗ 疑似失败"
            print(f"  {cid} [{cat}] {verdict}")


if __name__ == "__main__":
    main()
