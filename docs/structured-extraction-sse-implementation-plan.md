# 图片结构化抽取 SSE 实施方案

## 1. 文档目的

本文档定义图片和扫描件结构化抽取任务的 SSE 实时进度能力。方案建立在现有
`STRUCTURED_EXTRACTION` 持久化队列、`filesystem_job_events`、PP-StructureV3、
动态字段抽取、Autonomous Loop、局部视觉增强和用户任务回执之上。

本阶段的目标是让用户在较长时间的图片识别过程中持续看到真实、可恢复的任务阶段，
同时保证图片识别仍在独立 Worker 中执行，不占用聊天请求执行推理，也不把未经校验的
字段值提前暴露给前端。

## 2. 设计结论

采用“任务进度 SSE + 最终结果一次性回执”，不在第一阶段实现未经校验字段值或 LLM
token 的增量输出。

```text
用户发送识别请求
-> Agent 创建 STRUCTURED_EXTRACTION 持久化任务
-> 消息接口立即返回 processing 回执
-> 前端使用 Bearer Token 建立 SSE
-> Worker 独立执行 PP-StructureV3 和 Autonomous Loop
-> Worker 使用短事务写入持久化进度事件
-> SSE 增量读取并推送脱敏事件
-> Worker 原子写回任务终态和 AgentRun 最终回执
-> 前端关闭 SSE，并读取一次最终会话回执
-> 按用户请求展示 TABLE / JSON / TEXT / CSV / XLSX
```

结构化结果必须在以下步骤全部完成后一次性展示：

1. PP-StructureV3 版面和文字识别。
2. 动态字段映射。
3. 金额、日期等确定性标准化。
4. 证据位置和字段置信度校验。
5. 受预算 Autonomous Loop。
6. 必要时的局部视觉增强。
7. 导出文件和最终用户回执生成。

不提前流式发送字段值的原因：

- Autonomous Loop 可能用局部视觉增强修正低置信度字段。
- 最终字段必须经过证据校验，不能把 LLM 候选直接展示为事实。
- 用户要求的 TABLE、JSON、TEXT 等输出格式应基于同一份最终事实生成。
- OCR 全文和字段原文可能包含敏感信息，不应进入通用任务进度通道。

## 3. 目标与非目标

### 3.1 目标

- 用户可以实时看到排队、版面识别、字段映射、校验、增强和导出阶段。
- 页面刷新或网络闪断后可以从最后一条事件继续。
- SSE 断开不影响后台任务执行。
- 单个用户长时间识别不会阻塞其他用户的聊天、检索和下载请求。
- 终态结果继续使用现有 `UserTaskReceipt` 安全投影。
- 所有用户可见事件都经过字段白名单和任务归属校验。

### 3.2 非目标

- 不把 PP-StructureV3 或 LLM 推理移动到 API 进程。
- 不在 SSE 中传输 OCR 全文、完整结构化结果或导出文件内容。
- 不生成无法从底层识别过程获得的虚假百分比。
- 不使用 SSE 替代 `AgentRun`、`FilesystemJob`、`ToolInvocation` 或 ChangeSet。
- 不通过 SSE 绕过最终回执、证据校验或文件下载权限。

## 4. API 设计

### 4.1 保留现有接口

```http
GET /api/jobs/{job_id}
GET /api/jobs/{job_id}/events
```

上述接口继续分别提供任务状态快照和持久化事件列表，作为页面刷新、诊断和 SSE 降级轮询的
事实来源。

### 4.2 新增 SSE 接口

```http
GET /api/jobs/{job_id}/events/stream
Accept: text/event-stream
Authorization: Bearer <access-token>
Last-Event-ID: <filesystem_job_event_id>
```

为了兼容不能方便设置 `Last-Event-ID` 的调用方，可同时接受：

```http
GET /api/jobs/{job_id}/events/stream?after_event_id=<event_id>
```

当 Header 和 Query 同时存在时，以 `Last-Event-ID` 为准。

响应头：

```http
Content-Type: text/event-stream
Cache-Control: no-cache, no-transform
X-Accel-Buffering: no
Connection: keep-alive
```

连接建立后先发送重试建议：

```text
retry: 2000
```

### 4.3 事件 Envelope

所有用户可见事件使用同一结构：

```json
{
  "schema_version": "job-stream-v1",
  "event_id": "event-uuid",
  "job_id": "job-uuid",
  "job_type": "STRUCTURED_IMAGE_EXTRACTION",
  "status": "RUNNING",
  "phase": "LAYOUT_ANALYSIS",
  "message": "正在识别版面、表格和文字区域",
  "progress": {
    "current": null,
    "total": null,
    "unit": "page",
    "determinate": false
  },
  "metrics": {},
  "warning": false,
  "terminal": false,
  "occurred_at": "2026-08-24T10:20:15Z"
}
```

SSE `event` 类型限制为：

- `snapshot`：连接建立或游标失效时发送当前安全快照。
- `progress`：非终态阶段变化。
- `warning`：不终止任务的降级或需关注事项。
- `completed`：任务成功终态。
- `needs_review`：任务完成但部分字段需复核。
- `failed`：任务失败终态。
- `resync`：游标不存在，需要客户端以当前快照重新同步。

示例：

```text
id: 3c735fe1-7d9f-4a21-9f28-c13f2e913951
event: progress
data: {"schema_version":"job-stream-v1","job_id":"...","status":"RUNNING","phase":"LAYOUT_ANALYSIS","message":"正在识别版面、表格和文字区域","progress":{"current":null,"total":null,"unit":"page","determinate":false},"terminal":false,"occurred_at":"..."}

: keepalive 2026-08-24T10:20:30Z
```

### 4.4 安全边界

SSE 数据不得包含：

- OCR 全文、字段原始文本或完整 LLM 响应。
- 文件宿主机绝对路径和内部存储路径。
- JWT、API key、模型请求头或外部服务凭证。
- Worker ID、租约所有者和内部队列诊断信息。
- Python 堆栈、数据库异常和第三方原始错误响应。
- 未经最终证据校验的字段候选值。

普通用户只能订阅 `created_by == current_user.id` 的任务。越权和不存在统一返回 404，避免通过
任务 ID 探测其他用户任务。`ops` 和 `admin` 可以按现有职责查看任务，但用户 SSE 仍只返回安全投影。

## 5. 阶段模型

后端使用稳定 `phase` 枚举，前端不能根据中文 `message` 推断状态。

| phase | 含义 | 是否可确定进度 |
|---|---|---|
| `QUEUED` | 已进入结构化抽取队列 | 否 |
| `WORKER_STARTED` | Worker 已领取任务 | 否 |
| `LAYOUT_ANALYSIS` | 正在执行版面、表格和文字识别 | 仅底层提供页进度时 |
| `LAYOUT_COMPLETED` | 版面识别完成 | 可返回页数和元素数 |
| `FIELD_MAPPING` | 正在按动态 schema 映射字段 | 否 |
| `NORMALIZATION` | 正在标准化金额、日期等字段 | 可返回字段数量 |
| `EVIDENCE_VALIDATION` | 正在校验证据位置和质量 | 可返回待复核数量 |
| `VISION_ENHANCEMENT` | 正在处理低置信度局部区域 | 可返回目标字段数量 |
| `EXPORTING` | 正在生成 CSV/XLSX 等派生件 | 否 |
| `RECEIPT_UPDATING` | 正在写回 AgentRun 最终回执 | 否 |
| `COMPLETED` | 任务完成 | 是 |
| `NEEDS_REVIEW` | 任务完成但有字段需要复核 | 是 |
| `FAILED` | 任务失败 | 是 |

PP-StructureV3 的单次推理不保证持续提供真实百分比。因此：

- 已知且真实完成某页时才更新 `current/total`。
- 无法观测时设置 `determinate=false`，前端展示阶段和已耗时。
- 不按照时间估算 30%、60%、90% 等虚假进度。
- SSE 服务独立发送 keepalive，keepalive 不改变业务进度。

局部视觉增强失败但初始结果可用时，发送 `warning`：

```json
{
  "phase": "VISION_ENHANCEMENT",
  "message": "局部增强未成功，已保留初始识别结果",
  "warning": true,
  "terminal": false
}
```

该情况不能把整个任务标记为失败。

## 6. 持久化事件与断线续传

### 6.1 复用现有事件表

继续使用 `filesystem_job_events` 作为任务进度事实，不引入只存在于内存的消息总线作为唯一来源。

为增量查询增加组合索引：

```text
(job_id, created_at, id)
```

第一阶段可以在新用户事件的 `details_json` 中保存受控字段：

```json
{
  "public": true,
  "event_code": "STRUCTURED_LAYOUT_STARTED",
  "phase": "LAYOUT_ANALYSIS",
  "progress_current": null,
  "progress_total": null,
  "progress_unit": "page",
  "metrics": {}
}
```

API 必须通过 Pydantic 安全模型重新投影，不能把 `details_json` 原样交给 SSE。

### 6.2 游标恢复

SSE 的 `id` 使用持久化 `FilesystemJobEvent.id`。恢复算法：

1. 在当前 `job_id` 范围内读取 `Last-Event-ID` 对应事件。
2. 以该事件的 `created_at + id` 作为复合游标。
3. 查询 `created_at` 更大，或时间相同且 `id` 更大的事件。
4. 继续按 `created_at ASC, id ASC` 返回。
5. 前端按事件 ID 去重。

不能直接使用随机 UUID 进行全局大小比较。

如果游标不存在：

1. 发送 `resync`。
2. 发送当前任务 `snapshot`。
3. 从当前可用事件窗口重新播放。

结构化抽取单任务事件数量应保持有限。服务端每次最多读取配置的批量条数，仍有剩余时立即读取下一批，
不得静默跳过事件。

## 7. 数据库会话与事务设计

### 7.1 Worker 进度发布必须独立提交

现有结构化识别会在 Worker 业务 Session 中执行较长时间。如果使用同一个事务写进度事件，事件可能要等
整个识别提交后才对 SSE 可见。

新增运行时服务：

```text
JobProgressPublisher
```

接口示意：

```python
publisher.publish(
    job_id=job.id,
    phase="LAYOUT_ANALYSIS",
    message="正在识别版面、表格和文字区域",
    progress=None,
    metrics={},
    warning=False,
)
```

每次发布：

```text
创建短 Session
-> 校验任务存在
-> 写入一条受控事件
-> commit
-> 关闭 Session
```

事件发布失败只写结构化日志，不撤销已经完成的识别事实，也不让识别任务失败。

### 7.2 SSE 不能长期占用请求 Session

SSE 连接可能持续数分钟，不能把 `get_db` 产生的普通请求 Session 持有到连接关闭。

实现要求：

- 建连阶段完成 token、用户和任务归属校验。
- 捕获的只允许是 `user_id`、`role`、`job_id` 等标量，不捕获 ORM 对象。
- 建连鉴权依赖使用 function scope，在返回 `StreamingResponse` 前关闭数据库 Session。
- 每轮读取通过 session factory 创建短 Session 并立即关闭。
- 当前项目使用同步 SQLAlchemy，SSE 的数据库读取通过线程池运行，不能阻塞 FastAPI 事件循环。
- 每轮等待使用异步 sleep，并定期检查 `request.is_disconnected()`。

## 8. 后端组件改造

### 8.1 通用任务 SSE 服务

建议增加：

```text
apps/api/app/modules/managed_files/job_stream.py
```

职责：

- 读取任务安全快照。
- 根据复合游标增量读取事件。
- 将内部事件投影为 `job-stream-v1`。
- 生成 SSE wire format。
- 发送 heartbeat。
- 在终态事件全部发送后关闭连接。
- 记录连接建立、恢复、断开和异常日志，不记录正文或 token。

现有路由增加 `/api/jobs/{job_id}/events/stream`，原 REST 接口保持兼容。

### 8.2 进度发布器

建议增加：

```text
apps/api/app/modules/managed_files/job_progress.py
```

包括：

- `JobProgressReporterProtocol`。
- `NoopJobProgressReporter`。
- `PersistentJobProgressPublisher`。
- 阶段枚举和用户事件 Pydantic schema。
- 受控 `metrics` 字段白名单。

`StructuredExtractionService` 默认注入 No-op Reporter；只有 Worker 构造服务时注入持久化发布器。
测试可以注入 deterministic fake reporter。

### 8.3 结构化抽取埋点

在以下真实边界写事件：

- 任务被结构化抽取 Worker 领取。
- PP-StructureV3 调用前后。
- 动态字段映射调用前后。
- 确定性标准化完成。
- 证据和质量评估完成。
- Autonomous Loop 判断是否需要局部增强。
- 局部增强开始、完成、跳过或失败保留初始结果。
- CSV/XLSX 派生件生成完成。
- AgentRun 最终回执更新。
- 任务进入成功、需复核或失败终态。

事件仅包含页数、元素数量、字段数量、记录数量、待复核数量、质量等级等安全统计。

## 9. 前端设计

### 9.1 使用 fetch 读取 SSE

当前项目使用 Bearer Token。原生 `EventSource` 不能可靠携带 `Authorization` Header，因此使用
`fetch + ReadableStream + TextDecoder`。

建议增加：

```text
apps/web/src/api/jobStream.ts
apps/web/src/features/chat/StructuredExtractionProgress.tsx
```

流客户端职责：

- 携带 `Authorization: Bearer ...`。
- 解析跨网络分块的 SSE 帧和多行 `data`。
- 保存最后成功处理的事件 ID。
- 使用 `AbortController` 主动关闭。
- 按事件 ID 去重。
- 区分服务端业务失败和网络中断。
- 采用有限指数退避重连。

### 9.2 ChatPage 状态更新

收到 `task_status=processing` 且有 `pending_job_ids` 时：

1. 为当前聊天 turn 建立 SSE。
2. 将进度保存在独立的 UI state，不改写正式 `TaskResult` 事实。
3. 根据 `phase` 更新进度卡。
4. 收到终态后关闭流。
5. 查询一次当前会话详情，取得 Worker 已写回的最终 `UserTaskReceipt`。
6. 用最终回执替换同一条消息，不创建重复聊天消息。

页面刷新时，历史消息仍为 `processing` 就重新建立 SSE。切换会话、退出登录或组件卸载时取消连接。

### 9.3 降级策略

以下情况回退到现有轮询：

- 浏览器不支持流式 `fetch`。
- 代理没有正确透传 SSE。
- SSE 在有限次数重连后仍失败。
- 服务端返回明确的 SSE 功能关闭状态。

降级轮询只查询任务状态或在终态时查询一次会话，不应继续每 1.5 秒下载整份会话历史。

## 10. 并发与资源隔离

SSE 只读取任务事件，不执行 PP-StructureV3 或 LLM：

- API 进程维护轻量异步连接。
- 推理由 `STRUCTURED_EXTRACTION` Worker 执行。
- 一个用户长时间识别不会占用其他用户的聊天请求线程。
- SSE 连接断开不会取消后台任务。
- 一个结构化 Worker 同时只能执行其配置并发数内的任务，其他图片任务仍会排队。

部署建议：

- CPU 部署根据内存设置 1 至 2 个结构化 Worker。
- GPU 部署通常一张 GPU 对应一个 Worker 进程，避免多个模型副本耗尽显存。
- 多 Worker 继续通过 PostgreSQL `SKIP LOCKED` 安全领取任务。
- 增加单用户排队和运行任务上限，避免单一用户占满识别队列。
- API、通用 Worker 和结构化抽取 Worker 分进程运行。

SSE 能改善用户可见性，但不会增加推理吞吐量；任务吞吐仍由 Worker 数量、CPU/GPU 和模型耗时决定。

## 11. 配置与部署

建议增加配置：

```env
JOB_SSE_ENABLED=true
JOB_SSE_POLL_INTERVAL_MS=1000
JOB_SSE_HEARTBEAT_SECONDS=15
JOB_SSE_MAX_DURATION_SECONDS=1800
JOB_SSE_BATCH_SIZE=100
```

配置校验：

- 轮询间隔设置合理下限，避免数据库高频空查询。
- heartbeat 必须小于常见代理空闲超时。
- 单连接最大时长达到后发送可重连事件并正常关闭。
- 批量条数必须设置上限。

Nginx 或等价代理需要为该路由关闭响应缓冲：

```nginx
location ~ ^/api/jobs/.+/events/stream$ {
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
    proxy_pass http://file_agent_api;
}
```

如果使用网关或 CDN，还必须确认其不会合并 SSE 数据块或缓存 `text/event-stream` 响应。

## 12. 日志与可观测性

增加以下不含敏感数据的结构化日志：

- `job.sse.connected`
- `job.sse.resumed`
- `job.sse.disconnected`
- `job.sse.terminal_sent`
- `job.sse.failed`
- `structured_extraction.progress_published`
- `structured_extraction.progress_publish_failed`

建议指标：

- 当前 SSE 连接数。
- SSE 平均连接时长。
- 每分钟重连次数。
- 事件增量查询耗时。
- 结构化抽取排队时间和执行时间。
- 每个阶段耗时。
- 局部视觉增强触发率和失败保留率。

日志不得记录 token、OCR 全文、字段值、绝对路径或完整事件 data。

## 13. 测试方案

### 13.1 后端测试

- 用户只能订阅本人任务，越权统一返回 404。
- ops/admin 权限符合现有任务权限边界。
- 响应 Content-Type、Cache-Control 和 X-Accel-Buffering 正确。
- 初次连接先收到安全快照和后续事件。
- `Last-Event-ID` 能正确恢复且不重复发送已确认事件。
- 相同时间戳事件按 `created_at + id` 稳定排序。
- 游标不存在时发送 `resync`，不会静默丢失状态。
- heartbeat 不携带业务数据。
- 客户端断开后生成器和数据库轮询停止。
- 任务终态事件全部发送后连接自动关闭。
- Worker 主事务未完成时，独立发布的阶段事件已经对 SSE 可见。
- 进度发布失败不会让图片识别失败。
- 局部增强失败发送 warning 并保留初始结果。
- SSE 数据不包含路径、正文、字段值、Worker ID 或内部堆栈。
- 慢速 deterministic fake PP Provider 执行时，健康检查和其他 API 仍可响应。

### 13.2 前端测试

- 正确解析被任意网络分块拆开的 SSE 帧。
- 正确处理 CRLF、多行 data、注释 heartbeat 和空行。
- 重连携带最后事件 ID，并对重复事件去重。
- 页面卸载、切换会话和退出登录会 abort 连接。
- 终态只刷新一次最终会话回执。
- SSE 失败后回退轮询。
- 多个聊天 turn 的进度不会互相覆盖。
- 最终 TABLE、JSON、TEXT 和下载卡仍使用现有结构化抽取回执。

### 13.3 集成和负载验证

- 使用慢速 fake Provider 验证识别期间持续收到阶段事件。
- 同时发起聊天、检索和健康检查，验证不被长识别阻塞。
- 模拟代理断开和重连，验证事件恢复。
- 模拟 50 至 200 个轻量 SSE 连接，观察 API 事件循环、数据库连接池和查询量。
- 验证 PostgreSQL 组合索引被增量事件查询使用。

## 14. 实施顺序

### 阶段一：契约和持久化读取

1. 增加 SSE Envelope、阶段枚举和安全投影模型。
2. 增加事件复合索引迁移。
3. 增加按游标读取事件的 repository 方法。
4. 补齐用户事件 details 的白名单投影。

### 阶段二：SSE 后端

1. 实现短 Session 的 `JobStreamService`。
2. 实现鉴权、heartbeat、断线检测和终态关闭。
3. 增加 `/api/jobs/{job_id}/events/stream`。
4. 保持现有 REST 状态和事件接口兼容。

### 阶段三：Worker 真实阶段事件

1. 实现 `JobProgressReporterProtocol` 和持久化 Publisher。
2. 在结构化识别服务中注入 Reporter。
3. 在 PP-StructureV3、字段映射、校验、增强和导出真实边界发布事件。
4. 保证发布使用独立短事务。

### 阶段四：前端接入

1. 实现带 Bearer Token 的 SSE fetch 客户端。
2. 增加结构化识别进度卡。
3. 用 SSE 替代结构化抽取任务的整会话高频轮询。
4. 保留终态单次刷新和轮询降级。

### 阶段五：验证和文档

1. 完成后端、前端和集成测试。
2. 更新 `.env.example`、README 和运行手册。
3. 增加代理关闭缓冲的部署说明。
4. 验证完整迁移链和回滚路径。

## 15. 验收标准

满足以下条件才视为完成：

- 图片识别请求能立即返回 processing 回执。
- 识别期间前端持续显示真实阶段或连接 heartbeat。
- 识别推理不在 API/SSE 协程中执行。
- 页面刷新和断线重连不会丢失已持久化事件。
- 终态结构化结果只从最终 AgentRun 安全回执读取。
- SSE 事件不泄漏文件正文、路径、字段值和内部诊断信息。
- 单个慢识别任务不阻塞其他用户普通 API 请求。
- SSE 不可用时用户仍可通过轮询得到最终结果。
- 后端全量测试、前端测试、前端构建和 Alembic head 检查全部通过。

