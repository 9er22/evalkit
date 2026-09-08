"""
把 FastAPI 官方教程文档解析成**自包含**的语料快照。

为什么要这一步：FastAPI 的文档源码用 `{* ../../docs_src/xxx.py hl[3] *}` 引用外部 Python 文件，
代码块并不在 markdown 里。不展开的话，"答案在文档里"的 case 会变成"文档里其实没有"——
评测还没开始就已经失效了。

产出：
  research/corpus/resolved/fastapi-<page>.md   展开后的单页
  research/corpus/CORPUS.md                    合并后的完整语料（评测时注入 prompt 的就是它）

用法：
  python src/fetch_corpus.py              # 用已固定的 commit
  python src/fetch_corpus.py --sha <sha>  # 换 commit（会同时更新 MANIFEST）
"""

import argparse
import re
import sys
import urllib.request
from pathlib import Path

REPO = "fastapi/fastapi"
PINNED_SHA = "50113da16fec53b66b80d75e80a89296de4fa5a5"
RAW = f"https://raw.githubusercontent.com/{REPO}/{{sha}}/{{path}}"

PAGES = ["path-params", "query-params", "body", "response-model"]
DOC_DIR = "docs/en/docs/tutorial"

# {* ../../docs_src/body/tutorial001_py310.py hl[16] *}
INCLUDE_RE = re.compile(
    r"^\{\*?\s*\.\./\.\./docs_src/(?P<path>\S+?)(?:\s+hl\[[^\]]*\])?\s*\*?\}\s*$"
)

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "research" / "corpus"
SRC_CACHE = CORPUS / "_src"
RESOLVED = CORPUS / "resolved"


def fetch(sha: str, path: str) -> str:
    url = RAW.format(sha=sha, path=path)
    req = urllib.request.Request(url, headers={"User-Agent": "evalkit-corpus-fetch"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8")


def resolve_page(sha: str, page: str, cache: dict) -> str:
    raw = fetch(sha, f"{DOC_DIR}/{page}.md")
    out = []
    for line in raw.splitlines():
        m = INCLUDE_RE.match(line)
        if not m:
            out.append(line)
            continue
        rel = m.group("path")
        if rel not in cache:
            cache[rel] = fetch(sha, f"docs_src/{rel}")
        body = cache[rel].rstrip("\n")
        lang = "python" if rel.endswith(".py") else ""
        out.append(f"```{lang}\n# docs_src/{rel}\n{body}\n```")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sha", default=PINNED_SHA, help="FastAPI 仓库的 commit，用于固定语料版本")
    args = ap.parse_args()

    RESOLVED.mkdir(parents=True, exist_ok=True)
    SRC_CACHE.mkdir(parents=True, exist_ok=True)

    cache: dict[str, str] = {}
    combined = []
    for page in PAGES:
        text = resolve_page(args.sha, page, cache)
        (RESOLVED / f"fastapi-{page}.md").write_text(text, encoding="utf-8")
        n_inc = sum(1 for _ in re.finditer(r"^# docs_src/", text, re.M))
        print(f"  {page:16s} {len(text):>7,d} 字符, 展开 {n_inc} 个代码块")
        combined.append(f"\n\n<!-- ===== 来源: {DOC_DIR}/{page}.md ===== -->\n\n{text}")

    corpus = "".join(combined)
    (CORPUS / "CORPUS.md").write_text(corpus, encoding="utf-8")

    manifest = (
        "# 语料清单\n\n"
        "| 项 | 值 |\n|---|---|\n"
        f"| 来源仓库 | https://github.com/{REPO} |\n"
        f"| 固定 commit | `{args.sha}` |\n"
        f"| 取用日期 | 见 git 提交记录 |\n"
        f"| 页面 | {', '.join(f'`{DOC_DIR}/{p}.md`' for p in PAGES)} |\n"
        f"| 展开的外部代码文件 | {len(cache)} 个 |\n"
        f"| 合并产物 | `CORPUS.md`（{len(corpus):,} 字符） |\n\n"
        "## 文件说明\n\n"
        "- `fastapi-<page>.md`：原始 markdown，**未展开**外部引用，仅作溯源备份\n"
        "- `resolved/fastapi-<page>.md`：展开后的单页\n"
        "- `_src/`：展开时下载的外部 Python 文件缓存\n"
        "- `CORPUS.md`：**评测时真正注入 prompt 的语料**，由本脚本生成，不要手改\n\n"
        "## 为什么固定 commit\n\n"
        "语料在变，评测结果就不可复现。本项目的 rubric 里有「标准版本可追溯」这一维，"
        "语料自己必须先做到可追溯，否则是自相矛盾。\n"
    )
    (CORPUS / "MANIFEST.md").write_text(manifest, encoding="utf-8")

    print(f"\nCORPUS.md 已生成：{len(corpus):,} 字符")
    print(f"外部代码文件：{len(cache)} 个")
    print(f"固定 commit：{args.sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
