# File Agent Skills

本目录存放 File Agent 产品内 Agent Skill 规则。

详细清单见 [`docs/skills-catalog.md`](../docs/skills-catalog.md)。

MVP 阶段每个 Skill 至少应有一个 `SKILL.md`，用于定义触发条件、输入输出、可调用 Tool、处理步骤、证据规则、ChangeSet 规则、OperationPlan 规则和禁止事项。

当前 MVP 保留以下业务 Skill：

```text
change-report
chat-intake
confirmed-file-action
document-classification
evidence-answer
feedback-and-memory
file-ingest
file-search
managed-file-query
operation-plan
```

底层通用能力不再作为 Skill 存放在本目录，而是在 Tool Adapter 层使用开源项目实现。每个 `SKILL.md` 的 `Open Source Backing` 小节标注该 Skill 是否直接或间接使用开源组件；完整地址见 `docs/skills-catalog.md`。
