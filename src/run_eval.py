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

# ----------------------------- 供应商注册表 -----------------------------
#
# 两家并存（2026-09-10 起）：
#   ark      = 火山方舟 Coding Plan，订阅制，一个月内有效 —— 额度优先烧它
#   deepseek = DeepSeek 官方 API，按量计费 —— 当备胎，也用于 judge（保证判定可比）
#
# ⚠ 火山方舟有两套端点，填错会白花钱：
#   /api/coding/v3  → Coding Plan 订阅额度（本项目用这个）
#   /api/v3         → 在线推理，按量另计费，**不消耗**套餐额度
#
# 模型名在两家之间会撞车（都有 deepseek-v4-flash），所以要么用
# `--provider ark` 显式指定，要么写成 `ark:deepseek-v4-flash`。

PROVIDERS = {
    "ark": {
        "label": "火山方舟 Coding Plan",
        "url": "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions",
        "key_env": "ARK_API_KEY",
        # Coding Plan 是 OpenAI 兼容端点，不吃 DeepSeek 的 thinking 字段
        "supports_thinking": False,
        # Model Name 全小写（火山方舟官方文档 2026-09-10 核对）
        # 注意这些是套餐名，不是在线推理的 Model ID（后者形如 deepseek-v3-2-251201）
        "models": {
            "deepseek-v4-flash":    "deepseek-v4-flash",
            "deepseek-v4-pro":      "deepseek-v4-pro",
            "kimi-k3":              "kimi-k3",
            "kimi-k2.7-code":       "kimi-k2.7-code",
            "kimi-k2.6":            "kimi-k2.6",
            "glm-5.3":              "glm-5.3",
            "glm-5.3-flash":        "glm-5.3-flash",
            "minimax-m3":           "minimax-m3",
            "minimax-m2.7":         "minimax-m2.7",
            "doubao-seed-2.1-turbo": "doubao-seed-2.1-turbo",
            "doubao-seed-evolving": "doubao-seed-evolving",
            "doubao-seed-2.0-lite": "doubao-seed-2.0-lite",
            # Auto 模式：实际模型在控制台「开通管理」页切换，切换后 3-5 分钟生效
            "ark-code-latest":      "ark-code-latest",
        },
    },
    "deepseek": {
        "label": "DeepSeek 官方",
        "url": "https://api.deepseek.com/chat/completions",
        "key_env": "DEEPSEEK_API_KEY",
        "supports_thinking": True,  # v4 系默认开思考且忽略 temperature，必须显式关
        # api-docs.deepseek.com，2026-09-08 核对。
        # 注意：deepseek-chat / deepseek-reasoner 已于 2026-04-24 官方公告废弃
        # （原定 2026-07-24 停用，实测 2026-09-08 仍可调通）——随时可能失效，评测别再依赖。
        "models": {
            "v4-flash":        "deepseek-v4-flash",            # 当前版本 DeepSeek-V4-Flash-0731
            "v4-pro":          "deepseek-v4-pro",              # 当前版本 DeepSeek-V4-Pro-0813，价格约 3 倍
            "v4-flash-vision": "deepseek-v4-flash-vision-exp", # 实验版多模态
            "chat":            "deepseek-chat",                # 废弃别名 = v4-flash 非思考模式
            "reasoner":        "deepseek-reasoner",            # 废弃别名 = v4-flash 思考模式
        },
    },
}

# 向后兼容：旧脚本 import MODELS 只用来列可选别名
MODELS = PROVIDERS["deepseek"]["models"]

DEFAULT_PROVIDER = "ark"            # 订阅额度有时限，先烧它
DEFAULT_MODEL = "deepseek-v4-flash"
# 兜底链：ark 挂了（额度耗尽 / 401 / 限流）就退到 DeepSeek 官方
FALLBACK_PROVIDER = "deepseek"
FALLBACK_MODEL = "deepseek-v4-flash"
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

def split_provider(name: str) -> tuple[str | None, str]:
    """`ark:deepseek-v4-flash` → ("ark", "deepseek-v4-flash")。没写前缀则 (None, 原名)。"""
    if ":" in name:
        head, tail = name.split(":", 1)
        if head in PROVIDERS:
            return head, tail.strip()
    return None, name


def resolve(name: str, provider: str | None = None) -> tuple[str, str]:
    """(模型名, 供应商) → (供应商, 完整模型名)。

    查找顺序：显式前缀 > 指定供应商 > 默认供应商 > 其它供应商。
    最后一道：没进注册表但看着像模型名就透传——试新模型必须留这个口子。
    """
    m = (name or "").strip()
    if not m:
        die("模型名为空。用 --model deepseek-v4-flash，或在 .env 里设 EVAL_MODEL。")

    explicit, m = split_provider(m)
    provider = explicit or provider

    if provider:
        if provider not in PROVIDERS:
            die(f"未知供应商 `{provider}`。可用：{', '.join(PROVIDERS)}")
        table = PROVIDERS[provider]["models"]
        if m in table:
            return provider, table[m]
        print(f"⚠ `{m}` 不在 {provider} 的注册表里，直接透传（新模型/过期测试）")
        return provider, m

    # 未指定供应商：默认供应商优先，其它家兜底
    order = [DEFAULT_PROVIDER] + [p for p in PROVIDERS if p != DEFAULT_PROVIDER]
    for p in order:
        if m in PROVIDERS[p]["models"]:
            return p, PROVIDERS[p]["models"][m]
    print(f"⚠ 未在注册表中找到 `{m}`，按 {DEFAULT_PROVIDER} 透传")
    return DEFAULT_PROVIDER, m


def resolve_model(name: str, provider: str | None = None) -> str:
    """兼容旧调用：只返回完整模型名。"""
    return resolve(name, provider)[1]


def get_api_key(env: dict, provider: str) -> str:
    key_env = PROVIDERS[provider]["key_env"]
    v = (env.get(key_env) or "").strip()
    if not v or v.endswith("xxx"):
        die(f"没读到 {key_env}（供应商 {provider}）。"
            f"在 .env 里填入真实 key，或用 --provider 换一家。")
    return v


class ApiError(Exception):
    """API 返回非 2xx。message 里带状态码和响应体片段。"""


def is_provider_failure(msg: str) -> bool:
    """供应商级故障：套餐额度耗尽、鉴权失败、限流。

    这类错误换模型没用，得换供应商。Coding Plan 额度用光返回 4028。
    """
    markers = ("4028", "401", "403", "429", "quota", "exceeded",
               "Unauthorized", "Insufficient", "Payment Required")
    return any(s in msg for s in markers)


def call_deepseek(api_key: str, system: str, question: str,
                  model: str, temperature: float,
                  thinking: str | None = None,
                  reasoning_effort: str | None = None,
                  timeout: int = 120,
                  *, provider: str = "deepseek") -> dict:
    """发一次 chat 请求。provider 决定端点和要不要带 thinking 字段。"""
    conf = PROVIDERS[provider]
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
    # 火山方舟 Coding Plan 是 OpenAI 兼容端点，不吃这个字段，传了可能直接 400。
    if conf["supports_thinking"]:
        if thinking:
            payload["thinking"] = {"type": thinking}
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
    req = urllib.request.Request(
        conf["url"],
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
        raise ApiError(f"[{conf['label']}] API 返回 {e.code}：{detail}") from e
    except urllib.error.URLError as e:
        raise ApiError(f"[{conf['label']}] 网络请求失败：{e.reason}") from e
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
    ap.add_argument("--provider", choices=list(PROVIDERS), default=None,
                    help=f"供应商：{' / '.join(PROVIDERS)}（默认 .env 的 EVAL_PROVIDER "
                         f"或 {DEFAULT_PROVIDER}）")
    ap.add_argument("--model", default=None,
                    help="模型名，可带供应商前缀，如 ark:kimi-k3、deepseek:v4-flash。"
                         "覆盖 .env 的 EVAL_MODEL")
    ap.add_argument("--thinking", choices=["enabled", "disabled"], default=None,
                    help="思考模式开关（默认 disabled）。v4 系默认开思考且忽略 temperature")
    ap.add_argument("--effort", choices=["low", "high", "max"], default=None,
                    help="思考档位，仅 thinking=enabled 时生效")
    args = ap.parse_args()

    env = load_env()
    provider_arg = args.provider or (env.get("EVAL_PROVIDER") or "").strip() or None
    model_alias = (args.model or env.get("EVAL_MODEL")
                   or env.get("DEEPSEEK_MODEL") or DEFAULT_MODEL)
    provider, model = resolve(model_alias, provider_arg)
    api_key = get_api_key(env, provider)

    thinking = args.thinking or env.get("EVAL_THINKING", DEFAULT_THINKING)
    if thinking == "enabled" and not PROVIDERS[provider]["supports_thinking"]:
        print(f"⚠ {PROVIDERS[provider]['label']} 不支持 thinking 参数，已忽略")
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
        "provider": provider,
        "provider_label": PROVIDERS[provider]["label"],
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

    print(f"语料 {len(corpus):,} 字符 | {PROVIDERS[provider]['label']} · {model} | "
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
                              thinking=thinking, reasoning_effort=reasoning_effort,
                              provider=provider)
        except ApiError as e:
            msg = str(e)
            # 降级一：模型名被拒（内测名过期等）→ 同供应商内换兜底模型
            if ("400" in msg and "model" in msg.lower()
                    and model != FALLBACK_MODEL):
                print(f"⚠ {c['id']}: 模型 `{model}` 被服务端拒绝，"
                      f"自动降级到 `{FALLBACK_MODEL}`，后续 case 也用它")
                meta["model_fallback"] = {"from": model, "to": FALLBACK_MODEL,
                                          "at_case": c["id"], "reason": msg[:200]}
                model = FALLBACK_MODEL
            # 降级二：供应商级故障（套餐额度耗尽 4028 / 鉴权失败 / 限流）
            #         → 换模型没用，整家换掉
            elif provider != FALLBACK_PROVIDER and is_provider_failure(msg):
                print(f"⚠ {c['id']}: {PROVIDERS[provider]['label']} 不可用（{msg[:90]}），"
                      f"整轮切到 {PROVIDERS[FALLBACK_PROVIDER]['label']}")
                meta["provider_fallback"] = {"from": provider,
                                             "to": FALLBACK_PROVIDER,
                                             "at_case": c["id"], "reason": msg[:200]}
                provider = FALLBACK_PROVIDER
                model = FALLBACK_MODEL
                api_key = get_api_key(env, provider)
            else:
                print(f"FAIL {c['id']} {msg[:200]}")
                fail += 1
                continue
            # 降级后重试一次
            try:
                r = call_deepseek(api_key, system, c["question"], model, temperature,
                                  thinking=thinking, reasoning_effort=reasoning_effort,
                                  provider=provider)
            except ApiError as e2:
                print(f"FAIL {c['id']} 降级后仍失败：{e2}")
                fail += 1
                continue
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
            "provider": provider, "model": model,
            "temperature": temperature_for_record,
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
               f"- 供应商 {PROVIDERS[provider]['label']} | 模型 {model} "
               f"| thinking {thinking}"
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
