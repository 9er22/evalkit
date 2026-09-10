#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""闭卷模式 runner（T004）——模拟真实 RAG：只给模型"检索到的片段"，不给全文。

为什么要有这一轮：开卷轮（整份 5 万字语料注入）跑出 30/30 全通过，事实性满分——
区分度为 0，因为答案就在眼前，失败类型 B（语料有但没检索到）物理上不可能发生。
闭卷轮把"检索"这一环补上：每个 case 只注入 top-k 个相关片段 + n 个干扰片段。

用法：
    python src/run_closed_book.py --dry-run              # 只看检索命中率，不调 API
    python src/run_closed_book.py                        # 跑全量（默认 top-k 3 + noise 3）
    python src/run_closed_book.py --top-k 2 --noise 4    # 调检索强度
    python src/run_closed_book.py --only C001 C019       # 只跑指定 case

产物：runs/<时间戳>/（同 run_eval 的结构，meta.json 里 mode=closed-book）
"""

import argparse
import json
import math
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from run_eval import (call_deepseek, load_env, resolve_model, sha256, die,
                      get_api_key, resolve, PROVIDERS,
                      CORPUS, PROMPT_TPL, CASES_DIR, RUNS_DIR, ROOT,
                      load_system_prompt_template, parse_case, ApiError)


# ----------------------------- 切分语料为检索单元 -----------------------------

def split_chunks(corpus_text: str) -> list:
    """按「来源文件 + ## 小节」切成检索单元。

    不用定长切块：小节是人写的语义单元，跨小节切会切断语境，
    那样产生的失败是"切块的锅"，不是"检索的锅"。
    """
    # 先按来源文件切，再按 ## 小节切
    parts = re.split(r"<!-- ===== 来源: (.+?) ===== -->", corpus_text)
    chunks = []
    # parts: [前言, 文件名1, 内容1, 文件名2, 内容2, ...]
    for i in range(1, len(parts), 2):
        src = parts[i].strip()
        body = parts[i + 1]
        subs = re.split(r"\n(?=## )", body)
        for si, s in enumerate(subs):
            title = ""
            m = re.match(r"##\s+(.+?)\s*\{", s) or re.match(r"##\s+(.+)", s)
            if m:
                title = re.sub(r"[^A-Za-z0-9]+", "-", m.group(1)).strip("-").lower()[:40]
            if len(s.strip()) < 200:  # 太短的并到上一块
                if chunks:
                    chunks[-1]["text"] += "\n" + s
                    continue
            chunks.append({
                "id": f"{src.split('/')[-1]}#{si:02d}-{title or 'head'}",
                "source": src,
                "text": s.strip(),
            })
    return chunks


def tokenize(text: str) -> list:
    """中英文混排的粗粒度分词：英文取标识符/单词，中文取字二元组。"""
    toks = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text.lower())
    zh = re.findall(r"[\u4e00-\u9fff]", text)
    toks += [zh[i] + zh[i + 1] for i in range(len(zh) - 1)]
    return toks


class Retriever:
    """词袋 + idf 的朴素检索器。够差——这正是我们要的。"""

    def __init__(self, chunks):
        self.chunks = chunks
        self.tok = [set(tokenize(c["text"])) for c in chunks]
        self.df = {}
        for s in self.tok:
            for t in s:
                self.df[t] = self.df.get(t, 0) + 1
        self.n = len(chunks)

    def search(self, query: str, top_k: int) -> list:
        q = set(tokenize(query))
        scored = []
        for i, s in enumerate(self.tok):
            score = sum(math.log(self.n / (1 + self.df[t])) for t in (q & s))
            if score > 0:
                scored.append((score, i))
        scored.sort(reverse=True)
        return [(i, round(sc, 3)) for sc, i in scored[:top_k]]


# ----------------------------- case 的语料锚点 -----------------------------

def gold_anchor(path: Path) -> str | None:
    """从 case 的「语料依据」里取金标锚点。写"不存在"的返回 None。"""
    text = path.read_text(encoding="utf-8")
    m = re.search(r"##\s*语料依据\s*\n(.*?)(\n##|\Z)", text, re.S)
    if not m:
        return None
    body = m.group(1)
    if "不存在" in body:
        return None
    quotes = re.findall(r"`([^`]+)`", body)
    if not quotes:
        return None
    # 取最长的那条作为锚点，短的可能到处都有
    return max(quotes, key=len).strip()


def find_gold_chunk(chunks, anchor: str | None):
    if not anchor:
        return None
    for c in chunks:
        if anchor in c["text"]:
            return c["id"]
    return "MISSING"  # 锚点在语料里找不到——数据问题，要报出来


# ----------------------------- 主流程 -----------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只输出检索统计，不调 API")
    ap.add_argument("--top-k", type=int, default=3, help="注入的相关片段数")
    ap.add_argument("--noise", type=int, default=3, help="注入的干扰片段数")
    ap.add_argument("--seed", type=int, default=42, help="干扰片段抽样的随机种子，固定以保证可复现")
    ap.add_argument("--only", nargs="+")
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--provider", choices=list(PROVIDERS), default=None,
                    help="供应商：ark（火山方舟 Coding Plan）/ deepseek（官方）")
    ap.add_argument("--model", default=None,
                    help="模型名，可带供应商前缀如 ark:kimi-k3")
    args = ap.parse_args()

    corpus_text = CORPUS.read_text(encoding="utf-8")
    chunks = split_chunks(corpus_text)
    retr = Retriever(chunks)
    tpl = load_system_prompt_template()

    case_files = sorted(CASES_DIR.glob("C0*.md"))
    if args.only:
        want = {t if t.startswith("C") else f"C{t}" for t in args.only}
        case_files = [f for f in case_files if f.stem in want]

    print(f"语料切成 {len(chunks)} 个检索单元 | top-k {args.top_k} + noise {args.noise} "
          f"| seed {args.seed}")
    print("-" * 60)

    plan = []
    for f in case_files:
        c = parse_case(f)
        anchor = gold_anchor(f)
        gold = find_gold_chunk(chunks, anchor)
        hits = retr.search(c["question"], args.top_k)
        picked = [i for i, _ in hits]
        rng = random.Random(f"{args.seed}-{c['id']}")
        rest = [i for i in range(len(chunks)) if i not in picked]
        noise_idx = rng.sample(rest, min(args.noise, len(rest)))
        hit = gold in [chunks[i]["id"] for i in picked]
        status = "（本就该在语料里找不到）" if gold is None else ("命中" if hit else "**漏检**")
        print(f"{c['id']} [{c['category']}] 检索top1 {chunks[hits[0][0]]['id'][:34] if hits else '(无)':34} "
              f"| 金标 {str(gold)[:28]:28} {status}")
        plan.append({"case": c, "gold": gold, "picked": picked, "noise_idx": noise_idx,
                     "hit": hit if gold else None, "anchor": anchor, "hits": hits})

    gold_exist = [p for p in plan if p["gold"] and p["gold"] != "MISSING"]
    n_hit = sum(1 for p in gold_exist if p["hit"])
    print("-" * 60)
    print(f"检索命中率 {n_hit}/{len(gold_exist)}"
          + (f"（{n_hit/len(gold_exist)*100:.0f}%）" if gold_exist else "")
          + f" | 语料中本就不存在的 case {sum(1 for p in plan if p['gold'] is None)} 个")
    missing = [p["case"]["id"] for p in plan if p["gold"] == "MISSING"]
    if missing:
        print(f"⚠ 锚点在语料里找不到的 case（数据问题）：{missing}")

    if args.dry_run:
        print("\ndry-run 结束，未调用 API。")
        return

    env = load_env()
    provider_arg = args.provider or (env.get("EVAL_PROVIDER") or "").strip() or None
    model_alias = (args.model or env.get("EVAL_MODEL")
                   or env.get("DEEPSEEK_MODEL") or "deepseek-v4-flash")
    provider, model = resolve(model_alias, provider_arg)
    api_key = get_api_key(env, provider)
    thinking = env.get("EVAL_THINKING", "disabled")
    temperature = float(env.get("EVAL_TEMPERATURE", "0"))

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS_DIR / ts
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "closed-book",
        "provider": provider, "provider_label": PROVIDERS[provider]["label"],
        "model": model, "thinking": thinking, "temperature": temperature,
        "corpus": {"path": str(CORPUS.relative_to(ROOT)),
                   "sha256": sha256(corpus_text)[:12], "chars": len(corpus_text),
                   "note": "整份语料不注入；只注入检索片段（见 retrieval）"},
        "system_prompt": {"path": str(PROMPT_TPL.relative_to(ROOT)),
                          "sha256": sha256(tpl)},
        "retrieval": {
            "method": "词袋 + idf（src/run_closed_book.py Retriever）",
            "top_k": args.top_k, "noise": args.noise, "seed": args.seed,
            "chunk_count": len(chunks),
            "chunk_sha256": sha256(json.dumps([c["id"] for c in chunks]))[:12],
            "query_used": "case 的问题原文（不用期望答案检索，避免作弊）",
        },
        "cases": {}, "results": [],
    }

    print(f"\n本轮 {len(plan)} 个 case → {run_dir.relative_to(ROOT)}")
    ok = fail = 0
    for p in plan:
        c = p["case"]
        idxs = p["picked"] + p["noise_idx"]
        rng = random.Random(f"{args.seed}-{c['id']}-order")
        rng.shuffle(idxs)  # 打乱顺序，别让相关片段总排在最前
        injected = "\n\n---\n\n".join(
            f"[片段 {i+1}/{len(idxs)} | 来源 {chunks[j]['source']}]\n{chunks[j]['text']}"
            for i, j in enumerate(idxs))
        system = tpl.replace("{CORPUS}", injected).replace("{QUESTION}", c["question"])

        try:
            r = call_deepseek(api_key, system, c["question"], model, temperature,
                              thinking=thinking, provider=provider)
        except ApiError as e:
            print(f"FAIL {c['id']} {str(e)[:150]}")
            fail += 1
            continue

        meta["cases"][c["id"]] = {
            "file": c["path"], "sha256": c["file_hash"], "category": c["category"],
            "gold_chunk": p["gold"], "retrieval_hit": p["hit"],
            "injected_chunks": [chunks[j]["id"] for j in idxs],
            "injected_chars": len(injected),
        }
        record = {
            "id": c["id"], "category": c["category"],
            "question": c["question"], "answer": r["answer"],
            "case_sha256": c["file_hash"],
            "injected_ctx_sha256": sha256(injected),
            "gold_chunk": p["gold"], "retrieval_hit": p["hit"],
            "injected_chunks": [chunks[j]["id"] for j in idxs],
            "model": model, "temperature": temperature, "thinking": thinking,
            "finish_reason": r["finish_reason"],
            "prompt_tokens": r["prompt_tokens"],
            "completion_tokens": r["completion_tokens"],
            "elapsed_sec": r["elapsed_sec"],
        }
        (raw_dir / f"{c['id']}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        md = (f"# {c['id']}（{c['category']}）· 闭卷\n\n"
              f"**注入片段** {', '.join(chunks[j]['id'] for j in idxs)}\n\n"
              f"**检索是否命中金标** {'是' if p['hit'] else ('本就无金标' if p['gold'] is None else '**否**')}\n\n"
              f"**问** {c['question']}\n\n**答**\n\n{r['answer']}\n\n---\n\n"
              f"- 模型 {model} | thinking {thinking} | temperature {temperature}\n"
              f"- 注入 {len(injected):,} 字符 | token 入 {r['prompt_tokens']} / 出 {r['completion_tokens']}\n")
        (raw_dir / f"{c['id']}.md").write_text(md, encoding="utf-8")
        ok += 1
        print(f"OK   {c['id']} [{c['category']}] 注入{len(injected):,}字符 "
              f"入{r['prompt_tokens']}/出{r['completion_tokens']}")
        time.sleep(args.sleep)

    meta["results"] = [{"id": k, "retrieval_hit": v["retrieval_hit"]}
                       for k, v in meta["cases"].items()]
    meta["summary"] = {"ok": ok, "fail": fail,
                       "retrieval_hit_rate": round(n_hit / len(gold_exist), 3) if gold_exist else None}
    (run_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print("-" * 60)
    print(f"完成：成功 {ok}，失败 {fail} | 产物 {run_dir.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
