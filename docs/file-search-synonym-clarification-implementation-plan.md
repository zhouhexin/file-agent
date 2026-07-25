# 文件检索同义短语扩展与歧义选择实施方案

## 1. 目标

本阶段解决自然语言文件检索中的两个边界问题：

1. 用户说“提到、包含、出现”时，系统必须按正文连续短语查找，不能把短语静默拆成宽泛 OR。
2. 用户使用“相关、有关、关于”或未明确匹配范围时，系统可以使用受控同义短语扩展；只有不同解释会产生实质不同结果时，才通过对话选择卡让用户决定。

普通用户不需要理解分词、Skill、Tool、Chunk、召回或内部评分。所有选择项必须使用业务语言。

## 2. 查询模式

- `LITERAL`：适用于“提到、包含、出现、写到、涉及、原文有”。结果必须具有正文连续短语证据。
- `RELATED`：适用于“相关、有关、关于、类似”。允许完整同义短语命中文件名、摘要或正文。
- `UNSPECIFIED`：用户只输入主题而未说明范围。系统先做有上限的预检，结果集合存在实质差异时生成选择卡。
- `BROAD`：仅在用户明确选择“只查任职”或“只查通知”等宽泛主题后使用。

禁止把“任职通知”静默转换成 `任职 OR 通知`。同义扩展也必须按完整短语逐项匹配。

## 3. 同义词来源

当前阶段使用项目内版本化 JSON：

```text
apps/api/app/modules/retrieval/synonyms/school_file_search_synonyms.json
```

每个词组包含稳定 `id`、`canonical`、`aliases` 和 `version`。单次扩展最多 8 个短语，每个短语限制为 2 至 30 个字符。LLM 只能提出候选，不能直接修改或启用正式词组。

## 4. 选择卡触发规则

以下情况生成选择卡：

1. `LITERAL` 精确正文零结果，但受控同义短语存在正文结果。
2. `UNSPECIFIED` 的精确与同义扩展结果集合不同。
3. 同义扩展或宽泛主题会显著增加结果数量。

以下情况不生成选择卡：

1. 精确正文已经得到明确结果。
2. `RELATED` 查询可以直接使用受控同义短语。
3. 精确与扩展结果集合一致。
4. 用户已经明确选择匹配范围。

选择卡允许：

- 只查原短语。
- 查原短语及同义表达。
- 只查拆分后的某个业务主题。
- 输入自定义短语。

系统不得预选宽泛选项，也不得自动合并多个宽泛主题。

## 5. 持久化与续跑

新增 `file_search_clarifications`：

- `id`
- `conversation_id`
- `user_id`
- `agent_run_id`
- `original_query`
- `core_phrase`
- `relation_mode`
- `options_json`
- `status`
- `selected_option_id`
- `resolution_json`
- `result_message_id`
- `result_agent_run_id`
- `created_at`
- `resolved_at`
- `expires_at`

状态为 `WAITING_SELECTION`、`RESOLVED`、`SUPERSEDED`、`EXPIRED`。这不是高风险文件操作，因此原 AgentRun 使用 `NEEDS_REVIEW`，不得伪装为 OperationPlan。

前端只能提交服务端生成的 `option_id`；自定义短语必须重新经过长度、字符和查询 schema 校验。用户也可以在对话中回复“按同义表达查”“只查任职”，后端应解析当前会话最新的待选择记录并生成新 AgentRun。

## 6. 普通用户接口

新增普通回执类型：

```text
response_type = file_search_clarification
```

回执只包含选择卡 ID、提示、核心短语、脱敏选项、预估文件数和是否允许自定义短语。不得包含 Tool 输入、SQL、内部路径、哈希和评分。

选择提交接口：

```text
POST /api/file-search/clarifications/{clarification_id}/resolve
```

后端必须校验当前用户、会话、状态、过期时间和允许选项，并保证重复提交幂等。
首次执行成功后绑定唯一消息和 AgentRun；重复点击或网络重试直接复用该结果，不能生成重复回答。

## 7. 检索执行边界

- `LITERAL` 最终结果必须保留正文 Chunk 连续命中。
- `RELATED` 可以保留文件名、摘要或正文中的完整同义短语命中。
- `BROAD` 只有用户明确选择后才允许词项检索。
- 普通检索只访问共享工作区的 `ACTIVE` 工作副本，回收站继续由完整文件名恢复流程处理。
- 所有数据库条件使用绑定参数，候选文档数、短语数和 Chunk 数必须有固定上限。

## 8. 前端

新增 `FileSearchClarificationCard.tsx`：

- 单选业务范围。
- 展示同义表达示例和预估数量。
- 可输入自定义短语。
- 提交后显示现有 `SearchResultsReceipt`。
- 刷新页面后仍能恢复未选择或已解决状态。

## 9. 测试

必须覆盖：

- 查询关系模式解析。
- 同义词配置结构、去重、上限和版本。
- 精确短语不退化为 OR。
- `LITERAL` 没有正文证据时不返回文件。
- `RELATED` 可以按完整同义短语扩展。
- 仅结果集合存在实质差异时生成选择卡。
- 用户、会话、状态、过期和幂等校验。
- 卡片选择与自由文本回复续跑。
- 回收站不进入普通检索。
- Windows、SQLite deterministic fake 与 PostgreSQL 路径行为一致。
- 前端构建成功。
