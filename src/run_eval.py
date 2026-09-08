#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""EvalKit runner —— 把固定语料注入 prompt，逐个跑 case，落盘原始输出。

零第三方依赖（只用标准库），这样换机器不用装包。

用法：
    python src/run_eval.py --smoke              # 只跑 C001，验证链路
    python src/run_eval.py --only C001 C019     # 跑指定 case
    python src/run_eval.py                      # 全量跑 cases/C0*.md

产物：
    runs/<时间戳>/
        meta.json         本轮的"标准版本"快照（语料/提示/case 的 hash）
        raw/CXXX.json     结构化：输入、输出、token 用量、耗时
        raw/CXXX.md       人可读：一眼能看完一问一答
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "research" / "corpus" / "CORPUS.md"
PROMPT_TPL = ROOT / "cases" / "_system-prompt.md"
CASES_DIR = ROOT / "cases"
RUNS_DIR = ROOT / "runs"

CORPUS_SHA = "50113da16fec53b66b80d75e80a89296de4fa5a5"
API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"


# ----------------------------- 基础工具 -----------------------------

def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_env() -> dict:
    """读 .env。不装 python-dotenv，手写解析够用。"""
    env = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def die(msg: str):
    print(f"错误：{msg}", file=sys.stderr)
    sys.exit(1)


# ----------------------------- 解析 -----------------------------

def load_system_prompt_template() -> str:
    """从 cases/_system-prompt.md 抽出 ``` 块里的模板。

    不从代码里硬编码，是因为：提示改了而代码没改，跑出来的结果就对不上账。
    """
    text = PROMPT_TPL.read_text(encoding="utf-8")
    m = re.search(r"```\n(.*?)\n```", text, re.S)
    if not m:
        die(f"在 {PROMPT_TPL} 里找不到 ``` 代码块形式的系统提示模板")
    return m.group(1)


def parse_case(path: Path) -> dict:
    """解析一个 case 文件：frontmatter + 输入 + 元信息。"""
    path = Path(path).resolve()
    text = path.read_text(encoding="utf-8")

    fm = {}
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip()
        body = m.group(2)
    else:
        body = text

    q = re.search(r"##\s*输入\s*\n+```\n(.*?)\n```", body, re.S)
    if not q:
        die(f"{path.name} 里找不到「## 输入」下的代码块")

    return {
        "id": fm.get("id", path.stem),
        "category": fm.get("category", "?"),
        "question": q.group(1).strip(),
        "file_hash": sha256(text)[:12],
        "path": str(path.relative_to(ROOT)),
    }


# ----------------------------- 调用 -----------------------------

def call_deepseek(api_key: str, system: str, question: str,
                  model: str, temperature: float, timeout: int = 120) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ],
        "temperature": temperature,
        "stream": False,
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        die(f"API 返回 {e.code}：{detail}\n（401/402 通常是 key 错或余额不足）")
    except urllib.error.URLError as e:
        die(f"网络请求失败：{e.reason}")
    elapsed = time.time() - t0

    usage = data.get("usage", {})
    return {
        "answer": data["choices"][0]["message"]["content"],
        "finish_reason": data["choices"][0].get("finish_reason"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "elapsed_sec": round(elapsed, 2),
        "raw_id": data.get("id"),
    }


# ----------------------------- 主流程 -----------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="只跑 C001")
    ap.add_argument("--only", nargs="+", help="只跑指定 case，如 --only C001 C019")
    ap.add_argument("--sleep", type=float, default=1.0, help="case 之间的间隔秒数")
    args = ap.parse_args()

    env = load_env()
    api_key = env.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key or api_key == "sk-xxx":
        die("没读到 DEEPSEEK_API_KEY。先 cp .env.example .env 并填入真实 key。")
    model = env.get("DEEPSEEK_MODEL", DEFAULT_MODEL)
    temperature = float(env.get("EVAL_TEMPERATURE", "0"))

    if not CORPUS.exists():
        die(f"语料不存在：{CORPUS}\n先跑 python src/fetch_corpus.py 生成。")
    corpus = CORPUS.read_text(encoding="utf-8")
    tpl = load_system_prompt_template()

    if args.smoke:
        targets = ["C001"]
    elif args.only:
        targets = [t if t.startswith("C") else f"C{t}" for t in args.only]
    else:
        targets = None

    case_files = sorted(CASES_DIR.glob("C0*.md"))
    if targets:
        case_files = [f for f in case_files if f.stem in targets]
    if not case_files:
        die("没有匹配到任何 case 文件")

    # 本轮目录：时间戳 + 短 hash，避免覆盖历史轮次
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS_DIR / ts
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # meta.json —— 这一轮的"标准版本"快照。
    # rubric 里「标准版本可追溯」这一维，靠的就是这个文件：
    # 将来分数变了，能查清是语料/提示/case 哪一环动的。
    meta = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "temperature": temperature,
        "corpus": {"path": str(CORPUS.relative_to(ROOT)),
                   "sha": CORPUS_SHA, "sha256": sha256(corpus),
                   "chars": len(corpus)},
        "system_prompt": {"path": str(PROMPT_TPL.relative_to(ROOT)),
                          "sha256": sha256(tpl)},
        "cases": {},
        "results": [],
    }

    print(f"语料 {len(corpus):,} 字符 | 模型 {model} | temperature={temperature}")
    print(f"本轮 {len(case_files)} 个 case → {run_dir.relative_to(ROOT)}")
    print("-" * 60)

    ok = fail = 0
    for f in case_files:
        c = parse_case(f)
        meta["cases"][c["id"]] = {"file": c["path"], "sha256": c["file_hash"],
                                  "category": c["category"]}

        system = tpl.replace("{CORPUS}", corpus).replace("{QUESTION}", c["question"])
        try:
            r = call_deepseek(api_key, system, c["question"], model, temperature)
        except SystemExit:
            raise
        except Exception as e:  # 单个 case 失败不中断整轮
            print(f"FAIL {c['id']} {e}")
            fail += 1
            continue

        record = {
            "id": c["id"], "category": c["category"],
            "question": c["question"], "answer": r["answer"],
            "case_sha256": c["file_hash"],
            "corpus_sha256": sha256(corpus),
            "system_prompt_sha256": sha256(tpl),
            "model": model, "temperature": temperature,
            "finish_reason": r["finish_reason"],
            "prompt_tokens": r["prompt_tokens"],
            "completion_tokens": r["completion_tokens"],
            "elapsed_sec": r["elapsed_sec"],
            "raw_id": r["raw_id"],
        }
        (raw_dir / f"{c['id']}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        (raw_dir / f"{c['id']}.md").write_text(
            f"# {c['id']}（{c['category']}）\n\n"
            f"**问** {c['question']}\n\n"
            f"**答**\n\n{r['answer']}\n\n"
            f"---\n\n"
            f"- 模型 {model} | temperature {temperature}\n"
            f"- token 入 {r['prompt_tokens']} / 出 {r['completion_tokens']}"
            f" | 耗时 {r['elapsed_sec']}s\n"
            f"- case hash {c['file_hash']} | 语料 hash {sha256(corpus)[:12]}\n",
            encoding="utf-8")

        meta["results"].append({"id": c["id"],
                                "tokens": r["prompt_tokens"],
                                "elapsed": r["elapsed_sec"]})
        ok += 1
        print(f"OK   {c['id']} [{c['category']}] "
              f"入{r['prompt_tokens']}/出{r['completion_tokens']} "
              f"{r['elapsed_sec']}s")
        time.sleep(args.sleep)

    meta["summary"] = {"ok": ok, "fail": fail,
                       "total_prompt_tokens": sum(x["tokens"] for x in meta["results"])}
    (run_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print("-" * 60)
    print(f"完成：成功 {ok}，失败 {fail}"
          f" | 累计输入 token {meta['summary']['total_prompt_tokens']:,}")
    print(f"产物：{run_dir.relative_to(ROOT)}")
    if args.smoke:
        print("\n冒烟通过。下一步：python src/run_eval.py 跑全量。")


if __name__ == "__main__":
    main()
