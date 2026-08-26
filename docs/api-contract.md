# File Agent API Contract

本文定义 File Agent MVP 的 HTTP API 契约。项目定位以 `agent.md` 为准：`/chat` 是任务型文件智能体入口，`evidence-answer` 是一个 Skill，不是主入口。

## 1. General Rules

### 1.1 Base URL

```text
/api
```

### 1.2 Authentication

除注册、登录、健康检查外，所有接口都需要 JWT。

```http
Authorization: Bearer <access_token>
```

JWT payload 至少包含：

```json
{
  "user_id": "uuid",
  "role": "user"
}
```

### 1.3 Roles

```text
user:
- 使用 /chat
- 新建会话
- 发送文件工作指令
- 上传文件
- 查看自己的 UserTaskReceipt、文件结果、引用和 OperationPlan
- 确认自己的 OperationPlan
- 提交反馈

admin / ops:
- 查看 AgentRun、ToolInvocation 和 ChangeSet 审计
- 查看文件处理状态
- 触发重新解析/重新索引
- 处理反馈
- 配置模型
```

### 1.4 Error Envelope

MVP 可以直接返回业务 JSON，不强制包裹 `code/data`。错误统一返回：

```json
{
  "error": {
    "code": "BAD_REQUEST",
    "message": "Invalid request",
    "details": null,
    "request_id": "request-uuid"
  }
}
```

约束：

- `code` 是稳定的机器可读错误码；普通 HTTPException 未声明业务码时使用下方通用码。
- `message` 是可以展示给用户的安全信息，不得包含堆栈、服务器绝对路径、密钥或文件正文。
- `details` 可选，只保存字段校验或业务选择所需的结构化信息。
- `request_id` 与 `X-Request-ID` 响应头一致，供 admin/ops 关联 JSONL 日志。
- FastAPI 参数校验、认证失败、业务 HTTPException 和未捕获异常都必须使用该 Envelope，不再返回
  顶层 `detail`。前端滚动升级期间可以兼容旧 `detail`，完成部署后应移除兼容分支。

### 1.5 Common Error Codes

```text
400 BAD_REQUEST
401 UNAUTHORIZED
403 FORBIDDEN
404 NOT_FOUND
409 CONFLICT
422 VALIDATION_ERROR
500 INTERNAL_ERROR
```

## 2. Public APIs

### 2.1 Health

```text
GET /api/health
```

Response:

```json
{
  "status": "ok",
  "knowledge_graph": {
    "status": "disabled",
    "reason": "GRAPH_DISABLED",
    "graphrag_package": "not_installed"
  }
}
```

`knowledge_graph` 是可选增强状态。图谱关闭或不可用不会把 API 总体健康状态改为失败；启用后应根据
该字段诊断连接和 `neo4j-graphrag-python` 安装状态。

## 3. Auth APIs

### 3.1 Register

```text
POST /api/auth/register
```

Request:

```json
{
  "username": "zhangsan",
  "password": "password123",
  "display_name": "张三",
  "email": "zhangsan@example.com"
}
```

Response:

```json
{
  "id": "user-uuid",
  "username": "zhangsan",
  "email": "zhangsan@example.com",
  "display_name": "张三",
  "role": "user",
  "default_workspace_id": "workspace-uuid"
}
```

### 3.2 Login

```text
POST /api/auth/login
```

Request:

```json
{
  "username": "zhangsan",
  "password": "password123"
}
```

Response:

```json
{
  "access_token": "jwt-token",
  "token_type": "bearer",
  "user": {
    "id": "user-uuid",
    "username": "zhangsan",
    "email": "zhangsan@example.com",
    "display_name": "张三",
    "role": "user",
    "default_workspace_id": "workspace-uuid"
  }
}
```

### 3.3 Current User

```text
GET /api/auth/me
```

Response:

```json
{
  "id": "user-uuid",
  "username": "zhangsan",
  "email": "zhangsan@example.com",
  "display_name": "张三",
  "role": "user",
  "default_workspace_id": "workspace-uuid"
}
```

### 3.4 Logout

```text
POST /api/auth/logout
```

Current MVP status:

```text
not implemented yet; client can drop token locally
```

Response:

```json
{
  "ok": true
}
```

## 4. Workspace APIs

### 4.1 Get Default Workspace

```text
GET /api/workspace/default
```

Behavior:

```text
if current user has default_workspace_id, return it
otherwise create default workspace and workspace_members row
```

Response:

```json
{
  "id": "workspace-uuid",
  "name": "Default Workspace",
  "description": "",
  "is_default": true,
  "owner_id": "user-uuid",
  "stats": {
    "documents": 12,
    "ready_documents": 10,
    "open_feedback": 2
  },
  "llm_configured": true
}
```

## 5. Conversation APIs

### 5.1 List Conversations

```text
GET /api/conversations
```

Query:

```text
status=active
limit=50
cursor=可选
```

Response:

```json
{
  "items": [
    {
      "id": "conversation-uuid",
      "title": "资助材料整理",
      "status": "active",
      "created_at": "2026-06-24T08:00:00Z",
      "updated_at": "2026-06-24T08:10:00Z"
    }
  ],
  "next_cursor": null
}
```

### 5.2 Create Conversation

```text
POST /api/conversations
```

Request:

```json
{
  "title": "资助材料整理"
}
```

Response:

```json
{
  "id": "conversation-uuid",
  "workspace_id": "workspace-uuid",
  "title": "资助材料整理",
  "status": "active",
  "created_at": "2026-06-24T08:00:00Z",
  "updated_at": "2026-06-24T08:00:00Z"
}
```

### 5.3 Get Conversation

```text
GET /api/conversations/{conversation_id}
```

Status: implemented for authenticated user-owned conversations. The current response is optimized for frontend refresh recovery.

Response:

```json
{
  "id": "conversation-uuid",
  "user_id": "user-uuid",
  "title": "资助材料整理",
  "status": "active",
  "messages": [
    {
      "id": "message-uuid",
      "conversation_id": "conversation-uuid",
      "user_id": "user-uuid",
      "role": "user",
      "content": "帮我读取并分类这批文件。",
      "attachments": [
        {
          "document_id": "document-uuid",
          "filename": "材料.pdf",
          "content_type": "application/pdf",
          "size_bytes": 1024,
          "sha256": "sha256",
          "status": "USED_IN_MESSAGE",
          "ingest_status": "INGESTED",
          "deduplicated": false
        }
      ],
      "task_result": {
        "task_id": "opaque-task-uuid",
        "task_status": "completed",
        "response_type": "file_results",
        "operation_plan_id": null,
        "final_response": "已处理 1 个文件：...",
        "document_results": [],
        "managed_file_result": null,
        "rename_plan_result": null,
        "pending_job_ids": [],
        "pending_decisions": [],
        "references": [],
        "suggested_next_actions": ["继续查找相关文件", "询问文件中的具体内容"]
      }
    }
  ]
}
```

### 5.4 Update Conversation

```text
PUT /api/conversations/{conversation_id}
```

Request:

```json
{
  "title": "新的会话标题",
  "status": "active"
}
```

### 5.5 Send Message To Agent

```text
POST /api/conversations/{conversation_id}/messages
```

This is the primary `/chat` entrypoint.

Authentication:

```http
Authorization: Bearer <access_token>
```

Request:

```json
{
  "content": "帮我读取并分类刚上传的文件。",
  "attachments": [
    {
      "document_id": "document-uuid"
    }
  ]
}
```

Behavior:

```text
save user message
create agent_runs row
start LangGraph run
if LLM_ENABLED=true: call LLM to create structured UserIntentPlan
if uploaded file insights already exist: reuse document_insights through read-document-insights
if original text extraction is required: run extract-document-text and persist document_results in agent_runs.graph_state_json
if classification suggestions are produced: persist structured suggestions in document_classification_runs and document_category_suggestions
persist tool_invocations for ops/admin audit
project the internal run into UserTaskReceipt
return message and task_result without Skill, Tool, Planner, AgentRun or host path payloads
```

`tool_invocations.status` follows the structured Tool business result: `ok=false` or `status=FAILED` is persisted as `FAILED`.

Response:

```json
{
  "message": {
    "id": "message-uuid",
    "conversation_id": "conversation-uuid",
    "user_id": "user-uuid",
    "role": "user",
    "content": "帮我读取并分类刚上传的文件。",
    "attachments": [
      {
        "document_id": "document-uuid"
      }
    ]
  },
  "task_result": {
    "task_id": "opaque-task-uuid",
    "task_status": "completed",
    "response_type": "file_results",
    "final_response": "已处理 1 个文件，原始文件保持不变。",
    "processed_count": 1,
    "document_results": [],
    "managed_file_result": null,
    "rename_plan_result": null,
    "file_search_result": null,
    "trash_restore_result": null,
    "pending_job_ids": [],
    "operation_plan_id": null,
    "pending_decisions": [],
    "references": [],
    "suggested_next_actions": ["继续查找相关文件", "询问文件中的具体内容"],
    "presentation": null
  }
}
```

阶段一、阶段二开始，搜索、受管目录列举、文件读取、文件总结、证据回答和表格分析会同时返回
`presentation`。该字段是版本化的公共展示外壳；专用明细仍由 `file_search_result`、
`managed_file_result`、`document_results` 或 `evidence_answer_result` 提供，前端不得从
`presentation` 反推 Tool、物理路径或文件正文。

受管目录列举结果同时返回安全定位键和业务展示名称：

```json
{
  "managed_file_result": {
    "root_key": "school_files",
    "root_display_name": "学校文件库",
    "files": []
  }
}
```

`root_key` 仅用于后续受控预览或文件操作定位；普通用户界面的范围和目录标题应优先展示
`root_display_name`。两者都不得包含服务器物理路径。

未指定单一受管根目录、需要跨根目录列举时，`root_key` 为 `null`，
`root_display_name` 为“全部受管目录”；每个文件仍携带自己的安全 `root_key + relative_path`。

```json
{
  "presentation": {
    "schema_version": "file-task-receipt.v1",
    "task_kind": "SEARCH",
    "title": "文件查找结果",
    "phase": {
      "code": "COMPLETED",
      "label": "处理完成"
    },
    "request": {
      "target_label": "相关文件",
      "scope_label": "学校",
      "action_label": "查找相关文件",
      "conditions": [
        {
          "label": "主题",
          "value": "工作总结",
          "condition_type": "topic",
          "status": "APPLIED"
        }
      ]
    },
    "outcome": {
      "headline": "找到 8 个明确相关文件，另有 4 个可能相关文件",
      "total_count": 12,
      "completed_count": 8,
      "failed_count": 0,
      "needs_review_count": 4,
      "skipped_count": 0,
      "completeness": "COMPLETE"
    },
    "change_impact": {
      "originals_changed": false,
      "working_copies_changed": false,
      "derivatives_created": 0,
      "operation_executed": false,
      "message": "本次只进行了文件查找，原文件和工作副本均未改变。"
    },
    "notices": [],
    "next_actions": [
      {
        "id": "refine-search",
        "label": "继续筛选",
        "action_kind": "FILL_PROMPT",
        "prompt": "请按年份、单位或文件类型继续筛选这些结果",
        "target_ref": null,
        "requires_confirmation": false
      }
    ]
  }
}
```

`presentation` 的安全规则：

- 数量、范围、完整性和文件变化状态必须由后端确定性生成，不能由前端或 LLM 猜测。
- `FILL_PROMPT` 只允许把建议写入输入框，不得自动发送。
- `OPEN_FILE` 必须继续经过文件预览接口鉴权。
- 高风险文件动作仍只能由已持久化、已确认归属的 OperationPlan 执行。
- 没有 `presentation` 的历史消息继续按原有 `response_type` 展示。

When LLM is enabled and the user asks for uploaded-file summary or basic file information, the same endpoint may return:

```json
{
  "task_result": {
    "task_id": "opaque-task-uuid",
    "task_status": "completed",
    "response_type": "text",
    "final_response": "已读取 1 个文件的基础洞察：student.txt。"
  }
}
```

When LLM is enabled and the user asks to read original content, parse PDF/Excel, or OCR an image, the same endpoint may return:

Current `extract-document-text` supports `txt/md/csv/xls/xlsx/doc/docx/pdf/image`. Legacy `.doc/.xls` files are converted by LibreOffice into versioned persistent `CONVERTED_DOCX/CONVERTED_XLSX` artifacts before downstream parsers read them; valid artifacts are reused across extraction and spreadsheet tools without modifying the original file.

```json
{
  "task_result": {
    "task_id": "opaque-task-uuid",
    "task_status": "needs_attention",
    "response_type": "file_results",
    "final_response": "已处理 2 个文件：1 个完成，1 个失败。",
    "processed_count": 2,
    "document_results": [
      {
        "document_id": "document-uuid",
        "filename": "student.txt",
        "extraction_status": "COMPLETED",
        "page_count": 1,
        "char_count": 1200,
        "categories": [],
        "warnings": [],
        "errors": []
      }
    ]
  }
}
```

The per-file structured result is persisted in `agent_runs.graph_state_json.document_results` for run snapshot and receipt generation. Category suggestions are calculated from full `document_pages.text_content`, not the 300-character `text_preview`. Category suggestions are also persisted in `document_classification_runs` and `document_category_suggestions`; they remain suggestions and are not official `document_categories` until user confirmation. The same run creates a real `change_sets` row and `change_items` rows for `TEXT_EXTRACTED`, `DOCUMENT_PAGES_CREATED`, `CATEGORY_SUGGESTED`, and `DOCUMENT_PROCESSING_FAILED`. When an existing successful extraction is reused, ChangeSet records `TEXT_REUSED`, `DOCUMENT_PAGES_REUSED`, and `CATEGORY_SUGGESTION_REUSED`; users can force a new extraction by saying “重新解析 / 重新读取 / 重新处理 / 重跑”. Legacy Office conversion additionally records `DOCX_DERIVATIVE_CREATED/REUSED` or `XLSX_DERIVATIVE_CREATED/REUSED`. “重新解析” keeps a valid derivative, while “重新转换” sets both `force_reprocess` and `force_reconvert`.

```json
[
  {
    "document_id": "document-uuid",
    "filename": "student.txt",
    "extraction_status": "COMPLETED",
    "extractor": "plain-text",
    "page_count": 1,
    "char_count": 1200,
    "categories": [
      {
        "name": "学校/人事师资/职称",
        "category_path": ["学校", "人事师资", "职称"],
        "confidence": 0.72,
        "status": "SUGGESTED",
        "evidence": ["职称"],
        "evidence_items": [
          {
            "type": "text_quote",
            "page_number": 1,
            "sheet_name": null,
            "quote": "本文件涉及教师职称申报材料。",
            "signals": ["职称"],
            "source": "rule"
          }
        ],
        "taxonomy_key": "school_file_classification",
        "taxonomy_version": "2026-07-v3"
      },
      {
        "name": "学校/党委相关/干部工作",
        "category_path": ["学校", "党委相关", "干部工作"],
        "confidence": 0.7,
        "status": "SUGGESTED",
        "evidence": ["干部工作"],
        "evidence_items": [
          {
            "type": "text_quote",
            "page_number": 1,
            "sheet_name": null,
            "quote": "材料同时包含干部工作和会议纪要。",
            "signals": ["干部工作"],
            "source": "rule"
          }
        ],
        "taxonomy_key": "school_file_classification",
        "taxonomy_version": "2026-07-v3"
      }
    ],
    "warnings": [],
    "errors": []
  },
  {
    "document_id": "document-uuid-2",
    "filename": "broken.pdf",
    "extraction_status": "FAILED",
    "extractor": "extract-document-text",
    "page_count": 0,
    "char_count": 0,
    "categories": [],
    "warnings": [],
    "errors": [
      {
        "code": "TOOL_EXECUTION_FAILED",
        "message": "不支持的文件类型"
      }
    ]
  }
]
```

Errors:

```text
403 not owner
404 conversation not found
422 invalid attachment document ids
```

## 6. AgentRun APIs

本节接口属于内部运行审计面，只允许 `ops` 和 `admin` 访问。普通用户消息接口只返回
`task_result` 用户任务投影，不返回 Planner、Skill、ToolInvocation 或原始 Tool 输出。

### 6.1 Get AgentRun

```text
GET /api/agent-runs/{agent_run_id}
```

Response:

```json
{
  "agent_run_id": "agent-run-uuid",
  "conversation_id": "conversation-uuid",
  "user_id": "user-uuid",
  "message_id": "message-uuid",
  "intent": "CLASSIFY_FILES",
  "status": "COMPLETED",
  "selected_skills": ["chat-intake", "document-text-extract", "document-classification", "change-report"],
  "tool_plan": {
    "intent": "CLASSIFY_FILES",
    "user_goal": "读取并分类刚上传的文件",
    "steps": [
      {
        "step_id": "step-extract-1",
        "skill": "document-text-extract",
        "tool_name": "extract-document-text",
        "requires_confirmation": false,
        "risk_level": "low",
        "expected_outputs": ["document_pages", "extraction_run"]
      }
    ]
  },
  "tool_results": [],
  "tool_invocations": [],
  "changeset_id": "changeset-uuid",
  "operation_plan_id": null,
  "final_response": "已处理 3 个文件，原件未变更。",
  "errors": []
}
```

### 6.2 List Agent Tools

```text
GET /api/agent/tools
```

Response:

```json
{
  "tools": [
    {
      "name": "extract-document-text",
      "description": "解析文件并持久化当前版本页面文本和结构",
      "side_effects": true,
      "requires_confirmation": false,
      "allowed_roles": ["user", "ops", "admin"],
      "writes": ["document_pages", "artifacts", "change_items"]
    },
    {
      "name": "confirmed-file-action",
      "description": "执行已确认的改名、移动、复制、导出等动作",
      "side_effects": true,
      "requires_confirmation": true,
      "allowed_roles": ["user", "ops", "admin"],
      "writes": ["documents", "artifacts", "change_items"]
    }
  ]
}
```

MVP Tool names:

```text
chunk-build
read-document-insights
read-document-classifications
read-original-file
extract-document-text
hybrid-search
evidence-answer
classification-decision
working-copy-action-plan-create
confirmed-file-action
feedback-record
managed-root-scan
```

### 6.3 List Tool Invocations

```text
GET /api/agent-runs/{agent_run_id}/tool-invocations
```

Response:

```json
{
  "tool_invocations": [
    {
      "id": "tool-invocation-uuid",
      "tool_name": "extract-document-text",
      "status": "COMPLETED",
      "input_json": {
        "document_id": "document-uuid"
      },
      "output_json": {
        "pages": 3
      },
      "changeset_id": "changeset-uuid",
      "operation_plan_id": null
    }
  ]
}
```

## 7. Document APIs

### 7.1 List Documents

```text
GET /api/documents
```

Query:

```text
conversation_id=可选
status=可选
limit=50
cursor=可选
```

Response:

```json
{
  "items": [
    {
      "id": "document-uuid",
      "title": "国家励志奖学金申请表.pdf",
      "original_filename": "国家励志奖学金申请表.pdf",
      "file_ext": "pdf",
      "mime_type": "application/pdf",
      "size_bytes": 102400,
      "status": "READY",
      "conversation_id": "conversation-uuid",
      "created_at": "2026-06-24T08:00:00Z"
    }
  ],
  "next_cursor": null
}
```

### 7.2 Upload Document

```text
POST /api/files/upload
```

Content-Type:

```text
multipart/form-data
```

Form fields:

```text
file: required
```

Current MVP behavior:

```text
accept any uploaded file
format whitelist and virus scanning will be added in later ingest phase
```

Behavior:

```text
save original file to FILE_STORAGE_ROOT
create documents row
create file_objects row
return document id for message attachments
file remains deletable until it is sent in a conversation message
Document-level reuse only applies to an identical, same-name, status=UPLOADED draft
same content with a different name, a locked Document, or a managed snapshot creates a new draft Document
physical content may still be deduplicated through a shared FileObject
```

Response:

```json
{
  "document_id": "document-uuid",
  "filename": "国家励志奖学金申请表.pdf",
  "content_type": "application/pdf",
  "size_bytes": 102400,
  "sha256": "hex-sha256",
  "status": "UPLOADED",
  "ingest_status": "INGESTED",
  "deduplicated": false
}
```

### 7.3 Read Uploaded Document Content

```text
GET /api/files/{document_id}/content
```

Current behavior:

```text
requires authenticated request
document_id must exist and Document.user_id must match the current user
returns original file stream from FILE_STORAGE_ROOT
used by frontend attachment click-to-preview/download
```

Response:

```text
200 original file stream
Content-Type = document.content_type
Content-Disposition = attachment filename
```

### 7.4 Delete Uploaded Document

```text
DELETE /api/files/{document_id}
```

Behavior:

```text
only owner can delete
only status=UPLOADED can be deleted
delete file_objects row
delete local storage file
delete documents row
return 409 if document already entered a message
shared physical content is removed only when no other FileObject references the same storage path
```

Response:

```json
{
  "deleted": true
}
```

### 7.4 Get Document

```text
GET /api/documents/{document_id}
```

Response:

```json
{
  "id": "document-uuid",
  "title": "国家励志奖学金申请表.pdf",
  "original_filename": "国家励志奖学金申请表.pdf",
  "file_ext": "pdf",
  "mime_type": "application/pdf",
  "size_bytes": 102400,
  "status": "READY",
  "conversation_id": "conversation-uuid",
  "versions": [
    {
      "id": "version-uuid",
      "version_no": 1,
      "parse_status": "COMPLETED",
      "created_at": "2026-06-24T08:00:00Z"
    }
  ],
  "artifacts": [
    {
      "id": "artifact-uuid",
      "artifact_type": "EXTRACTED_TEXT",
      "mime_type": "application/json"
    }
  ]
}
```

### 7.4 Download Document

```text
GET /api/documents/{document_id}/download
```

Response:

```text
binary file stream
```

### 7.5 List Document Chunks

```text
GET /api/documents/{document_id}/chunks
```

Response:

```json
{
  "document_id": "document-uuid",
  "document_version_id": "version-uuid",
  "status": "COMPLETED",
  "embedding_status": "DISABLED",
  "chunk_count": 1,
  "evidence_count": 1,
  "chunks": [
    {
      "chunk_id": "chunk-uuid",
      "chunk_index": 0,
      "chunk_type": "page",
      "char_count": 31,
      "token_count": 12,
      "page_start": 1,
      "page_end": 1,
      "sheet_name": null,
      "cell_range": null,
      "evidence_count": 1
    }
  ]
}
```

该接口只返回当前用户文档的安全定位元数据。`text_content`、`search_text`、`search_vector`、
`embedding` 和服务器路径不属于普通用户响应；完整 quote 只能在后续 EvidenceValidator 校验后作为回答引用
展示。没有索引时返回 `status=NOT_INDEXED` 和空 `chunks`，不能伪造索引成功。

### 7.6 Get Document Lineage

```text
GET /api/documents/{document_id}/lineage
```

Response:

```json
{
  "document_id": "document-uuid",
  "versions": [
    {
      "id": "version-uuid",
      "version_no": 1,
      "storage_key": "originals/document/version/original.pdf"
    }
  ],
  "artifacts": [
    {
      "id": "artifact-uuid",
      "artifact_type": "EXTRACTED_TEXT",
      "derived_from_version_id": "version-uuid"
    }
  ],
  "relations": []
}
```

## 8. Search API

### 8.1 CPU-only Two-stage File Search

```text
POST /api/search
```

Request:

```json
{
  "query": "贫困生补助怎么申请？",
  "conversation_id": null,
  "attachment_document_ids": [],
  "top_k": 8
}
```

Behavior:

```text
parse query with Jieba and deterministic rules
resolve L0 current attachments / L1 conversation files / L4 active workspace files on the server
recall a bounded set of current working copies from document_search_profiles
use PostgreSQL simple GIN as primary lexical recall; use normalized filename pg_trgm only as bounded fallback
when document recall is insufficient, make one bounded document_chunks lexical fallback recall
search chunks only inside the bounded candidate versions, then validate Evidence and permissions
merge deterministically and return user-safe file cards
```

聊天入口识别出“机构范围 × 文件主题”等双条件交集检索时，两个条件必须各自完成
无文件数量上限的受控召回后再按稳定文件 ID 求交集，不能先各截取 30 份候选。
这不取消普通单条件检索的候选上限，也不取消 Chunk 详情/证据限制；交集结果超过
20 份时仍返回结果数量确认卡，用户确认“全部展示”后才展示完整文件列表。

`hybrid-search` 可选接收严格 `semantic_plan`。LLM 只能声明完整 `core_topics`、明确机构短语、
机构层级偏好和分组字段；后端必须逐项执行完整短语召回，并对所有必需条件求文件级交集。
任何摘要降级仍需验证完整受保护短语，不得用 n-gram 中的“工作”等局部词替代“工作总结”。
结构化计划触发的完整短语检索同样在最终交集或结果整理前保留完整文件级候选集合。

Response:

```json
{
  "query": "贫困生补助怎么申请？",
  "total_returned": 1,
  "partial": false,
  "search_completeness": {
    "status": "COMPLETE",
    "can_claim_complete": true,
    "scope_label": "当前共享工作区全部活动文件",
    "eligible_file_count": 36,
    "ready_file_count": 36,
    "pending_file_count": 0,
    "failed_file_count": 0,
    "candidate_limit_reached": false,
    "message": "已完成当前共享工作区全部活动文件中 36 份活动文件的检索；当前条件下结果已找全。"
  },
  "user_message": "",
  "files": [
    {
      "document_id": "document-uuid",
      "document_version_id": "version-uuid",
      "filename": "2025年资助政策.pdf",
      "category_path": ["学生工作", "资助"],
      "overview": "资助申请的材料与时间说明。",
      "match_reasons": ["文件名命中：2025年资助政策.pdf", "原文 Chunk 命中查询词"],
      "match_location": {"page_number": 2, "sheet_name": null, "cell_range": null},
      "evidence_preview": "申请国家助学金需要提交申请表和相关证明材料。"
    }
  ]
}
```

此接口和聊天入口均不调用 embedding、GPU、LLM、Graph 或文件系统扫描；不会返回 Chunk 正文、
`search_text`、内部路径、SQL 分数或 Tool/Skill 载荷。`attachment_document_ids` 仅作为后端再次
鉴权的稳定 ID 输入，`top_k` 范围为 1–20。

`search_completeness` 由后端根据实际检索范围和当前索引状态计算，前端不得根据返回文件数量自行
推断“已找全”。`COMPLETE` 只表示当前唯一范围、检索条件和索引能力下不存在已知缺口；`PROCESSING`
表示有活动文件尚在准备检索，`PARTIAL` 表示索引降级、失败或候选上限导致结果可能不完整，
`UNVERIFIABLE` 表示附件或查找范围尚未被唯一确认。它不代表系统能够证明所有业务语义上的相关文件。

### 8.2 File Search Clarification

```text
GET  /api/file-search/clarifications/{clarification_id}
POST /api/file-search/clarifications/{clarification_id}/resolve
```

当原短语、同义完整短语或宽泛主题产生不同候选集合时，聊天入口先返回选择卡。前端只能提交
后端签发的 `option_id`；自定义短语最多 30 个字符，不能直接提交 Tool 参数或短语数组。后端必须
校验当前用户、状态和过期时间，同一选择重复提交时复用首次生成的消息与 AgentRun，不得重复回答。
GET 接口用于页面刷新后恢复最新状态，已解决或已过期的卡片不得重新显示为待选择。

## 9. Evidence Answer Skill API

### 9.1 Ask Evidence-Backed Question

```text
POST /api/conversations/{conversation_id}/evidence-answer
```

Request:

```json
{
  "question": "国家励志奖学金申请流程是什么？",
  "attachment_document_ids": []
}
```

Behavior:

```text
create or reuse AgentRun depending on caller
retrieve relevant chunks
call chat model using evidence-answer Skill
save qa_answers
save answer_references
reuse the AgentRun user-task projection instead of inserting a duplicate assistant message
return answer, compact file references, and safe excerpts of the evidence actually cited by the final answer
```

Response:

```json
{
  "message": {
    "id": "user-message-uuid",
    "role": "user",
    "content": "国家励志奖学金申请流程是什么？"
  },
  "task_result": {
    "task_id": "agent-run-uuid",
    "task_status": "completed",
    "response_type": "evidence_answer",
    "final_response": "申请流程包括提交申请表、学院审核和学校复核。",
    "evidence_answer_result": {
      "answer_id": "answer-uuid",
      "status": "COMPLETED",
      "answer": "申请流程包括提交申请表、学院审核和学校复核。",
      "files": [
        {
          "document_id": "document-uuid",
          "document_version_id": "version-uuid",
          "working_copy_id": "working-copy-uuid",
          "filename": "国家励志奖学金申请表.pdf",
          "category_labels": ["奖助学金 / 国家励志奖学金"],
          "reference_indexes": [1],
          "availability": "AVAILABLE",
          "availability_message": "文件可用",
          "can_open": true,
          "can_restore": false,
          "evidence_items": [
            {
              "quote": "申请人应提交国家励志奖学金申请表，并由学院审核后报送学校。",
              "page_number": 2,
              "sheet_name": null,
              "cell_range": null
            }
          ]
        }
      ],
      "limitations": [],
      "cached": false
    }
  }
}
```

`evidence_items` 只包含本次最终结论已经写入 `answer_references` 的受限原文片段，供普通用户
核对答案。每项最多 320 个字符；只允许返回 `quote`、`page_number`、`sheet_name` 和 `cell_range`，
不得返回 EvidenceSpan/Chunk ID、内部检索分数、文件系统路径或未被最终结论引用的召回正文。

完整 Evidence quote、Chunk ID、内部检索分数和完整正文保存在数据库引用与文件预览中，不在普通聊天
消息载荷重复返回；普通回执只返回受限片段及必要的页码、工作表或单元格定位。精确文件名命中回收站时返回 `trash_restore_selection`；同名不同内容候选返回
持久化 `file_selection`，用户提交后端签发的 `option_id` 后才继续回答。加载历史会话时后端重新
投影引用文件当前状态；已进入回收站的文件返回 `availability=TRASHED`、`can_open=false`，前端不得
继续打开旧正文。

No evidence response:

```json
{
  "message": {
    "id": "user-message-uuid",
    "role": "user"
  },
  "task_result": {
    "task_status": "needs_attention",
    "response_type": "evidence_answer",
    "final_response": "没有找到能够支持回答的原文证据。",
    "evidence_answer_result": {
      "answer_id": null,
      "status": "NO_EVIDENCE",
      "answer": "没有找到能够支持回答的原文证据。",
      "files": [],
      "limitations": [],
      "cached": false
    }
  }
}
```

### 9.2 Compatibility Alias

```text
POST /api/conversations/{conversation_id}/qa
```

This endpoint may call the same implementation as `evidence-answer` for backward compatibility. New frontend code must use `/messages` or `/evidence-answer`.

## 10. ChangeSet APIs

### 10.1 Get ChangeSet

```text
GET /api/changesets/{changeset_id}
```

该接口属于内部审计面，只允许 `ops`、`admin` 访问；普通用户通过消息中的 `task_result` 查看任务回执。

Response:

```json
{
  "id": "changeset-uuid",
  "conversation_id": "conversation-uuid",
  "agent_run_id": "agent-run-uuid",
  "user_id": "user-uuid",
  "status": "COMPLETED",
  "summary": "已处理 1 个文件，生成 3 项变更记录。",
  "created_at": "2026-06-28T08:00:00Z",
  "updated_at": "2026-06-28T08:00:00Z",
  "items": [
    {
      "id": "change-item-uuid",
      "target_type": "document",
      "target_id": null,
      "target_document_id": "document-uuid",
      "change_type": "CATEGORY_SUGGESTED",
      "before_value_json": {},
      "after_value_json": {
        "category_name": "学校/人事师资/职称",
        "confidence": 0.72,
        "status": "SUGGESTED"
      },
      "source": "rule",
      "confidence": 0.72,
      "evidence_json": {
        "evidence": ["职称"],
        "evidence_items": [
          {
            "type": "text_quote",
            "page_number": 1,
            "sheet_name": null,
            "quote": "本文件涉及教师职称申报材料。",
            "signals": ["职称"],
            "source": "rule"
          }
        ],
        "taxonomy_key": "school_file_classification",
        "taxonomy_version": "2026-07-v3"
      },
      "execution_status": "COMPLETED",
      "created_at": "2026-06-28T08:00:00Z"
    }
  ]
}
```

## 11. OperationPlan APIs

### 11.1 Create OperationPlan

```text
POST /api/operations/plans
```

Request:

```json
{
  "conversation_id": "conversation-uuid",
  "operation_type": "MOVE_WORKING_COPIES",
  "reason": "生成标准化文件名建议",
  "items": [
    {
      "working_copy_id": "working-copy-uuid",
      "after": {
        "relative_path": "奖助学金/2025-计算机学院-张三-国家励志奖学金申请表.pdf"
      }
    }
  ]
}
```

Response:

```json
{
  "id": "operation-plan-uuid",
  "status": "WAITING_CONFIRMATION",
  "operation_type": "MOVE_WORKING_COPIES",
  "requires_confirmation": true,
  "risk_level": "medium",
  "items": [
    {
      "document_id": "working-copy-document-uuid",
      "working_copy_id": "working-copy-uuid",
      "before": {
        "filename": "奖学金申请表张三.pdf"
      },
      "after": {
        "filename": "2025-计算机学院-张三-国家励志奖学金申请表.pdf"
      },
      "execution_status": "PLANNED"
    }
  ]
}
```

`RENAME_FILES` and `RENAME_UPLOADED_FILES` are retired operation types and cannot be created or confirmed.
Rename suggestions are generated by the controlled `generate-rename-suggestions` Tool as
`RENAME_WORKING_COPIES`. Generic plans accept only the working-copy operation whitelist and a stable
`working_copy_id`; the server fills the authoritative before path, current version and SHA-256.

### 11.2 Get OperationPlan

```text
GET /api/operations/plans/{plan_id}
```

### 11.3 Confirm OperationPlan

```text
POST /api/operations/plans/{plan_id}/confirm
```

Request:

```json
{
  "confirmation": "确认执行"
}
```

Behavior:

```text
validate owner
validate plan status is PLANNED or WAITING_CONFIRMATION
validate operation type has a controlled executor
record confirmation text
execute only RENAME_WORKING_COPIES, MOVE_WORKING_COPIES, TRASH_WORKING_COPIES, or RESTORE_WORKING_COPIES through the working-copy executor
write confirmed-file-action ToolInvocation, ChangeSet, per-file success/failure, and final plan status
verify the current working-copy path, DocumentVersion, and SHA-256 before every item
reject retired or unsupported operation types with 409 and keep the plan waiting
```

Response:

```json
{
  "id": "operation-plan-uuid",
  "status": "EXECUTED",
  "changeset_id": "changeset-uuid",
  "result": {
    "executor": "working-copy",
    "status": "EXECUTED",
    "matched_count": 1,
    "completed_count": 1,
    "failed_count": 0,
    "items": [
      {
        "working_copy_id": "working-copy-uuid",
        "before_relative_path": "扫描件.txt",
        "after_relative_path": "2026_春季学生活动总结.txt",
        "status": "COMPLETED"
      }
    ]
  }
}
```

## 12. Feedback APIs

### 12.1 Submit Feedback

```text
POST /api/feedback
```

Request:

```json
{
  "target_type": "ANSWER",
  "target_id": "answer-uuid",
  "feedback_type": "WRONG_ANSWER",
  "comment": "这个答案引用的材料不支持结论。"
}
```

Valid target types:

```text
ANSWER
REFERENCE
CHUNK
DOCUMENT
CHANGESET
OPERATION_PLAN
WIKI_PAGE
```

Response:

```json
{
  "id": "feedback-uuid",
  "target_type": "ANSWER",
  "target_id": "answer-uuid",
  "feedback_type": "WRONG_ANSWER",
  "comment": "这个答案引用的材料不支持结论。",
  "status": "OPEN",
  "created_at": "2026-06-24T08:30:00Z"
}
```

## 13. Admin Document APIs

### 13.1 List Admin Documents

```text
GET /api/admin/documents
```

Response:

```json
{
  "items": [
    {
      "id": "document-uuid",
      "title": "国家励志奖学金申请表.pdf",
      "owner": {
        "id": "user-uuid",
        "display_name": "张三"
      },
      "conversation_id": "conversation-uuid",
      "status": "READY",
      "parse_status": "COMPLETED",
      "chunk_count": 12,
      "last_changeset_id": "changeset-uuid",
      "last_job": {
        "id": "job-uuid",
        "status": "COMPLETED",
        "error_message": ""
      },
      "created_at": "2026-06-24T08:00:00Z"
    }
  ],
  "next_cursor": null
}
```

### 13.2 Reprocess Document

```text
POST /api/admin/documents/{document_id}/reprocess
```

Behavior:

```text
create processing job
set document status PROCESSING
parse again
chunk again
embed again
create ChangeSet
update document status
```

Response:

```json
{
  "job_id": "job-uuid",
  "document_id": "document-uuid",
  "status": "PENDING"
}
```

## 14. Job APIs

### 14.1 Get Job

```text
GET /api/jobs/{job_id}
```

### 14.2 List Job Events

```text
GET /api/jobs/{job_id}/events
```

## 15. Admin Feedback APIs

### 15.1 List Feedback

```text
GET /api/admin/feedback
```

### 15.2 Get Feedback

```text
GET /api/admin/feedback/{feedback_id}
```

### 15.3 Resolve Feedback

```text
POST /api/admin/feedback/{feedback_id}/resolve
```

### 15.4 Reprocess Related Document

```text
POST /api/admin/feedback/{feedback_id}/reprocess-document
```

## 16. Admin LLM Settings APIs

### 16.1 Get LLM Settings

```text
GET /api/admin/settings/llm
```

Response:

```json
{
  "id": "settings-uuid",
  "provider": "openai_compatible",
  "api_url": "https://api.example.com/v1",
  "api_key_masked": "sk-****1234",
  "chat_model": "gpt-4o-mini",
  "embedding_model": "text-embedding-3-small",
  "embedding_dim": 1536,
  "is_active": true,
  "updated_at": "2026-06-24T08:00:00Z"
}
```

### 16.2 Update LLM Settings

```text
PUT /api/admin/settings/llm
```

Rules:

```text
api_key must be encrypted before saving
api_key must not be returned in plain text
changing embedding_dim after chunks exist requires admin confirmation
external model use for file content must be explicit
```

## 17. Frontend Route Mapping

```text
/login
  uses POST /api/auth/login

/chat
  uses GET /api/workspace/default
  uses GET/POST/PUT /api/conversations
  uses POST /api/conversations/{conversation_id}/messages
  uses POST /api/conversations/{conversation_id}/documents/upload
  uses GET /api/agent/tools
  uses GET /api/agent-runs/{agent_run_id}
  uses GET /api/agent-runs/{agent_run_id}/tool-invocations
  uses GET /api/changesets/{changeset_id}
  uses POST /api/conversations/{conversation_id}/evidence-answer
  uses POST /api/operations/plans
  uses GET /api/operations/plans/{plan_id}
  uses POST /api/operations/plans/{plan_id}/confirm
  uses POST /api/feedback
  uses POST /api/classification/suggestions/{suggestion_id}/feedback
  uses GET /api/classification/feedback/summary

/admin/documents
  uses GET /api/admin/documents
  uses GET /api/jobs/{job_id}
  uses GET /api/jobs/{job_id}/events
  uses POST /api/admin/documents/{document_id}/reprocess

/admin/feedback
  uses GET /api/admin/feedback
  uses GET /api/admin/feedback/{feedback_id}
  uses POST /api/admin/feedback/{feedback_id}/resolve
  uses POST /api/admin/feedback/{feedback_id}/reprocess-document

/admin/settings/llm
  uses GET /api/admin/settings/llm
  uses PUT /api/admin/settings/llm
```

## 18. Permission Matrix

| API | user | ops | admin |
|---|---:|---:|---:|
| `GET /api/workspace/default` | yes | yes | yes |
| `GET/POST/PUT /api/conversations` | yes | yes | yes |
| `POST /api/conversations/{id}/messages` | yes | yes | yes |
| `GET /api/agent/tools` | no | yes | yes |
| `POST /api/conversations/{id}/documents/upload` | yes | yes | yes |
| `GET /api/agent-runs/{id}` | no | yes | yes |
| `GET /api/agent-runs/{id}/tool-invocations` | no | yes | yes |
| `GET /api/documents` | yes | yes | yes |
| `POST /api/search` | yes | yes | yes |
| `POST /api/conversations/{id}/evidence-answer` | yes | yes | yes |
| `POST /api/operations/plans` | yes | yes | yes |
| `POST /api/operations/plans/{id}/confirm` | owner | yes | yes |
| `GET /api/changesets/{id}` | no | yes | yes |
| `POST /api/feedback` | yes | yes | yes |
| `POST /api/classification/suggestions/{id}/feedback` | owner run | owner run | owner run |
| `GET /api/classification/feedback/summary` | own | own | own |
| `GET /api/admin/documents` | no | yes | yes |
| `POST /api/admin/documents/{id}/reprocess` | no | yes | yes |
| `GET /api/admin/feedback` | no | yes | yes |
| `POST /api/admin/feedback/{id}/resolve` | no | yes | yes |
| `GET/PUT /api/admin/settings/llm` | no | yes | yes |
| `GET /api/admin/capability-suggestions` | no | yes | yes |
| `POST /api/admin/capability-suggestions/{id}/review` | no | review | review/accept |
| `GET /api/admin/planner-shadow/metrics` | no | yes | yes |

## 19. Three-tier File Lifecycle APIs

上传接口返回 `202 Accepted`，并包含 `upload_document_version_id`、`duplicate_review_id`、`filesystem_job_id`、`archive_status` 和 `duplicate_review_status`。请求线程只落暂存和创建任务。

```text
GET  /api/uploads/{upload_version_id}/duplicate-review
POST /api/uploads/{upload_version_id}/duplicate-review/decision
GET  /api/uploads/{upload_version_id}/archive-status
GET  /api/jobs/{job_id}
GET  /api/jobs/{job_id}/events

GET  /api/working-copies
GET  /api/working-copies/{working_copy_id}
GET  /api/working-copies/{working_copy_id}/download
GET  /api/working-copies/{working_copy_id}/lineage
GET  /api/working-copies/{working_copy_id}/versions
GET  /api/working-copies/{working_copy_id}/path-records

GET  /api/trash-entries
POST /api/trash-entries/{trash_entry_id}/restore-plan
```

普通文件检索只读取 `ACTIVE` 工作副本。只有用户消息明确包含带扩展名的完整文件名，且不存在
同名活动副本时，消息接口才可以返回 `response_type=trash_restore_selection`。对应
`trash_restore_result` 逐条返回当前用户可恢复的回收站候选；同名、同版本或同内容哈希候选不得
合并或预选，前端必须使用单选卡取得用户选择后，才可调用恢复计划和确认接口。

上传查重通过 SHA-256 命中已进入回收站的历史工作副本时，确认卡必须说明相同内容此前已删除，
并且只允许用户选择再次上传或取消。选择再次上传按新文件处理，不能按 WorkingCopy、
DocumentVersion 或内容哈希自动复活或合并已删除文件。

工作副本高风险计划使用 `RENAME_WORKING_COPIES`、`MOVE_WORKING_COPIES`、`TRASH_WORKING_COPIES` 和 `RESTORE_WORKING_COPIES`。创建请求只能提交 `working_copy_id` 和目标逻辑字段，后端必须从数据库重建 before/version/SHA-256 快照；确认后逐文件执行并写 ChangeSet。任何响应不得返回三个目录的宿主机绝对路径。

## 20. Adaptive Planner Admin APIs

### 20.1 List Capability Suggestions

```text
GET /api/admin/capability-suggestions?status=NEW&limit=100
```

只允许 ops/admin。响应是脱敏、去重后的能力缺口，不包含用户消息全文、文件正文、Prompt 或 Tool 输入。

### 20.2 Review Capability Suggestion

```text
POST /api/admin/capability-suggestions/{suggestion_id}/review
```

Request:

```json
{
  "status": "UNDER_REVIEW",
  "review_note": "评估是否进入下一版本"
}
```

ops 可以标记评审中、拒绝或合并；只有 admin 可以标记接受或已实现。任何状态变化都不会自动创建代码、
注册 Tool、启用 Skill 或扩大权限。

### 20.3 Read Planner Shadow Metrics

```text
GET /api/admin/planner-shadow/metrics?limit=5000
```

响应包含当前聚合批次的 `catalog_fingerprint`、`schema_version`、样本数、schema 校验通过率、
决策/范围/风险/确认一致率以及错误码计数。服务默认只聚合最新 Catalog 与 Planner schema 的同一批
样本，失败生成和失败校验同样计入分母。接口不能修改 `ADAPTIVE_PLANNER_MODE` 或灰度比例，也不能
返回 Shadow 决策中的输入内容。
