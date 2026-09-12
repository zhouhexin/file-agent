# WorkBuddy MCP 搜索会话外键回归修复方案

日期：2026-09-12
状态：待开发；本文只定义修复，不包含代码改动。

## 1. 问题与影响

WorkBuddy 通过 MCP 调用 `file_search` 时，后端收到：

```text
POST /api/search
```

实际请求已完成两阶段检索并得到候选，但随后返回 HTTP 500。2026-09-12 的请求日志记录：

```text
request_id: req_b74e1fefed26418f82ef46a12996bb80
retrieval.search.completed: result_count=1, status=DEGRADED
api.request.failed: error_code=IntegrityError, path=/api/search
```

因此，500 不是 File Agent 未选择 MCP 工具、令牌错误、自然语言解析失败，也不是“没有搜索结果”。检索主体已经完成；失败发生在检索完成后保存相关文件集合、安排后续受控物化的写库阶段。

影响范围：

- WorkBuddy MCP 的 `file_search` 会在有最终相关结果、需要写入 `RelevantFileSet` 时失败。
- 搜索结果因为同一事务回滚而无法返回给 WorkBuddy。
- 后续依赖搜索结果的 `file_read`、`evidence_answer` 和 `file_rename` 无法自然衔接。
- 普通 Web/API 调用如果传入一个不存在的 `conversation_id`，同样可能触发该问题。

不受影响：

- `file_batch_ingest`、`workbuddy_attachment_ingest`、查重和 `duplicate_*` 决策走的是导入批次链路，不写 `RelevantFileSet.conversation_id`，所以重复文件检测仍可正常返回给 WorkBuddy。

## 2. 已确认根因

当前 MCP 映射逻辑位于：

```text
apps/mcp/file_agent_mcp/conversation_tools.py
conversation_id_for_workbuddy(conversation_ref)
```

它把 WorkBuddy 的稳定会话引用转换成：

```text
wb-<sha256 前 32 位>
```

该值长度受控、稳定且不泄漏宿主原始会话 ID，这部分设计正确。

但是 `WorkBuddyConversationService.search()` 直接调用 `FileAgentIntegrationClient.file_search()`，把上述 ID 传给 `/api/search`。目前 `/api/search` 的 `search_files()`：

1. 使用该 ID 解析会话检索范围；此处允许会话不存在。
2. 完成两阶段检索和完整性评估。
3. 调用 `RelevantFileSetService.persist_and_enqueue()`。
4. 将 `request.conversation_id` 写入 `relevant_file_sets.conversation_id`。

`RelevantFileSet.conversation_id` 是 `conversations.id` 的外键。MCP 搜索没有先创建对应的 `Conversation` 行，因而数据库拒绝外键写入，抛出 `IntegrityError`；整个请求事务回滚，客户端最终看到 500。

已有 `ConversationRepository.ensure_conversation(conversation_id, user_id)` 可以安全创建或读取当前用户的空会话记录，且会拒绝其他用户拥有的同 ID 会话。因此不是缺少基础能力，而是 `/api/search` 没有在持久化会话外键之前调用该现有能力。

## 3. 修复目标与非目标

### 3.1 目标

1. MCP 第一次在一个新的 WorkBuddy 对话中调用 `file_search` 时，不再返回 500。
2. 如果本次搜索产生需要固化的最终结果，`RelevantFileSet` 能绑定到当前用户实际拥有的会话。
3. 如果没有最终结果，搜索仍保持只读，不为了“占位”而创建无意义的会话或任务。
4. 任意用户不能借用猜测出的 `wb-*` ID 读取、写入或关联另一用户的会话。
5. 现有 Web 搜索、聊天搜索、批量导入、重复检测、分类落位、重命名和证据问答不得回归。

### 3.2 非目标

- 不改变 WorkBuddy `conversation_ref` 的哈希映射格式。
- 不移除 `relevant_file_sets.conversation_id` 外键或将其改为无约束字符串。
- 不把 `/api/search` 改成忽略所有数据库写入错误后仍返回成功。
- 不改变查重规则、检索排序、分类规则或工作副本物化策略。
- 不新增 MCP 工具、数据库表或迁移。

## 4. 选定修复方案

在 `/api/search` 的 `search_files()` 中，只在确定本轮有需要持久化的最终相关结果、且请求携带 `conversation_id` 时，调用现有：

```python
ConversationRepository(db).ensure_conversation(
    conversation_id=request.conversation_id,
    user_id=current_user.id,
)
```

再调用：

```python
RelevantFileSetService(...).persist_and_enqueue(...)
```

### 4.1 推荐执行顺序

```text
认证当前用户
-> 执行检索与完整性评估
-> 计算最终 results
-> 若 results 无需固化：直接返回，不创建会话
-> 若 results 需要固化且 conversation_id 存在：ensure_conversation
-> persist_and_enqueue
-> 返回搜索投影
```

“是否需要固化”必须和 `RelevantFileSetService.persist_and_enqueue()` 的现有过滤条件一致：仅最终相关结果中含 `SUPPORTED`、`RELATED` 或 `POSSIBLE`，且存在 `working_copy_id` 或 `managed_file_revision_id` 时才需要创建集合。避免因为一个普通空搜索创建大量空会话。

### 4.2 为什么不在 MCP 客户端先创建会话

- `/api/search` 仍是公开的已认证 API，其他调用方也可能传入不存在的会话 ID；只修 MCP 会留下 API 回归入口。
- 会话所有权、数据库事务和外键完整性应该由 API 服务端统一处理，不能依赖每个客户端先调用某个隐式初始化步骤。
- `ensure_conversation` 已在后端存在，复用它是最小改动。

### 4.3 为什么不能简单传 `conversation_id=null`

这能暂时绕开外键，但会丢失“本次检索结果属于哪个 WorkBuddy 对话”的审计关系，并使后续证据问答、读取和重命名无法安全复用同一对话上下文。它不是可接受的修复。

## 5. 实施文件与最小改动

| 文件 | 修改 |
|---|---|
| `apps/api/app/modules/retrieval/router.py` | 导入并调用既有 `ConversationRepository.ensure_conversation`；只在本次确实要写 `RelevantFileSet` 时执行。 |
| `apps/api/app/tests/test_file_search_api.py` 或新增专项测试 | 增加 MCP 风格 `wb-*` 会话 ID 的首次搜索测试、所有权冲突测试和空结果不创建会话测试。 |
| `apps/mcp/tests/test_conversation_tools.py` | 保留现有稳定映射测试；补充真实 API 集成或客户端契约测试，证明 `file_search` 可使用映射 ID。 |
| `docs/runbook.md` | 补充说明：MCP 搜索首次使用会由后端创建当前用户的受控会话占位记录；用户无需配置或手写会话 ID。 |

不修改 MCP 配置文件、数据库 schema、迁移、查重链路或前端页面。

## 6. 自动化验收

### 6.1 必须新增或调整的用例

| 编号 | 场景 | 预期 |
|---|---|---|
| S01 | 新用户、全新 `wb-*` 会话、搜索有最终相关文件 | HTTP 200；返回结果；创建当前用户的 `Conversation` 与 `RelevantFileSet`。 |
| S02 | 同一 `wb-*` 会话再次搜索 | HTTP 200；复用会话；不产生所有权冲突。 |
| S03 | 空结果或不满足固化条件的结果 | HTTP 200；不创建空 `Conversation` 或 `RelevantFileSet`。 |
| S04 | 用户 A 已拥有相同会话 ID，用户 B 请求该 ID | 不泄漏 A 数据；返回受控 403/404，且不写相关集合。 |
| S05 | `conversation_id=null` 的普通 API 搜索 | HTTP 200；行为保持现有兼容性。 |
| S06 | `RelevantFileSet` 写入正常时 | `conversation_id` 指向真实同用户会话，外键有效。 |
| S07 | 受管源检索或 Chunk 分支降级 | 仍可返回 200 的部分结果；降级日志不再演变为外键 500。 |
| S08 | 批量导入后的 `duplicate_review_get`/`duplicate_decide` | 回归通过，证明不影响重复确认。 |
| S09 | MCP `file_search` | 客户端能收到结构化搜索结果，而不是 `POST /api/search` 500。 |

### 6.2 建议命令

```powershell
$env:PYTHONPATH='apps/api;apps/mcp'
& 'D:\anaconda\envs\myenv\python.exe' -m pytest `
  apps/api/app/tests/test_file_search_api.py `
  apps/api/app/tests/test_file_search_clarification.py `
  apps/mcp/tests/test_conversation_tools.py `
  apps/mcp/tests/test_client.py -q
```

如果修复触及相关文件集合和物化入队，再补跑：

```powershell
$env:PYTHONPATH='apps/api'
& 'D:\anaconda\envs\myenv\python.exe' -m pytest `
  apps/api/app/tests/test_managed_files_worker.py -q
```

## 7. WorkBuddy 实机复测

修复部署并重连 MCP 后，在一个全新 WorkBuddy 对话发送：

> 找出去年奖学金申请相关文件，并说明文件名和依据。

预期：

1. WorkBuddy 调用 `file_search`。
2. 不再出现 `POST /api/search 500`。
3. 返回文件结果或受控“未找到/结果不完整”提示。
4. 继续发送“请阅读第一份文件并总结”，能够走 `file_read`；再发送问题能够走 `evidence_answer`。
5. 更换 File Agent 账号后，不得读取此前账号的同一 WorkBuddy 会话结果。

## 8. 部署与回退

- 此修复不需要改 `mcp.json`、不需要数据库迁移、也不需要清理现有文件或批次。
- 需要更新 API 服务；MCP 客户端代码无变更时不必更新每台 WorkBuddy 的 MCP 包，但重连后应进行一次实际搜索验证。
- 回退仅恢复 API 到前一版本；不会影响已导入文件、重复确认和工作副本。
- 禁止通过删除外键、手工删除数据或将 `conversation_id` 永久置空作为应急修复。
