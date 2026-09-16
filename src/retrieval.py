#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""检索器 v2（P0 检索回归 T1）——给"中文提问 → 英文语料"加一座桥 + 长度归一化。

## 为什么要它

闭卷轮的 7 个失败**全部**是检索漏检（B 型），根因两条（见 docs/检索回归-实验报告-20260916.md）：

1. **跨语言断裂（主因，物理级）**：语料是纯英文（FastAPI 官方文档，中文字符数 = 0），
   提问是中文，而 `tokenize()` 对中文只产**二元组**——中文二元组永远匹配不到英文语料。
   于是中文提问里只有"夹带的英文标识符"（`item_id`、`response_model`…）能参与匹配，
   提问中英文 token 数为 0 的 case 检索结果是**空集**（物理上不可能命中）。
2. **打分无长度归一化（次因）**：`sum(log(n/(1+df)))` 无 tf、无长度归一化 →
   token 多的长 chunk 命中机会天然更多。实测最长 chunk（6766 字符，是中位数的 8 倍）
   霸榜 11 个 case 的 top-1。

对应两个改动，**只动"选哪 3 个片段"这一件事**，其余（模型/温度/语料/提示词/rubric）全部不动：

- `v2-bridge`：术语桥（中文术语 → 英文术语，追加到查询尾部）；
- `v2-bm25`：只换 BM25（k1=1.2, b=0.75）；
- `v2`：两者都上（本实验的处理臂）。

## 为什么术语表要单独成文件

术语表是**判据的一部分**（决定"检索能看见什么"），必须能被独立登记指纹、独立复核，
所以放 `src/terms_zh_en.tsv` 而不是硬编码在本文件里。消融证据见该文件头部注释。

## 选取语义必须与 v1 逐字一致

`Retriever.search`（v1）的规则是：过滤 `score > 0`，按 `(score, index)` **降序**
（同分时 index 大的在前），取前 top_k。v2 的 `Bm25.search` 保持**完全相同**的规则——
否则"命中率变化"里会混进一个与改动无关的排序口径差异，实验就不干净了。
"""

from pathlib import Path
import math

TERMS_FILE = Path(__file__).resolve().parent / "terms_zh_en.tsv"

ARMS = ("v1", "v2-bridge", "v2-bm25", "v2")

ARM_LABEL = {
    "v1": "v1 词袋 + idf（基准，原文查询）",
    "v2-bridge": "v2-bridge 词袋 + idf + 术语桥（双语查询）",
    "v2-bm25": "v2-bm25 BM25 + 长度归一化（原文查询）",
    "v2": "v2 术语桥 + BM25（双语查询 + 长度归一化）",
}


def load_glossary(path=None) -> dict:
    """读术语表。<中文, 英文>，保持文件里的顺序（顺序不影响打分，只为可复现）。"""
    p = Path(path) if path else TERMS_FILE
    table = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        zh, en = parts[0].strip(), parts[1].strip()
        if zh and en:
            table[zh] = en
    return table


def expand_query(question: str, table: dict) -> str:
    """把查询里的中文术语映射成英文术语，**追加**到查询尾部（原中文保留）。

    不替换原文：保留中文可以继续吃到"提问里本就夹带的英文标识符"，
    替换会丢掉原查询里可能存在的信号。子串命中即追加（如"查询参数"同时命中"查询参数""查询"），
    重复的英文词对词袋/BM25 的集合口径无影响。
    """
    extra = [en for zh, en in table.items() if zh in question]
    if not extra:
        return question
    return question + " " + " ".join(extra)


class Bm25:
    """BM25（k1=1.2, b=0.75）——带文档长度归一化，抑制长 chunk 霸榜。

    与 `run_closed_book.Retriever` 的关系：同样的分词（`tokenize`）、同样的选取规则，
    唯一区别是打分公式。b=0.75 是 BM25 常规默认值，未做任何调参
    （调参会变成"跑完再调"——本项目最大的禁忌，见预注册文档）。
    """

    name = "BM25 + 长度归一化"

    def __init__(self, chunks, k1=1.2, b=0.75):
        from run_closed_book import tokenize  # 复用 v1 的分词，不另写一份

        self.chunks = chunks
        self.k1, self.b = k1, b
        self.tf = []
        for c in chunks:
            d = {}
            for t in tokenize(c["text"]):
                d[t] = d.get(t, 0) + 1
            self.tf.append(d)
        self.dl = [sum(d.values()) for d in self.tf]
        self.avgdl = sum(self.dl) / len(self.dl) if self.dl else 0
        self.df = {}
        for d in self.tf:
            for t in d:
                self.df[t] = self.df.get(t, 0) + 1
        self.n = len(chunks)
        self._idf = {t: math.log(1 + (self.n - df + 0.5) / (df + 0.5))
                     for t, df in self.df.items()}

    def scores(self, query: str) -> list:
        from run_closed_book import tokenize

        qt = set(tokenize(query))
        out = []
        for i, d in enumerate(self.tf):
            s = 0.0
            for t in qt:
                f = d.get(t, 0)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * self.dl[i] / self.avgdl)
                s += self._idf[t] * f * (self.k1 + 1) / denom
            out.append(s)
        return out

    def search(self, query: str, top_k: int) -> list:
        """与 v1 完全同构：过滤 score>0 → 按 (score, index) 降序 → 取前 top_k。"""
        sc = self.scores(query)
        scored = [(sc[i], i) for i in range(len(sc)) if sc[i] > 0]
        scored.sort(reverse=True)
        return [(i, round(s, 3)) for s, i in scored[:top_k]]
