# Triage Labels

工程技能用五个标准 triage 角色说话。本文件把这些角色映射到本仓库实际使用的标签字符串。

| mattpocock/skills 中的标签 | 本仓库使用的标签 | 含义 |
| --- | --- | --- |
| `needs-triage` | `needs-triage` | 需要维护者评估这个 issue |
| `needs-info` | `needs-info` | 等待提出者补充信息 |
| `ready-for-agent` | `ready-for-agent` | 已充分定义，可以交给 agent 独立完成 |
| `ready-for-human` | `ready-for-human` | 需要人工实施 |
| `wontfix` | `wontfix` | 不处理 |

当技能提到某个角色（比如"打上 AFK-ready 的 triage 标签"），使用上表右侧对应的字符串。

本仓库使用的是**默认值**——此前没有任何标签体系，不存在重复问题。

因为 issue 是本地 markdown（见 `issue-tracker.md`），这些标签不是真的 GitHub label，而是写在 issue 文件顶部的 `Status:` 行里。
