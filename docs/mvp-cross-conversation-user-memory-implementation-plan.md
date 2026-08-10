# MVP 用户跨会话记忆实施计划

> 文档状态：待实施  
> 编写日期：2026-07-30  
> 适用范围：File Agent 当前 FastAPI + LangGraph + PostgreSQL 架构  
> 核心边界：只保存用户明确表达的结构化偏好，不实现 Graphiti，不从隐式行为自动学习

## 1. 目标

当前系统已经能够保存会话消息、引用当前会话历史附件，并将会话中出现过的文件作为检索排序信号；
这些能力属于会话上下文，不是跨会话用户记忆。

本计划实现一个可审计、可查看、可纠正、可忘记的 MVP 用户记忆闭环：

```text
用户在任意对话中明确说“记住……”
-> Planner 识别受控记忆意图
-> 后端把自然语言解析成白名单偏好类型
-> 用户偏好写入 PostgreSQL
-> 记录 ToolInvocation + ChangeSet
-> 后续新对话按用户 ID 加载相关偏好
-> 偏好只影响回答风格、查询扩展或有限排序
-> 用户可以查看、修改、忘记或确认清空全部偏好
```

实施后的用户体验示例：

- 用户说：“记住，以后回答简洁一些。”
- 新建另一个对话后，系统仍按简洁风格回答。
- 用户说：“记住，我说计算机学院时，也包括计算机科学与工程学院。”
- 后续文件检索可以受控扩展该用户的查询，但不会修改全局同义词、文件分类或其他用户结果。
- 用户说：“你记住了我什么？”系统列出当前有效偏好。
- 用户说：“忘记回答简洁这个偏好。”系统停用该单项偏好并给出回执。
- 用户说：“清空我的所有偏好。”系统先展示 OperationPlan，确认后才执行。

## 2. MVP 范围

### 2.1 必须实现

1. 用户明确偏好的新增、更新、读取、单项停用和批量清空。
2. 偏好跨会话生效，但严格按 `user_id` 隔离。
3. 回答风格偏好参与最终回复生成。
4. 用户自定义文件检索别名参与受控查询扩展。
5. 用户检索排序偏好只能提供有限加权，不能排除全局高相关文件。
6. 所有写入和停用都经过白名单 Tool、schema 校验和数据库审计。
7. 新增、更新和停用写入 ChangeSet / ChangeItem。
8. 批量清空偏好必须经过 OperationPlan 和用户确认。
9. 普通用户只能查看和操作自己的偏好。
10. 提供功能开关、数量上限、日志和 deterministic 测试。

### 2.2 明确不做

- 不接入 Graphiti。
- 不把 Neo4j 作为用户记忆事实源。
- 不保存完整聊天历史作为长期记忆。
- 不根据点击、打开、下载、未反馈或继续对话推断用户偏好。
- 不从文件正文、OCR 文本、网页文本或附件元数据自动创建用户记忆。
- 不使用 embedding 对用户记忆做向量召回。
- 不允许偏好修改文件客观分类、正文事实、正式 taxonomy 或共享工作副本状态。
- 不允许记忆绕过文件删除、覆盖、改名、移动等 OperationPlan 确认。
- 不实现组织级共享记忆、管理员代用户设置偏好或全局自动 Skill 演化。

## 3. 当前项目差距

当前项目存在以下预留边界，但尚未形成真实记忆闭环：

- `docs/database-schema.md` 声明了 `user_preferences`，实际 ORM 和 Alembic 尚未创建。
- `feedback-and-memory` Skill 已声明，但没有用户偏好 Service 和专用 Tool。
- `feedback-record` 主要用于 managed-file-query 反馈样本，不是通用用户记忆存储。
- `AgentContextLoader` 只加载本轮附件和文件洞察，没有加载用户跨会话偏好。
- Planner 没有 `MEMORY_READ`、`MEMORY_UPSERT`、`MEMORY_FORGET`、`MEMORY_CLEAR` 意图。
- 检索目前有 L0/L1/L4，尚未实现 L2 用户显式偏好排序。

实施时不得把现有反馈占位直接描述为已经完成的记忆能力。

## 4. 产品与安全原则

### 4.1 只接受显式记忆

只有用户使用明确表达时才允许写入，例如：

- “记住……”
- “以后都……”
- “把……设为我的偏好”
- “我说 A 时指 B”
- “以后查找时优先……”

以下情况不得自动写入：

- 用户在一次任务中临时要求“这次回答简短”。
- 用户打开或下载了某类文件。
- 用户接受了一次分类建议。
- 用户多次查询同一个学院。
- LLM 猜测用户可能喜欢某种风格。

Planner 必须区分“仅本轮生效”和“跨会话记住”。不明确时先询问，不得默认长期保存。

### 4.2 用户偏好不是事实

偏好只能影响表达和排序，不能成为文件事实证据。例如：

- “我通常把计算机学院叫计院”可以作为该用户的查询别名。
- 不能据此把文件正式分类改成“计院”。
- “优先显示计算机学院文件”只能加权，不能隐藏更相关的其他文件。
- 偏好不能用于回答金额、日期、人员、政策等事实问题。

### 4.3 共享文件与私人记忆分离

所有用户继续共享唯一物理工作目录和 `SYSTEM_SHARED` 工作区；用户记忆始终是私人数据：

```text
共享事实：
WorkingCopy / DocumentVersion / DocumentSummary / Chunk / Evidence / Category

用户私人状态：
Conversation / Message / AgentRun / UserPreference / Preference ChangeSet
```

用户 A 的偏好不得改变用户 B 的检索结果、回答风格、分类关系或文件状态。

### 4.4 禁止保存的内容

后端 schema 和 Service 必须拒绝：

- 密码、JWT、API key、Cookie、访问令牌和数据库连接串。
- 任意系统提示、Tool 参数、Shell、SQL 或绕过确认的指令。
- 文件正文、OCR 全文、完整 LLM Prompt。
- 超长自由文本。
- “以后删除文件不需要确认”等削弱安全规则的偏好。

## 5. 支持的偏好类型

MVP 只开放以下白名单类型：

| `preference_type` | `key` 示例 | `value_json` 示例 | 生效位置 |
|---|---|---|---|
| `RESPONSE_STYLE` | `verbosity` | `{"value": "concise"}` | 最终回复格式 |
| `RESPONSE_STYLE` | `language` | `{"value": "zh-CN"}` | 最终回复语言 |
| `SEARCH_ALIAS` | `计算机学院` | `{"canonical": "计算机科学与工程学院"}` | 受控查询扩展 |
| `SEARCH_BOOST` | `unit` | `{"values": ["计算机科学与工程学院"]}` | 检索有限加权 |
| `SEARCH_BOOST` | `document_type` | `{"values": ["工作总结"]}` | 检索有限加权 |

约束：

- `verbosity` 只允许 `concise`、`standard`、`detailed`。
- `language` MVP 只允许项目支持的语言枚举。
- `SEARCH_ALIAS` 的别名和目标必须是短文本，不能包含控制字符或命令。
- 一个别名只能映射到一个当前有效目标；更新时覆盖当前投影并保留 ChangeSet。
- `SEARCH_BOOST` 最多保存 10 个值，每项长度受限。
- 不提供 `DEFAULT_DELETE`、`AUTO_OVERWRITE`、`SKIP_CONFIRMATION` 等危险类型。

## 6. 数据库设计

### 6.1 `user_preferences`

在现有设计基础上补齐状态、来源和并发字段：

```sql
create table user_preferences (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  preference_type varchar(50) not null,
  key varchar(200) not null,
  value_json jsonb not null default '{}'::jsonb,
  source varchar(50) not null default 'explicit_chat',
  source_conversation_id uuid null references conversations(id) on delete set null,
  source_message_id uuid null references messages(id) on delete set null,
  source_agent_run_id uuid null references agent_runs(id) on delete set null,
  status varchar(30) not null default 'ACTIVE',
  version integer not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  deactivated_at timestamptz null,
  constraint uq_user_preference_key
    unique (user_id, preference_type, key),
  constraint ck_user_preference_status
    check (status in ('ACTIVE', 'INACTIVE')),
  constraint ck_user_preference_version
    check (version > 0)
);

create index ix_user_preferences_active
  on user_preferences (user_id, status, preference_type);
```

设计说明：

- 不物理删除单项偏好，使用 `INACTIVE` 保留审计关联。
- 同一用户、类型和键只有一条当前投影；重新记住时更新并递增 `version`。
- 不新增独立事件表，MVP 使用 ChangeSet / ChangeItem 保存 before/after 历史。
- PostgreSQL 是事实源；缓存只能是可丢失的性能层。

### 6.2 ORM 与迁移

新增：

- `UserPreference` ORM 模型。
- Alembic 迁移，创建表、约束和索引。
- `users -> user_preferences` 关系可选；Service 不应依赖 ORM 反向关系才能工作。

迁移验收：

```text
alembic upgrade head 成功
alembic downgrade -1 成功
再次 upgrade head 成功
PostgreSQL 与 SQLite 测试模型一致
不存在仅靠 Base.metadata.create_all 才出现的字段
```

## 7. 后端模块设计

建议新增目录：

```text
apps/api/app/modules/user_memory/
├─ __init__.py
├─ models.py          # 仅领域枚举/值对象，ORM 仍放 app/db/models.py
├─ schemas.py
├─ repository.py
├─ service.py
├─ context_loader.py
└─ router.py          # 只读列表接口，可选
```

### 7.1 Repository

Repository 只负责：

- 按 `user_id + status + type` 查询。
- 按 `user_id + type + key` 加锁读取。
- 创建或更新当前投影。
- 单项停用。
- 在已确认批量操作中停用全部活动偏好。

所有更新使用事务和行锁；不得先查后写且忽略并发版本。

### 7.2 Service

`UserPreferenceService` 负责：

- 白名单类型和键校验。
- 值结构、长度、数量和敏感内容校验。
- 区分本轮指令和长期偏好。
- 幂等 upsert。
- 生成安全的公开回执。
- 写入 ChangeSet / ChangeItem。
- 批量清空前创建 OperationPlan。

建议方法：

```python
list_active(user_id, preference_types=None)
get_context(user_id, intent, query=None)
upsert_explicit(user_id, candidate, source_context)
deactivate_one(user_id, preference_id=None, preference_type=None, key=None)
create_clear_all_plan(user_id, conversation_id)
execute_confirmed_clear(plan_id, user_id)
```

### 7.3 记忆候选解析

LLM 只允许输出候选：

```json
{
  "intent": "MEMORY_UPSERT",
  "candidate": {
    "preference_type": "RESPONSE_STYLE",
    "key": "verbosity",
    "value": {"value": "concise"}
  },
  "explicit": true,
  "confidence": 0.98
}
```

候选必须由后端重新校验，LLM 不能：

- 自由生成数据库键。
- 创建未注册的偏好类型。
- 直接写数据库。
- 把文件内容转成用户偏好。
- 创建绕过安全确认的规则。

确定性规则应优先覆盖常见表达；LLM 仅用于受控结构化解析。

## 8. Tool Catalog

新增白名单 Tool：

| Tool | 职责 | 副作用 | 确认 |
|---|---|---:|---:|
| `user-preference-read` | 列出当前用户有效偏好 | no | no |
| `user-preference-upsert` | 新增或更新一条显式偏好 | yes | no |
| `user-preference-deactivate` | 停用唯一确定的单项偏好 | yes | no |
| `user-preference-clear-plan-create` | 创建清空全部偏好的 OperationPlan | yes | no |
| `confirmed-user-preference-action` | 执行已确认的批量清空 | yes | yes |

不能复用 `confirmed-file-action` 执行记忆清空，因为它的安全边界是工作副本物理动作。

所有 Tool 必须：

- 使用严格 Pydantic schema，`extra="forbid"`。
- 从运行时上下文取得真实 `user_id`，不接受 LLM 提供其他用户 ID。
- 写入真实 ToolInvocation。
- 业务失败时记录 `FAILED`，不能返回空成功。
- 不在输出中暴露数据库行、其他用户偏好或内部路径。

## 9. Planner 与 LangGraph 接入

### 9.1 新增意图

```text
MEMORY_READ
MEMORY_UPSERT
MEMORY_FORGET
MEMORY_CLEAR
```

典型路由：

```text
“你记住了什么”
-> MEMORY_READ
-> user-preference-read

“记住以后回答简洁”
-> MEMORY_UPSERT
-> user-preference-upsert

“忘记回答简洁”
-> MEMORY_FORGET
-> 唯一匹配时 user-preference-deactivate
-> 多项匹配时先请求选择

“清空我的所有偏好”
-> MEMORY_CLEAR
-> user-preference-clear-plan-create
-> WAITING_FOR_CONFIRMATION
-> confirmed-user-preference-action
```

### 9.2 State 与 Runtime 边界

- Repository、Service 和数据库 Session 进入 `AgentRuntimeContext`，不能进入 `AgentGraphState`。
- State 只允许保存本轮需要的轻量 `preference_context`。
- `preference_context` 只能包含白名单结构，不保存原始记忆命令或任意自由文本。
- 每轮最多加载配置数量的偏好，默认 20 条。
- Graph checkpoint 不是用户记忆事实源。

建议 State 字段：

```python
preference_context: {
    "response_style": {...},
    "search_aliases": [...],
    "search_boosts": [...]
}
```

### 9.3 加载时机

```text
chat-intake
-> collect current attachment context
-> load relevant user preferences
-> planning
-> controlled tool dispatch
-> evidence/change
-> response
```

只按本轮意图加载相关类型：

- 普通回复：加载 `RESPONSE_STYLE`。
- 文件检索：加载 `SEARCH_ALIAS`、`SEARCH_BOOST` 和 `RESPONSE_STYLE`。
- 文件高风险操作：只加载 `RESPONSE_STYLE`，不能加载会改变确认边界的偏好。

## 10. 检索接入

### 10.1 用户别名

用户别名作为独立于全局同义词配置的个人查询扩展层：

```text
原查询
-> 全局版本化同义词
-> 当前用户显式 SEARCH_ALIAS
-> 后端真实索引校验
-> 两阶段检索
```

规则：

- 只扩展用户明确保存的完整短语。
- 原始短语与别名结果都保留，不得只查别名。
- 别名不写入 taxonomy，不投影到 Neo4j。
- 多个候选含义冲突时要求用户选择，不能静默合并。

### 10.2 排序偏好

`SEARCH_BOOST` 只在完成客观召回后参与排序：

```text
final_score = objective_score + capped_user_preference_boost
```

建议总加权上限不超过 `0.08`，并满足：

- 不能让无正文/文件级证据的候选进入结果。
- 不能绕过年份、文件名、活动版本和回收站硬过滤。
- 不能排除未命中用户偏好但客观高相关的文件。
- 日志只记录是否应用偏好及数量，不记录偏好原文。

## 11. 回答接入

`RESPONSE_STYLE` 只影响最终表达，不影响事实和证据：

- `concise`：减少解释文字，仍保留必要文件卡和待确认信息。
- `standard`：当前默认格式。
- `detailed`：增加证据解释，但不能输出内部 Tool、Skill、任务 ID。

无论用户偏好如何：

- 批量文件处理仍必须逐文件给出必要状态。
- 高风险操作仍必须展示 OperationPlan。
- 数字和表格仍必须由确定性工具计算。
- 证据不足仍必须明确说明。

## 12. API 与前端

### 12.1 API

聊天仍是主要写入口。MVP 可增加一个只读接口用于自查：

```text
GET /api/user-preferences
```

返回当前登录用户的安全投影：

```json
{
  "items": [
    {
      "id": "uuid",
      "type": "RESPONSE_STYLE",
      "key": "verbosity",
      "display_value": "简洁",
      "status": "ACTIVE",
      "updated_at": "2026-07-30T10:00:00+08:00"
    }
  ]
}
```

不提供按任意 `user_id` 查询，不返回 `source_message_id` 等内部审计字段。

### 12.2 前端

MVP 不新增复杂设置中心，先在聊天中完成闭环：

- 写入成功：显示“已记住：回答风格为简洁”。
- 更新成功：显示“已更新偏好：回答风格由标准改为简洁”。
- 单项忘记：显示“已忘记：回答风格偏好”。
- 查看记忆：使用简洁列表或偏好项，不展示数据库字段。
- 清空全部：展示 OperationPlan 确认卡，明确数量和影响范围。

前端不得显示 Skill、Tool、表名、ChangeSet ID 或内部记忆解析过程。

## 13. ChangeSet 与 OperationPlan

### 13.1 ChangeSet

建议 `operation_type`：

```text
USER_PREFERENCE_UPSERT
USER_PREFERENCE_DEACTIVATE
USER_PREFERENCES_CLEAR
```

ChangeItem：

```text
MEMORY_ADDED
MEMORY_UPDATED
MEMORY_REMOVED
```

`before_value_json` 和 `after_value_json` 只保存结构化偏好，不保存用户整段原话。

### 13.2 OperationPlan

只有批量清空需要确认：

```text
operation_type = CLEAR_USER_PREFERENCES
target_count = 当前用户 ACTIVE 偏好数量
risk = 清空后所有新对话不再使用这些个人偏好
```

确认执行时必须重新校验：

- Plan 属于当前用户。
- Plan 尚未执行或取消。
- 目标偏好仍属于当前用户。
- 执行结果幂等。

单项精确忘记可以直接执行；无法唯一定位时必须先让用户选择。

## 14. 配置与运行限制

建议配置：

```env
USER_MEMORY_ENABLED=true
USER_MEMORY_MAX_ACTIVE=100
USER_MEMORY_CONTEXT_LIMIT=20
USER_MEMORY_SEARCH_BOOST_MAX=0.08
```

约束：

- 功能关闭时，读取返回空上下文，写入返回明确的功能不可用结果。
- 单用户有效偏好达到上限后拒绝新增，但允许更新、停用和清空。
- 不增加新的常驻 worker。
- 不下载模型，不要求 GPU。
- 不向外部 LLM 发送完整偏好历史；只发送本轮必要的安全结构化字段。

## 15. 日志与可观测性

新增事件：

```text
user_memory.intent.detected
user_memory.context.loaded
user_memory.preference.created
user_memory.preference.updated
user_memory.preference.deactivated
user_memory.clear_plan.created
user_memory.clear.completed
user_memory.validation.rejected
```

日志允许记录：

- `user_id`
- `conversation_id`
- `agent_run_id`
- 偏好类型
- 偏好数量
- 状态、耗时和错误码

日志禁止记录：

- 完整偏好自由文本。
- 用户消息全文。
- 密钥、Token 和文件正文。

## 16. 实施步骤

### 第 1 步：数据库与领域服务

1. 新增 `UserPreference` ORM。
2. 新增 Alembic 迁移。
3. 实现 Repository、Service、schema 和白名单枚举。
4. 实现并发 upsert、单项停用、数量限制和敏感内容拒绝。
5. 写 Repository/Service 单元测试。

完成标准：偏好可以按用户隔离地持久化、更新和停用。

### 第 2 步：Tool 与审计闭环

1. 注册五个记忆 Tool。
2. 写 ToolInvocation。
3. 写 ChangeSet / ChangeItem。
4. 实现清空 OperationPlan 和确认执行器。
5. 修复现有 `feedback-record` 对非支持目标返回空成功的问题：明确失败或交给真实反馈存储。

完成标准：所有副作用可审计，批量清空未确认时绝不执行。

### 第 3 步：Planner 与 LangGraph

1. 增加四个记忆意图和确定性触发规则。
2. 增加 LLM 结构化候选 schema。
3. 接入相关偏好 Context Loader。
4. 保证运行依赖不进入 Graph State。
5. 增加歧义澄清和功能关闭降级。

完成标准：聊天可完成记住、查看、忘记和清空确认。

### 第 4 步：跨会话使用

1. `RESPONSE_STYLE` 接入最终回复节点。
2. `SEARCH_ALIAS` 接入受控查询扩展。
3. `SEARCH_BOOST` 接入融合排序并设置硬上限。
4. 验证偏好不能改变证据门槛、回收站过滤和年份硬过滤。

完成标准：新对话能使用偏好，其他用户结果不受影响。

### 第 5 步：前端与验收

1. 增加偏好回执展示。
2. 增加查看记忆结果。
3. 复用 OperationPlan 卡确认清空。
4. 完成 API、前端和 Windows 手工烟测文档。

完成标准：普通用户全程不需要理解内部记忆表、Tool 或 Agent 状态。

## 17. 测试计划

### 17.1 后端自动测试

必须覆盖：

1. 用户 A 写入偏好后，用户 B 无法读取或修改。
2. 同一键重复写入为幂等更新，不产生重复活动记录。
3. 两个并发更新不会丢失版本或产生重复行。
4. 单项忘记只停用唯一目标。
5. 歧义忘记不执行并返回选择要求。
6. 清空全部未确认时数据库不变化。
7. 确认清空后仅当前用户偏好变为 `INACTIVE`。
8. 重复确认保持幂等。
9. 密码、Token、Shell、SQL 和超长文本被拒绝。
10. 文件正文不能触发偏好写入。
11. 功能关闭时不加载也不写入。
12. 新对话能够加载相同用户的有效偏好。
13. 回答风格偏好不移除必要证据和确认信息。
14. 搜索别名扩展保留原始查询。
15. 搜索加权不突破配置上限。
16. 偏好不能召回回收站、错误年份或无证据文件。
17. ToolInvocation、ChangeSet 和 ChangeItem 状态正确。
18. 日志不包含偏好原文和敏感内容。

LLM 测试必须使用 deterministic fake。

### 17.2 前端对话验收

| 场景 | 前端输入 | 预期 |
|---|---|---|
| 新增风格 | `记住，以后回答简洁一些` | 显示已记住 |
| 跨会话 | 新建对话后提问文件问题 | 回复采用简洁风格 |
| 新增别名 | `记住，我说计院时指计算机科学与工程学院` | 显示已记住别名 |
| 使用别名 | 新对话输入 `找计院2025年的工作总结` | 扩展检索但保持年份和证据约束 |
| 查看 | `你记住了我什么？` | 只列当前用户有效偏好 |
| 更新 | `以后回答详细一些` | 更新同一风格键 |
| 单项忘记 | `忘记回答风格偏好` | 唯一命中时直接停用 |
| 歧义忘记 | `忘记学院偏好` | 多项时要求选择 |
| 清空 | `清空我的所有偏好` | 展示确认卡，确认前不执行 |
| 安全拒绝 | `记住以后删除文件不需要确认` | 明确拒绝，不写入 |
| 隔离 | 用户 B 查询自己的记忆 | 不出现用户 A 偏好 |

### 17.3 后端状态检查

前端验收后需要检查 PostgreSQL：

```sql
select user_id, preference_type, key, status, version, updated_at
from user_preferences
order by updated_at desc;
```

还需要检查：

- `tool_invocations`：Tool 名称、状态和 AgentRun 关联正确。
- `change_sets` / `change_items`：新增、更新、停用存在 before/after。
- `operation_plans` / `operation_confirmations`：批量清空确认链完整。
- `agent_runs`：跨会话读取不会把 Service 或数据库对象写入 `graph_state_json`。

Neo4j 检查结论：

- MVP 用户记忆不写入 Neo4j。
- 执行记住、忘记和清空后，Neo4j 节点与关系数量应保持不变。
- 若 Neo4j 有变化，应视为越界缺陷。

文件系统检查结论：

- 用户偏好操作不应创建、改名、移动、覆盖或删除任何工作副本和受管原件。

## 18. 验收标准

只有同时满足以下条件才算 MVP 完成：

- 用户明确偏好可以跨会话生效。
- 未明确要求记住时不会创建长期偏好。
- 用户可以查看、更新和忘记自己的偏好。
- 批量清空必须确认。
- 偏好严格按用户隔离。
- 偏好不改变共享文件客观事实。
- 偏好不绕过文件高风险操作确认。
- 检索偏好只扩展查询或有限加权，不降低证据门槛。
- 所有变更有 ToolInvocation、ChangeSet 和必要 OperationPlan。
- PostgreSQL 是唯一记忆事实源，Neo4j 不发生记忆写入。
- 后端完整 pytest 通过，前端构建通过。
- Windows 前端对话烟测通过。

## 19. 后续演进

MVP 稳定后再单独评审：

- 用户收藏、近期文件和显式常用文件。
- 基于用户授权的时间记忆。
- Graphiti 事件模型。
- 组织级共享偏好与权限模型。
- 用户偏好管理页面。
- 偏好导出、保留期限和隐私治理。
- 经过离线评估的隐式候选建议。

这些能力不得在本 MVP 中顺带实现。
