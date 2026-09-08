# AGENTS.md

项目：EvalKit —— 一个最小的 LLM / Agent 输出评测工具。两周冲刺 2026-09-08 → 2026-09-22。
重点不是代码，是**评分标准（rubric）怎么定**，以及定完之后发现了什么问题。详见 `README.md`。

## Agent skills

### Issue tracker

issue 以本地 markdown 文件形式存在 `.scratch/<feature>/` 下，不用 GitHub Issues。See `docs/agents/issue-tracker.md`.

### Triage labels

使用五个默认角色标签：`needs-triage` / `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix`。See `docs/agents/triage-labels.md`.

### Domain docs

single-context。ADR 记在 `docs/决策记录.md`（编号 D1…），**没有** `docs/adr/`。See `docs/agents/domain.md`.
