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
- 查看 AgentRun、ChangeSet、引用和 OperationPlan
- 确认自己的 OperationPlan
- 提交反馈

admin / ops:
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
    "message": "Invalid request"
  }
}
```

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
  "status": "ok"
}
```

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

Response:

```json
{
  "id": "conversation-uuid",
  "title": "资助材料整理",
  "status": "active",
  "messages": [
    {
      "id": "message-uuid",
      "role": "user",
      "content": "帮我读取并分类这批文件。",
      "agent_run_id": "agent-run-uuid",
      "created_at": "2026-06-24T08:01:00Z"
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
persist tool_invocations
return message_id and agent_run_id
```

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
  "agent_run": {
    "agent_run_id": "agent-run-uuid",
    "conversation_id": "conversation-uuid",
    "message_id": "message-uuid",
    "intent": "CLASSIFY_FILES",
    "status": "COMPLETED",
    "selected_skills": ["chat-intake", "file-ingest", "document-classification", "change-report"],
    "tool_plan": {},
    "tool_results": [],
    "tool_invocations": [],
    "changeset_id": null,
    "operation_plan_id": null,
    "final_response": "AgentRun completed with 4 tool invocation(s).",
    "errors": []
  }
}
```

When LLM is enabled and the user asks for uploaded-file summary or basic file information, the same endpoint may return:

```json
{
  "agent_run": {
    "intent": "SUMMARIZE_DOCUMENTS",
    "status": "COMPLETED",
    "selected_skills": ["llm-understanding", "document-insight-read"],
    "tool_invocations": [
      {
        "tool_name": "read-document-insights",
        "status": "COMPLETED"
      }
    ],
    "final_response": "已读取 1 个文件的基础洞察：student.txt。"
  }
}
```

When LLM is enabled and the user asks to read original content, parse PDF/Excel, or OCR an image, the same endpoint may return:

Current `extract-document-text` supports `txt/md/csv/xlsx/docx/pdf/image`.

```json
{
  "agent_run": {
    "intent": "EXTRACT_DOCUMENT_TEXT",
    "status": "COMPLETED",
    "selected_skills": ["llm-understanding", "document-text-extract"],
    "tool_invocations": [
      {
        "tool_name": "extract-document-text",
        "status": "COMPLETED"
      }
    ],
    "final_response": "已解析 1 个文件，提取 1 页/Sheet，共 1200 个字符。"
  }
}
```

Errors:

```text
403 not owner
404 conversation not found
422 invalid attachment document ids
```

## 6. AgentRun APIs

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
  "selected_skills": ["chat-intake", "file-ingest", "document-classification", "change-report"],
  "tool_plan": {
    "intent": "CLASSIFY_FILES",
    "user_goal": "读取并分类刚上传的文件",
    "steps": [
      {
        "step_id": "step-1",
        "skill": "file-ingest",
        "tool_name": "document-convert",
        "requires_confirmation": false,
        "risk_level": "low",
        "expected_outputs": ["pages", "metadata", "artifacts"]
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
      "name": "document-convert",
      "description": "用 Unstructured/Haystack/LlamaIndex/LangChain adapter 抽取文档文本和结构",
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
document-register-upload
security-scan
document-convert
table-extract
artifact-write
chunk-build
embedding-generate
metadata-extract
multi-label-classify
read-document-insights
read-original-file
extract-document-text
hybrid-search
evidence-answer
change-report
operation-plan-create
confirmed-file-action
feedback-record
job-status-read
document-lineage-read
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
      "tool_name": "document-convert",
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

### 7.3 Delete Uploaded Document

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
  "items": [
    {
      "id": "chunk-uuid",
      "chunk_index": 0,
      "text": "申请国家励志奖学金需要提交申请表、成绩证明和相关审核材料。",
      "page_no": 1,
      "sheet_name": null,
      "cell_range": null,
      "metadata": {
        "source_type": "pdf_page"
      }
    }
  ],
  "next_cursor": null
}
```

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

### 8.1 Hybrid Search

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
search current attachments first
prioritize current conversation documents
generate query embedding
run vector search
run full-text search
merge and rerank
filter by current user's workspace
```

Response:

```json
{
  "results": [
    {
      "chunk_id": "chunk-uuid",
      "document_id": "document-uuid",
      "document_title": "资助政策.pdf",
      "page_no": 2,
      "sheet_name": null,
      "cell_range": null,
      "quote": "申请国家助学金需要提交申请表和相关证明材料。",
      "score": 0.82
    }
  ]
}
```

## 9. Evidence Answer Skill API

### 9.1 Ask Evidence-Backed Question

```text
POST /api/conversations/{conversation_id}/evidence-answer
```

Request:

```json
{
  "question": "国家励志奖学金申请流程是什么？",
  "attachment_document_ids": [],
  "top_k": 8
}
```

Behavior:

```text
create or reuse AgentRun depending on caller
retrieve relevant chunks
call chat model using evidence-answer Skill
save assistant message
save qa_answers
save answer_references
return answer and references
```

Response:

```json
{
  "answer_id": "answer-uuid",
  "message_id": "assistant-message-uuid",
  "agent_run_id": "agent-run-uuid",
  "answer": "根据当前知识库资料，申请流程包括提交申请表、学院审核和学校复核。",
  "references": [
    {
      "reference_id": "reference-uuid",
      "document_id": "document-uuid",
      "document_title": "国家励志奖学金申请表.pdf",
      "chunk_id": "chunk-uuid",
      "page_no": 1,
      "sheet_name": null,
      "cell_range": null,
      "quote": "申请人提交申请表后，由学院审核并报学校复核。",
      "score": 0.87
    }
  ]
}
```

No evidence response:

```json
{
  "answer_id": "answer-uuid",
  "message_id": "assistant-message-uuid",
  "agent_run_id": "agent-run-uuid",
  "answer": "我没有在当前知识库中找到明确依据。",
  "references": []
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

Response:

```json
{
  "id": "changeset-uuid",
  "conversation_id": "conversation-uuid",
  "agent_run_id": "agent-run-uuid",
  "operation_type": "DOCUMENT_INGEST",
  "status": "COMPLETED",
  "summary": {
    "total": 3,
    "success": 2,
    "failed": 0,
    "needs_review": 1,
    "original_files_changed": false
  },
  "items": [
    {
      "id": "change-item-uuid",
      "target_type": "DOCUMENT",
      "target_id": "document-uuid",
      "change_type": "CATEGORY_ADDED",
      "before": null,
      "after": {
        "category": "奖助学金与资助",
        "confidence": 0.92,
        "status": "AUTO_APPLIED"
      },
      "evidence": {
        "page_no": 1,
        "quote": "国家励志奖学金申请表"
      },
      "execution_status": "COMPLETED"
    }
  ],
  "created_at": "2026-06-24T08:00:00Z",
  "completed_at": "2026-06-24T08:02:00Z"
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
  "operation_type": "RENAME_FILES",
  "reason": "生成标准化文件名建议",
  "items": [
    {
      "document_id": "document-uuid",
      "before": {
        "filename": "奖学金申请表张三.pdf"
      },
      "after": {
        "filename": "2025-计算机学院-张三-国家励志奖学金申请表.pdf"
      }
    }
  ]
}
```

Response:

```json
{
  "id": "operation-plan-uuid",
  "status": "PLANNED",
  "operation_type": "RENAME_FILES",
  "requires_confirmation": true,
  "risk_level": "medium",
  "items": [
    {
      "document_id": "document-uuid",
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
execute through confirmed-file-action Tool
create ChangeSet
update plan status
```

Response:

```json
{
  "id": "operation-plan-uuid",
  "status": "EXECUTED",
  "changeset_id": "changeset-uuid"
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
| `GET /api/agent/tools` | yes | yes | yes |
| `POST /api/conversations/{id}/documents/upload` | yes | yes | yes |
| `GET /api/agent-runs/{id}` | owner | yes | yes |
| `GET /api/documents` | yes | yes | yes |
| `POST /api/search` | yes | yes | yes |
| `POST /api/conversations/{id}/evidence-answer` | yes | yes | yes |
| `POST /api/operations/plans` | yes | yes | yes |
| `POST /api/operations/plans/{id}/confirm` | owner | yes | yes |
| `GET /api/changesets/{id}` | owner | yes | yes |
| `POST /api/feedback` | yes | yes | yes |
| `GET /api/admin/documents` | no | yes | yes |
| `POST /api/admin/documents/{id}/reprocess` | no | yes | yes |
| `GET /api/admin/feedback` | no | yes | yes |
| `POST /api/admin/feedback/{id}/resolve` | no | yes | yes |
| `GET/PUT /api/admin/settings/llm` | no | yes | yes |
