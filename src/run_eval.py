#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""EvalKit runner —— 把固定语料注入 prompt，逐个跑 case，落盘原始输出。

零第三方依赖（只用标准库），这样换机器不用装包。

用法：
    python src/run_eval.py --smoke              # 只跑 C001，验证链路
    python src/run_eval.py --only C001 C019     # 跑指定 case
    python src/run_eval.py                      # 全量跑 cases/C0*.md
    python src/run_eval.py --model v4.1-flash-beta   # 切模型（别名见 MODELS）
    python src/run_eval.py --thinking enabled --effort low   # 开思考模式

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

# 模型注册表（api-docs.deepseek.com，2026-09-08 核对）：
# 现役只有 deepseek-v4-flash / deepseek-v4-pro / deepseek-v4-flash-vision-exp 三个名字。
# 别名是为了在命令行和配置里少打字；填完整模型名也认。
# 注意：deepseek-chat / deepseek-reasoner 已于 2026-04-24 官方公告废弃
# （原定 2026-07-24 停用，实测 2026-09-08 仍可调通）——随时可能失效，评测别再依赖。
MODELS = {
    "v4-flash":        "deepseek-v4-flash",            # 默认。当前版本 DeepSeek-V4-Flash-0731
    "v4-pro":          "deepseek-v4-pro",              # 当前版本 DeepSeek-V4-Pro-0813，价格约 3 倍
    "v4-flash-vision": "deepseek-v4-flash-vision-exp", # 实验版多模态；纯文本能力与 v4-flash 相当
    # V4.1 Flash 限时内测（2026-09-08 官方群宣布，官方文档尚未收录）：
    # 新架构 + 原生多模态；计费同 v4-flash；单账号限 20 并发；
    # ⚠ 模型名自带过期时间，2026-09-10 后失效，届时删除此条即可。
    "v4.1-flash-beta": "deepseek-v4.1-flash-expires-on-0910",
    "chat":            "deepseek-chat",                # 废弃别名 = v4-flash 非思考模式
    "reasoner":        "deepseek-reasoner",            # 废弃别名 = v4-flash 思考模式
}
DEFAULT_MODEL = "v4-flash"
# v4 系默认开思考模式且忽略 temperature（官方文档明确）。
# 本项目要 temperature=0 的可复现性，所以默认显式关思考；要开用 --thinking enabled。
DEFAULT_THINKING = "disabled"


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

def resolve_model(name: str) -> str:
    """别名或完整模型名 → 完整模型名。"""
    m = (name or "").strip()
    if not m:
        die("模型名为空。用 --model v4-flash 或在 .env 里设 DEEPSEEK_MODEL。")
    if m in MODELS:
        return MODELS[m]
    if m in set(MODELS.values()):
        return m
    die(f"未知模型 `{m}`。可用别名：{', '.join(MODELS)}；或直接填完整模型名。")


def call_deepseek(api_key: str, system: str, question: str,
                  model: str, temperature: float,
                  thinking: str | None = None,
                  reasoning_effort: str | None = None,
                  timeout: int = 120) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ],
        "temperature": temperature,
        "stream": False,
    }
    # v4 系默认开思考模式且忽略 temperature。要 temperature 生效必须显式关思考。
    if thinking:
        payload["thinking"] = {"type": thinking}
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
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

    msg = data["choices"][0]["message"]
    usage = data.get("usage", {})
    # 思考模式下 usage 里会多 completion_tokens_details.reasoning_tokens
    reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    return {
        "answer": msg.get("content") or "",
        "reasoning": msg.get("reasoning_content") or "",
        "finish_reason": data["choices"][0].get("finish_reason"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": reasoning_tokens,
        "elapsed_sec": round(elapsed, 2),
        "raw_id": data.get("id"),
    }


# ----------------------------- 主流程 -----------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="只跑 C001，验证链路")
    ap.add_argument("--only", nargs="+", help="只跑指定 case，如 --only C001 C019")
    ap.add_argument("--sleep", type=float, default=1.0, help="case 之间的间隔秒数")
    ap.add_argument("--model", default=None,
                    help="模型别名或完整名（v4-flash / v4-pro / v4.1-flash-beta / ...），"
                         "覆盖 .env 的 DEEPSEEK_MODEL")
    ap.add_argument("--thinking", choices=["enabled", "disabled"], default=None,
                    help="思考模式开关（默认 disabled）。v4 系默认开思考且忽略 temperature")
    ap.add_argument("--effort", choices=["low", "high", "max"], default=None,
                    help="思考档位，仅 thinking=enabled 时生效")
    args = ap.parse_args()

    env = load_env()
    api_key = env.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key or api_key == "sk-xxx":
        die("没读到 DEEPSEEK_API_KEY。先 cp .env.example .env 并填入真实 key。")

    model_alias = args.model or env.get("DEEPSEEK_MODEL", "") or DEFAULT_MODEL
    model = resolve_model(model_alias)
    thinking = args.thinking or env.get("EVAL_THINKING", DEFAULT_THINKING)
    reasoning_effort = args.effort or env.get("EVAL_REASONING_EFFORT") or None
    if thinking == "disabled":
        reasoning_effort = None  # 关思考时档位无意义，不传
    temperature = float(env.get("EVAL_TEMPERATURE", "0"))
    if thinking == "enabled":
        # 官方文档：thinking 模式不支持 temperature/top_p 等参数，传了也不生效
        print(f"⚠ thinking=enabled：temperature({temperature}) 将被服务端忽略，"
              f"可复现性依赖模型自身稳定性")
        temperature_for_record = None  # 记录为 null，别假装它生效了
    else:
        temperature_for_record = temperature

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
    # 将来分数变了，能查清是语料/提示/case/模型配置哪一环动的。
    meta = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "model_alias": model_alias if model_alias != model else None,
        "thinking": thinking,
        "reasoning_effort": reasoning_effort,
        "temperature": temperature_for_record,
        "corpus": {"path": str(CORPUS.relative_to(ROOT)),
                   "sha": CORPUS_SHA, "sha256": sha256(corpus),
                   "chars": len(corpus)},
        "system_prompt": {"path": str(PROMPT_TPL.relative_to(ROOT)),
                          "sha256": sha256(tpl)},
        "cases": {},
        "results": [],
    }

    print(f"语料 {len(corpus):,} 字符 | 模型 {model} | "
          f"thinking={thinking}" + (f"/{reasoning_effort}" if reasoning_effort else "") +
          ("" if thinking == "enabled" else f" | temperature={temperature}"))
    print(f"本轮 {len(case_files)} 个 case → {run_dir.relative_to(ROOT)}")
    print("-" * 60)

    ok = fail = 0
    for f in case_files:
        c = parse_case(f)
        meta["cases"][c["id"]] = {"file": c["path"], "sha256": c["file_hash"],
                                  "category": c["category"]}

        system = tpl.replace("{CORPUS}", corpus).replace("{QUESTION}", c["question"])
        try:
            r = call_deepseek(api_key, system, c["question"], model, temperature,
                              thinking=thinking, reasoning_effort=reasoning_effort)
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
            "model": model, "temperature": temperature_for_record,
            "thinking": thinking, "reasoning_effort": reasoning_effort,
            "finish_reason": r["finish_reason"],
            "prompt_tokens": r["prompt_tokens"],
            "completion_tokens": r["completion_tokens"],
            "reasoning_tokens": r["reasoning_tokens"],
            "elapsed_sec": r["elapsed_sec"],
            "raw_id": r["raw_id"],
        }
        (raw_dir / f"{c['id']}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        md = (f"# {c['id']}（{c['category']}）\n\n"
              f"**问** {c['question']}\n\n"
              f"**答**\n\n{r['answer']}\n\n")
        if r["reasoning"]:
            md += (f"<details><summary>思考过程（{len(r['reasoning'])} 字，"
                   f"评分不参考）</summary>\n\n{r['reasoning']}\n\n</details>\n\n")
        md += (f"---\n\n"
               f"- 模型 {model} | thinking {thinking}"
               + (f"/{reasoning_effort}" if reasoning_effort else "") + "\n"
               f"- temperature {temperature_for_record if temperature_for_record is not None else '（thinking 模式下被忽略）'}\n"
               f"- token 入 {r['prompt_tokens']} / 出 {r['completion_tokens']}"
               + (f"（思考 {r['reasoning_tokens']}）" if r["reasoning_tokens"] else "") +
               f" | 耗时 {r['elapsed_sec']}s\n"
               f"- case hash {c['file_hash']} | 语料 hash {sha256(corpus)[:12]}\n")
        (raw_dir / f"{c['id']}.md").write_text(md, encoding="utf-8")

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
