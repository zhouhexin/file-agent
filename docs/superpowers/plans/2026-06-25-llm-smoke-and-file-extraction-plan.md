# LLM Smoke And File Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 验证真实 LLM 对话闭环，并实现读取原始上传文件、解析文本/PDF/Excel/图片 OCR 的最小 Tool 能力。

**Architecture:** 保留现有 LangGraph 线性流程，不在本阶段修复条件边、暂停恢复和批量 map/reduce 问题。新增文件解析持久化表，Tool Registry 通过当前 `db + user_id` 读取用户自己的文件并写入解析结果。

**Tech Stack:** FastAPI、SQLAlchemy、Alembic、LangGraph、Pydantic、OpenAI-compatible Chat Completions、PyMuPDF、openpyxl、Pillow、pytesseract。

---

## Task 1: 真实 LLM Smoke Test

**Files:**
- No code change required unless smoke exposes a bug.
- Update if needed: `docs/runbook.md`

- [ ] 从项目根目录读取 `.env` 中的真实 LLM 配置。
- [ ] 使用真实 PostgreSQL、真实 LLM 和 TestClient 完成注册、上传、发送消息。
- [ ] 验证 `/api/conversations/{conversation_id}/messages` 返回 `COMPLETED`。
- [ ] 验证 ToolInvocation 包含 `read-document-insights`。
- [ ] 清理 smoke test 创建的用户、会话、文件记录和本地上传文件。

## Task 2: 文件解析持久化模型

**Files:**
- Modify: `apps/api/app/db/models.py`
- Create: `apps/api/alembic/versions/20260625_0007_add_document_extraction_tables.py`
- Test: `apps/api/app/tests/test_file_extraction_tools.py`

- [ ] 新增 `DocumentExtractionRun`，记录解析工具运行状态、extractor 和错误信息。
- [ ] 新增 `DocumentPage`，记录页、sheet、文本内容和元数据。
- [ ] 新增 Alembic migration。
- [ ] 编写 ORM 创建和迁移文件存在性测试。

## Task 3: 安全文件定位和读取

**Files:**
- Create: `apps/api/app/modules/files/extraction_repository.py`
- Test: `apps/api/app/tests/test_file_extraction_tools.py`

- [ ] 根据 `document_id + user_id` 定位当前用户自己的 Document。
- [ ] 根据 FileObject 和 `FILE_STORAGE_ROOT` 解析本地文件路径。
- [ ] 禁止读取不存在、不属于当前用户或没有 FileObject 的文件。
- [ ] 实现 `read-original-file` Tool，返回文件元信息，不返回二进制内容。

## Task 4: 文本、Excel、PDF、图片 OCR 解析

**Files:**
- Create: `apps/api/app/modules/files/extractors.py`
- Modify: `apps/api/app/modules/agent/tool_schemas.py`
- Modify: `apps/api/app/modules/agent/tool_registry.py`
- Test: `apps/api/app/tests/test_file_extraction_tools.py`

- [ ] `.txt` / `.md` 使用 UTF-8 容错解码。
- [ ] `.csv` 使用标准库读取并转为文本。
- [ ] `.xlsx` 使用 openpyxl 提取 sheet 行文本。
- [ ] `.pdf` 使用 PyMuPDF 提取页面文本。
- [ ] 图片使用 pytesseract + Pillow；如果 OCR 引擎不可用，返回结构化错误。
- [ ] `extract-document-text` 写入 `document_extraction_runs` 和 `document_pages`。

## Task 5: 文档和验证

**Files:**
- Modify: `requirements.txt`
- Modify: `apps/api/pyproject.toml`
- Modify: `.env.example` if needed
- Modify: `docs/runbook.md`
- Modify: `docs/api-contract.md`
- Modify: `docs/database-schema.md`

- [ ] 更新依赖说明。
- [ ] 更新 Tool 行为和 smoke test 说明。
- [ ] 运行 `python3 -m pytest -q`。
- [ ] 运行 `npm run build`。
- [ ] 运行 `python3 -m alembic -c apps/api/alembic.ini upgrade head`。
- [ ] 扫描敏感信息，提交并推送。
