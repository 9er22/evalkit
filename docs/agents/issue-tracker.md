# Issue tracker: 本地 Markdown

本仓库的 issue 与 spec 以 markdown 文件形式存放在 `.scratch/`。

**不用 GitHub Issues**：这个仓库的最终形态是公开作品集，开发过程中的 issue 队列属于噪音，不该出现在公开仓库里。

## 约定

- 一个 feature 一个目录：`.scratch/<feature-slug>/`
- spec 是 `.scratch/<feature-slug>/spec.md`
- 实施类 issue 一个文件一张票：`.scratch/<feature-slug>/issues/<NN>-<slug>.md`，从 `01` 开始编号，**不要**合并成单个 tickets 文件
- triage 状态记在文件顶部附近的 `Status:` 行（角色字符串见 `triage-labels.md`）
- 评论与讨论历史追加到文件末尾的 `## Comments` 下

## 当技能说"发布到 issue tracker"

在 `.scratch/<feature-slug>/` 下新建文件（目录不存在就创建）。

## 当技能说"取回相关 ticket"

读取引用路径上的文件。用户通常会直接给出路径或 issue 编号。

## Wayfinding 操作

供 `/wayfinder` 使用。**map** 是一个文件，每个 ticket 一个**子文件**。

- **Map**：`.scratch/<effort>/map.md`（Notes / Decisions-so-far / Fog 三段式正文）
- **子 ticket**：`.scratch/<effort>/issues/NN-<slug>.md`，从 `01` 编号，正文里写问题。`Type:` 行记 ticket 类型（`research`/`prototype`/`grilling`/`task`）；`Status:` 行记 `claimed`/`resolved`
- **阻塞**：顶部附近的 `Blocked by: NN, NN` 行。当它列出的文件全部 `resolved` 时才解除阻塞
- **Frontier**：扫描 `.scratch/<effort>/issues/`，找 open、未阻塞、未被认领的文件；按编号最小的优先
- **认领**：动手前先设 `Status: claimed` 并保存
- **解决**：把答案追加到 `## Answer` 标题下，设 `Status: resolved`，再把上下文指针（要点 + 链接）追加到 `map.md` 的 Decisions-so-far

## 注意

- `.scratch/` 目前**没有被 .gitignore 忽略**，会被提交。如果不想让它进公开仓库，在 `.gitignore` 加一行 `.scratch/`。
- 本仓库的**节点计划**不在 `.scratch/`，在 `docs/执行方案.md`。两者别混。
