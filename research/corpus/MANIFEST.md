# 语料清单

| 项 | 值 |
|---|---|
| 来源仓库 | https://github.com/fastapi/fastapi |
| 固定 commit | `50113da16fec53b66b80d75e80a89296de4fa5a5` |
| 取用日期 | 见 git 提交记录 |
| 页面 | `docs/en/docs/tutorial/path-params.md`, `docs/en/docs/tutorial/query-params.md`, `docs/en/docs/tutorial/body.md`, `docs/en/docs/tutorial/response-model.md` |
| 展开的外部代码文件 | 28 个 |
| 合并产物 | `CORPUS.md`（50,917 字符） |

## 文件说明

- `fastapi-<page>.md`：原始 markdown，**未展开**外部引用，仅作溯源备份
- `resolved/fastapi-<page>.md`：展开后的单页
- `_src/`：展开时下载的外部 Python 文件缓存
- `CORPUS.md`：**评测时真正注入 prompt 的语料**，由本脚本生成，不要手改

## 为什么固定 commit

语料在变，评测结果就不可复现。本项目的 rubric 里有「标准版本可追溯」这一维，语料自己必须先做到可追溯，否则是自相矛盾。
