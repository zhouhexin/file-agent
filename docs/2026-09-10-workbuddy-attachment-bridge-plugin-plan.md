# WorkBuddy 会话附件到 File Agent MCP 的插件适配方案

## 1. 结论

仅在 `~/.workbuddy/mcp.json` 中注册 File Agent MCP，能够让 WorkBuddy 发现和调用工具，但不会把“用户在
输入框上传附件”自动转换成 `workbuddy_attachment_ingest` 的参数。当前需要增加一个 WorkBuddy 侧插件，
在 `UserPromptSubmit` 阶段识别本轮已提交附件，生成本机可信清单，并只把一个不透明的提交引用注入给
Agent；File Agent MCP 再通过新工具读取该清单并执行导入。

推荐链路如下：

```text
用户在 WorkBuddy 中上传附件并发送任务文字
-> WorkBuddy UserPromptSubmit Hook
-> Hook 从本轮 transcript 中识别附件，生成只读提交清单
-> Hook 向 Agent 上下文注入 submission_ref，不注入绝对路径
-> Skill 根据用户意图选择 File Agent
-> Agent 调用 workbuddy_submission_ingest(submission_ref, user_request)
-> MCP 校验清单、附件缓存根、时效、文件快照和幂等键
-> 复用现有 AttachmentTransferService 和后端批次导入流程
-> WorkBuddy 展示 batch_get / job_get 的逐文件回执
```

这能实现自然语言调用，例如用户直接上传文件并说“把这些材料类归档”，而不需要手写
`submission_id`、`attachment_id` 或 `local_path`。

## 2. 当前环境与代码事实

### 2.1 已有能力

当前项目已经暴露 `workbuddy_attachment_ingest`，输入为：

```json
{
  "submission_id": "稳定的本轮提交 ID",
  "attachments": [
    {
      "attachment_id": "稳定附件 ID",
      "filename": "原始文件名.pdf",
      "local_path": "WorkBuddy 本机缓存文件绝对路径"
    }
  ],
  "user_request": "用户本轮任务文字"
}
```

MCP 已经可以执行以下工作：

- 校验提交 ID、附件 ID 和安全文件名。
- 限制附件必须位于 `FILE_AGENT_WORKBUDDY_ATTACHMENT_ROOTS` 授权根内。
- 拒绝符号链接、目录、特殊文件和根外路径。
- 计算真实大小、mtime 和 SHA-256，冻结清单。
- 复用现有批次创建、清单 seal、文件上传、查重、分类、命名、落位和索引链路。
- 使用稳定提交 ID 生成幂等键，避免相同消息被重复导入。

### 2.2 当前本机配置缺项

当前 `C:\Users\zhouhexin\.workbuddy\mcp.json` 的 `file-agent` 配置已经包含 API 地址、访问令牌、
本地目录根和传输状态目录，但还没有：

```text
FILE_AGENT_WORKBUDDY_ATTACHMENT_ROOTS
```

所以即使 WorkBuddy 成功调用现有附件工具，也会返回“WorkBuddy 附件入口未配置授权缓存根”。

### 2.3 WorkBuddy 5.5.3 的附件落盘结构

本机 WorkBuddy 会话记录中的已提交用户附件使用如下内容块：

```json
{
  "type": "image_blob_ref",
  "blob_id": "内容标识",
  "mime": "image/jpeg",
  "size": 12345,
  "blob_path": "C:\\Users\\...\\.workbuddy\\blobs\\xx\\<sha256>.jpg",
  "original_filename": "用户上传时的文件名.jpg"
}
```

其中 `blob_path` 的 basename 是内容哈希，通常不等于 `original_filename`。当前
`WorkBuddyAttachmentRegistry.resolve()` 强制要求：

```text
resolved.name == filename
```

因此不能简单地把 `blob_path` 和 `original_filename` 原样交给现有工具。适配层必须选择以下一种方式：

1. 把 blob 原子复制到插件私有提交目录，并恢复为安全的原始文件名；或
2. 新增“可信提交清单”入口，把“缓存物理名称”和“原始逻辑名称”明确分开。

推荐第 2 种，避免为大文件额外复制一次。

### 2.4 WorkBuddy Hook 的公开边界

官方 `UserPromptSubmit` Hook 输入目前只有 `session_id`、`transcript_path`、`cwd`、
`permission_mode`、`hook_event_name` 和 `prompt`，没有正式的 `attachments` 字段。

本机 transcript 中确实能看到附件内容块，因此可以做本机试点插件，但读取 transcript 内部附件结构属于
兼容性适配，不应被视为长期稳定的 WorkBuddy 公共 API。正式发布前应推动 WorkBuddy 提供以下任一能力：

- 在 `UserPromptSubmit` Hook 中直接提供稳定附件清单；
- 提供“本轮消息附件”Host API；
- 允许宿主在消息提交事件上直接绑定一个 MCP Tool；
- 为本轮附件提供 MCP Roots/Resource，并保留稳定附件 ID 和原文件名。

## 3. 为什么不能只增加 Skill

Skill 可以解决“AI 什么时候应该选择 File Agent”问题，例如识别以下自然语言：

- “把这些文件归档。”
- “读取并分类我刚上传的材料。”
- “OCR 这些扫描件并标出不清楚的页。”
- “总结这份表格后存入 File Agent。”

但 Skill 本身不能凭空获得可信的 `submission_id`、`attachment_id` 和 `local_path`。如果让模型猜参数，会
违反本项目的附件授权边界，也会导致路径错误。因此必须由确定性的 WorkBuddy Hook/宿主适配器先生成清单，
Skill 只负责意图路由和调用顺序。

## 4. 推荐实现：Hook + 可信清单 + 新 MCP Tool

### 4.1 插件目录

建议在项目中新增：

```text
integrations/workbuddy/file-agent-plugin/
├─ .workbuddy-plugin/
│  └─ plugin.json
├─ hooks/
│  └─ hooks.json
├─ scripts/
│  ├─ capture_submission.py
│  └─ validate_submission_call.py
├─ skills/
│  └─ file-agent/
│     └─ SKILL.md
├─ README.md
└─ CHANGELOG.md
```

试点阶段不要在插件中再次注册一份同名 MCP Server，因为用户的全局 `mcp.json` 已经注册了
`file-agent`。等插件准备分发给其他用户时，再把 MCP 启动配置打包进插件或制作正式 Connector，避免
“全局配置一份、插件又启动一份”的重复工具问题。

### 4.2 plugin.json

建议声明插件元数据、Skill、Hook 和两个用户配置项：

```json
{
  "name": "file-agent-workbuddy-bridge",
  "version": "0.1.0",
  "description": "把 WorkBuddy 本轮已提交附件安全转交给 File Agent，并提供自然语言文件任务路由。",
  "author": {"name": "File Agent Team"},
  "skills": "./skills/",
  "hooks": "./hooks/hooks.json",
  "userConfig": {
    "python_executable": {
      "description": "运行 File Agent 适配脚本的 Python 可执行文件",
      "sensitive": false
    },
    "workbuddy_home": {
      "description": "WorkBuddy 数据目录，Windows 通常为 C:\\Users\\用户名\\.workbuddy",
      "sensitive": false
    }
  }
}
```

正式分发时访问令牌应通过 `sensitive: true` 的用户配置或 WorkBuddy Connector 的 Token/OAuth 机制保存，
不能写进插件源码或市场包。

### 4.3 hooks.json

```json
{
  "description": "捕获用户真正提交的 WorkBuddy 消息附件，并向 Agent 注入不透明提交引用。",
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"${user_config.python_executable}\" \"${CODEBUDDY_PLUGIN_ROOT}/scripts/capture_submission.py\"",
            "timeout": 10
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "mcp__.*file.agent.*__workbuddy_submission_ingest",
        "hooks": [
          {
            "type": "command",
            "command": "\"${user_config.python_executable}\" \"${CODEBUDDY_PLUGIN_ROOT}/scripts/validate_submission_call.py\"",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

Windows 上 WorkBuddy 的 command Hook 由 Git Bash 执行，脚本调用和路径引用必须按 Bash 语法编写；业务
逻辑放在 Python 中，避免在 Bash 中解析 JSON 或 Windows 路径。

### 4.4 capture_submission.py 职责

脚本从 stdin 读取 Hook JSON，但不能相信 `prompt` 能表达附件。它应当：

1. 校验 `hook_event_name == "UserPromptSubmit"`。
2. 取得 `session_id`、`transcript_path` 和本轮 `prompt`。
3. 确认 transcript 位于配置的 WorkBuddy 数据目录内，拒绝任意外部 JSONL。
4. 读取 transcript 最后一条与当前 `session_id`、当前 prompt 对应的 `role=user` 消息。
5. 只接受已知的附件内容块类型，并提取 `blob_id`、`blob_path`、`original_filename`、`mime`、`size`。
6. 校验 `blob_path` 位于 `<workbuddy_home>/blobs/`，文件不是符号链接且是普通文件。
7. 不读取文件正文；只记录文件 stat 和提交元数据。
8. 使用消息 `id` 作为稳定 `submission_id`；使用 `blob_id` 作为 `attachment_id`。
9. 把清单原子写入 `${CODEBUDDY_PLUGIN_DATA}/submissions/<submission_ref>.json`。
10. stdout 只输出 Hook JSON，把 `submission_ref`、附件数量和原文件名注入 Agent 上下文，不输出
    `blob_path`。

注入内容示例：

```json
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "FILE_AGENT_HOST_SUBMISSION ref=wbsub_v1_xxx count=2 filenames=[申请表.xlsx,证明.pdf]。这是宿主生成的本轮已提交附件清单。若用户要求 File Agent 读取、OCR、分类、归档、整理或入库，必须调用 file-agent 的 workbuddy_submission_ingest；不得自行构造附件路径或替换 submission_ref。"
  }
}
```

没有附件时，脚本应安静返回 `continue=true`，不注入 File Agent 路由信息。只选择附件但没有点击发送时，
`UserPromptSubmit` 不会执行，因此不会产生授权。

### 4.5 可信清单格式

```json
{
  "schema_version": "file_agent_workbuddy_submission.v1",
  "submission_ref": "wbsub_v1_随机或签名引用",
  "submission_id": "WorkBuddy 消息 ID",
  "session_id": "WorkBuddy 会话 ID",
  "created_at": "2026-09-10T10:00:00+08:00",
  "prompt_sha256": "本轮任务文字摘要",
  "attachments": [
    {
      "attachment_id": "WorkBuddy blob_id",
      "original_filename": "申请表.xlsx",
      "cache_path": "C:\\Users\\...\\.workbuddy\\blobs\\ab\\<hash>.xlsx",
      "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "expected_size": 123456,
      "expected_mtime_ns": 1234567890
    }
  ]
}
```

清单文件必须仅保存在本机插件数据目录，不能上传给模型或 File Agent 后端。MCP 读取后仍需重新校验路径、
stat 和 SHA-256，不能把 Hook 清单当成文件事实。

### 4.6 新 MCP Tool

建议新增：

```python
async def workbuddy_submission_ingest(
    submission_ref: str,
    user_request: str | None = None,
    placement_mode: Literal["BY_CATEGORY", "NEUTRAL"] = "BY_CATEGORY",
    rule_profile: Literal["content_based", "legacy_school_materials"] = "content_based",
) -> dict[str, Any]:
    ...
```

该工具的关键点：

- 模型只传 `submission_ref` 和用户任务，不传附件列表或路径。
- `submission_ref` 必须匹配固定格式，不能含 `/`、`\\` 或 `..`。
- MCP 只从 `FILE_AGENT_WORKBUDDY_BRIDGE_STATE_DIR` 查找清单。
- 清单必须是普通文件、不是符号链接，并且在允许时效内，例如 10 分钟。
- 清单里的 `cache_path` 必须位于 `FILE_AGENT_WORKBUDDY_ATTACHMENT_ROOTS`。
- `cache_path` 的物理 basename 可以是哈希名；`original_filename` 单独按安全文件名校验。
- 在上传前重新校验 size、mtime 和 SHA-256，文件变化则返回 `SOURCE_CHANGED`。
- 继续调用现有 `AttachmentTransferService`，不能另写一套后端上传和批次状态机。
- `submission_id` 继续决定后端幂等键；同一消息重试应恢复同一批次。
- 清单在成功创建/恢复批次后标记为已受理，但不能立即删除；需要保留到可恢复期限结束。
- MCP 输出不能包含 WorkBuddy 的绝对缓存路径。

现有 `workbuddy_attachment_ingest` 可以保留，供真正能够由宿主直接提供结构化参数的客户端使用；新工具是
WorkBuddy 插件桥接入口，不应放宽旧工具的任意路径读取边界。

### 4.7 PreToolUse 二次校验

`validate_submission_call.py` 只做快速、确定性检查：

- 工具名必须是新的提交引用工具。
- `submission_ref` 必须存在于插件数据目录。
- 本次调用的 `submission_ref` 必须是当前会话最近一次未过期提交。
- 模型不得传 `attachments`、`local_path` 或其他未声明字段。
- `user_request` 应来自当前用户 prompt；允许做空白规范化，但不能替换用户目标。

真正的安全边界仍在 MCP 内部。Hook 不是后端授权替代品，因为插件可能被停用或被其他 MCP 客户端绕过。

## 5. Skill 的自然语言路由规则

Skill 应明确以下触发范围。

### 5.1 有本轮附件时调用 File Agent

附件存在，并且用户要求下列任一任务时调用新工具：

- 导入、入库、归档、保存到 File Agent。
- 读取、解析、OCR、识别扫描件。
- 分类、归类、整理、标准化命名。
- 总结、讲解、提取字段、表格分析，同时要求文件进入 File Agent。
- 查重或判断这批附件是否已经存在。

### 5.2 没有本轮附件时

- “找去年奖学金材料”使用 `file_search`。
- “读取搜索结果中的这份文件”使用 `file_read`。
- “根据这些已入库文件回答问题”使用 `evidence_answer`。
- 明确改名使用 `file_rename`，随后按 OperationPlan 确认。
- 不得调用附件导入工具，不得把工作目录文件猜成本轮附件。

### 5.3 不调用 File Agent 的情况

- 用户上传附件但只要求 WorkBuddy 临时查看，明确表示不要归档或不要进入 File Agent。
- 用户只在输入框选择附件但没有提交消息。
- Hook 没有生成本轮 `submission_ref`。
- 用户的问题与附件无关。
- 附件清单歧义、过期、被替换或位于未授权根外。

遇到这些情况时，应解释当前边界或请求用户重新提交，不得退化为模型手写路径。

## 6. 本机试点步骤

### 第一步：先做只读 Hook 探针

建立最小插件，Hook 只输出本次事件字段名、最后一条用户消息 ID、附件内容块类型和附件数量，不输出正文、
绝对路径或凭证。用 PDF、DOCX、XLSX、图片各提交一次，确认非图片附件是否也提供
`blob_id/blob_path/original_filename`。

如果当前消息在 Hook 执行时还没有写入 transcript，立即停止本方案的 transcript 适配实现，不要通过 sleep
或扫描整个 `~/.workbuddy` 猜测附件；应改走 WorkBuddy 官方宿主 API 或专用文件选择入口。

### 第二步：实现可信清单与 MCP 新工具

完成清单 schema、原子写入、TTL、路径校验、内容寻址文件名兼容、幂等恢复和错误码。MCP 单元测试必须使用
临时目录，不读取真实 `~/.workbuddy`。

### 第三步：增加 Skill

Skill 只描述自然语言触发条件、工具顺序、失败恢复和安全边界。它不解析 transcript、不访问文件系统，也不
生成 attachment ID。

### 第四步：开发模式加载插件

先使用官方支持的临时开发加载：

```powershell
codebuddy --plugin-dir "E:\PycharmProject\file-agent\integrations\workbuddy\file-agent-plugin"
```

验证插件后，再建立本地 marketplace 并安装到用户作用域，不能直接编辑
`~/.workbuddy/plugins/installed_plugins.json`：

```powershell
codebuddy plugin marketplace add "<本地 marketplace 目录>" --name file-agent-local
codebuddy plugin install file-agent-workbuddy-bridge@file-agent-local --scope user
codebuddy plugin list --json
```

插件和 Hook 在新会话启动时加载；修改后需要重启 WorkBuddy/CodeBuddy 会话。

### 第五步：补齐 MCP 环境变量

本机原型至少需要新增两个配置：

```json
{
  "FILE_AGENT_WORKBUDDY_ATTACHMENT_ROOTS": "[\"C:\\\\Users\\\\zhouhexin\\\\.workbuddy\\\\blobs\"]",
  "FILE_AGENT_WORKBUDDY_BRIDGE_STATE_DIR": "C:\\Users\\zhouhexin\\.workbuddy\\plugins\\data\\file-agent-workbuddy-bridge"
}
```

实际 `BRIDGE_STATE_DIR` 必须以 WorkBuddy 为该插件解析出的 `${CODEBUDDY_PLUGIN_DATA}` 为准，不能照抄示例猜
目录。修改 `mcp.json` 后需要完全重启 WorkBuddy，使 MCP 进程取得新环境变量。

### 第六步：端到端验收

测试话术：

```text
（上传 2 个文件）请用 File Agent 读取并分类这些文件。
（上传扫描 PDF）请 OCR 后归档，并告诉我哪些页不清楚。
（上传 Excel）请整理所有工作表，生成摘要并存入 File Agent。
（不上传文件）请在 File Agent 中找去年的奖学金材料。
```

验收时检查：

1. 用户不需要输入任何 ID 或路径。
2. Hook 只在点击发送后生成提交清单。
3. Agent 调用的是 `workbuddy_submission_ingest`，参数只有提交引用和受控策略。
4. MCP 日志不打印附件正文、令牌或绝对缓存路径。
5. 后端收到逻辑来源和文件字节，不收到 WorkBuddy 物理路径。
6. 同一消息重试不产生第二个批次。
7. 替换缓存文件后重试返回 `SOURCE_CHANGED`。
8. 根外路径、符号链接、过期清单、伪造引用均被拒绝。
9. `batch_get` 返回逐文件处理状态；重命名后区分导入快照名和当前名。
10. 明确说“不要归档”时不调用导入工具。

## 7. 其他用户试用时的部署要求

如果 File Agent 后端继续运行在开发者电脑，而其他用户在同一网段的电脑上使用 WorkBuddy：

- 后端电脑运行 API、数据库依赖和 Worker，并监听局域网地址，而不是只监听 `127.0.0.1`。
- 每个测试用户电脑都需要安装/启用 WorkBuddy 插件，因为 Hook 必须读取该用户自己的本地提交事件。
- 每个测试用户电脑都需要运行本地 MCP 进程或安装包含 MCP 的正式插件/Connector；后端电脑上的 MCP 不能
  读取其他电脑的 WorkBuddy 本地附件缓存。
- 每台用户电脑的 MCP API 地址应指向后端电脑，例如 `http://10.102.4.241:8000`，不能使用
  `http://127.0.0.1:8000`。
- 每台用户电脑配置自己的 `FILE_AGENT_WORKBUDDY_ATTACHMENT_ROOTS`、桥接状态目录和访问令牌。
- 访问令牌必须按用户签发，不能多人共用开发者令牌。
- Windows 防火墙只放行内网测试网段访问 API 端口，不应把 PostgreSQL、Redis 或本地存储目录直接暴露给
  测试用户。

因此答案是：需要更新其他人电脑上的 WorkBuddy 插件和 MCP 配置。只有后端代码部署在开发者电脑；附件
捕获、可信清单和本地文件读取必须发生在附件所属的用户电脑上。

## 8. 不能承诺的边界

- 仅靠 MCP Tool description 或 Skill，不能保证附件提交后必然自动调用工具。
- 普通 Hook 不能通过公开输入直接取得附件；当前 transcript 适配依赖 WorkBuddy 5.5.3 的本机结构，需要
  版本兼容测试。
- 模型路由仍可能失败；如果要求“每次附件提交 100% 自动入库”，应让 Hook/本地桥接进程直接创建批次，
  而不是等待模型决定是否调用 MCP。该模式的用户授权文案和取消机制需要单独设计。
- 不能让插件扫描整个下载目录或 WorkBuddy 数据目录并把最近文件当作本轮附件。
- 不能把 `blob_path` 注入模型、发送到后端、写入聊天回执或普通日志。
- 不能直接修改 WorkBuddy 的 `installed_plugins.json`、会话数据库或附件缓存内容。

## 9. 推荐交付顺序

1. P0：只读 Hook 探针，验证 PDF/DOCX/XLSX/图片的实际 transcript 契约。
2. P1：可信清单 schema、MCP 新工具、缓存物理名与原文件名分离。
3. P2：自然语言 Skill、PreToolUse 校验、错误回执。
4. P3：本机端到端测试和重启/重试/过期测试。
5. P4：制作本地 marketplace，给同网段 1～2 台电脑试装。
6. P5：联系 WorkBuddy 团队确认正式附件 Host API，再决定是否提交插件/Connector 市场审核。

## 10. 参考资料

- WorkBuddy 官方插件系统：<https://cloud.tencent.com/document/product/1831/137027>
- WorkBuddy 官方插件 API：<https://cloud.tencent.com/document/product/1831/137036>
- WorkBuddy 官方 Hook 配置：<https://cloud.tencent.com/document/product/1831/137030>
- WorkBuddy 任务对话与文件上传：<https://www.workbuddy.cn/docs/workbuddy/Conversation>
- WorkBuddy Connector 开放平台：<https://open.workbuddy.cn/docs/connector>
