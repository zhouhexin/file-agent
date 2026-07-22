# File Agent Runbook

本文记录当前项目的本地启动、验证方式和可用接口。后续如果端口、命令、环境依赖、启动顺序或接口能力发生变化，必须同步更新本文和 `README.md`。

整项目发布前的真实文件系统烟测、测试数据矩阵和逐项通过标准见
`docs/file-agent-manual-smoke-test.md`。本文只维护启动与运行方式，不能替代烟测手册。

## 1. Python 环境

后端使用用户当前已经配置好的 `/opt/homebrew/anaconda3/envs/py311/bin/python` 环境，不强制创建新虚拟环境，不强制切换到 `uv`、Poetry 或其他包管理方式。

当前已验证的运行方式是在项目根目录执行命令，避免进入 `apps/api` 后 shell 解析到不同的 Python 解释器。

安装后端依赖：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python -m pip install -r requirements.txt
```

当前根目录 `requirements.txt` 包含后端运行、数据库 migration、测试和 PostgreSQL 连接所需依赖。`apps/api/pyproject.toml` 保留为后端包元数据；本地启动优先使用上面的 `requirements.txt` 安装命令。

首次配置本地环境时复制环境变量模板：

```bash
cp .env.example .env
```

后端启动和 migration 会自动读取项目根目录 `.env`。真实密码只保存在本地 `.env`，不要提交到 Git。

## 2. 运行测试

在项目根目录执行：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python -m pytest
```

当前期望结果：

```text
405 passed, 19 skipped
```

当前跳过项是需要真实外部执行器或独立环境的既有集成测试。Paddle、PyMuPDF 等依赖可能输出弃用或
环境兼容警告；只要没有失败项，不影响当前自动化验收结论。

## 3. 数据库

当前后端已经持久化 user、default workspace、message、AgentRun 和 ToolInvocation。

当前本机后端数据库连接：

```text
postgresql+psycopg2://fileagent_user:<password>@212.64.14.158:5432/fileAgent
```

当前已验证该 PostgreSQL 实例可连接，返回数据库 `fileAgent`、用户 `fileagent_user`、PostgreSQL `16.14`。

后端服务数据库必须使用 PostgreSQL。未配置 `DATABASE_URL`，或将 `DATABASE_URL` 配置为 SQLite，服务会直接启动失败。测试代码可以继续使用隔离的内存 SQLite，但运行中的 API 服务不得使用 SQLite。

如需使用项目自带 Docker PostgreSQL + pgvector：

```bash
docker compose up -d postgres
export DATABASE_URL='postgresql+psycopg2://file_agent:file_agent_dev@127.0.0.1:5432/file_agent'
export AUTO_CREATE_TABLES=false
/opt/homebrew/anaconda3/envs/py311/bin/python -m alembic -c apps/api/alembic.ini upgrade head
```

对当前 `.env` 指向的 PostgreSQL 执行 migration：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python -m alembic -c apps/api/alembic.ini upgrade head
```

Docker 生产环境中必须从容器内的仓库根目录 `/app` 显式指定 Alembic 配置文件；直接执行 `alembic upgrade head` 会因为找不到 `script_location` 失败：

```bash
docker compose -f deploy/docker-compose.production.yml exec -w /app api python -m alembic -c apps/api/alembic.ini upgrade head
```

如果已经在 `deploy/` 目录内执行 compose 命令，则使用：

```bash
docker compose exec -w /app api python -m alembic -c apps/api/alembic.ini upgrade head
```

当前 `.env` 中 `AUTO_CREATE_TABLES=false`，应通过 Alembic migration 管理数据库结构。

## 4. 启动后端服务

在项目根目录执行：

```bash
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

也可以在 `apps/api` 目录执行：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

配置层会从当前目录向上查找 `.env`，因此上述两种方式都会读取项目根目录 `.env` 并连接 PostgreSQL。

从项目根目录启动后端时必须设置 `PYTHONPATH=apps/api`，否则 Python 只能在根目录查找 `app` 包。
如果在项目根目录执行 `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000` 且没有设置 `PYTHONPATH=apps/api`，会报 `ModuleNotFoundError: No module named 'app'`。

服务地址：

```text
http://127.0.0.1:8000
```

message、AgentRun 和 ToolInvocation 会写入当前 `DATABASE_URL` 指向的数据库。
上传文件会写入 `FILE_STORAGE_ROOT`，默认是 `./storage/uploads`。
`extract-document-text` 会把解析结果写入 `document_extraction_runs` 和 `document_pages`。

上传接口使用临时文件和分块流式写入，不会把整份文件一次性读入内存。以下参数只用于部署资源保护，
可以根据磁盘、并发和 worker 容量调整，不应被解释为学校业务文件的固定大小限制：

```dotenv
UPLOAD_MAX_FILE_SIZE_MB=1024
UPLOAD_CHUNK_SIZE_BYTES=1048576
UPLOAD_ALLOWED_EXTENSIONS=pdf,doc,docx,xls,xlsx,xlsm,txt,md,csv,png,jpg,jpeg,tif,tiff,bmp,webp
```

当前阶段只检查受支持扩展名、基础 MIME 一致性、Office 宏标记和文件加密状态。没有接入病毒扫描
引擎，回执和日志不得把上述检查表述为“已通过病毒扫描”。

服务端结构化日志会写入 `LOG_DIR`，默认 `./logs`。日志文件按天生成：

```text
logs/file-agent-YYYY-MM-DD.log
```

每行是一条 JSON，包含 `request_id`、`agent_run_id`、`user_id`、`conversation_id`、`tool_name`、`document_id`、`status`、`duration_ms` 和 `error_code` 等字段。启动时会按 `LOG_RETENTION_DAYS` 清理超过保留期的旧日志，默认保留 7 天。

## 4.1 LLM 配置

默认 `LLM_ENABLED=false`，消息入口会继续使用确定性 Planner，便于本地开发和测试稳定运行。

如需在对话阶段启用 LLM 理解用户需求，请在项目根目录 `.env` 中增加：

```text
LLM_ENABLED=true
LLM_PROVIDER=openai_compatible
LLM_API_KEY=<your-api-key>
LLM_BASE_URL=<openai-compatible-base-url>
LLM_CHAT_MODEL=<chat-model-name>
LLM_TIMEOUT_SECONDS=30
LLM_CLASSIFICATION_MODE=rule_only
LLM_CLASSIFICATION_ALLOW_FREE_PATHS=false
OCR_ENABLED=true
OCR_PADDLE_MODEL_SOURCE=BOS
OCR_LLM_ENABLED=false
OCR_LLM_FALLBACK_QUALITY_THRESHOLD=0.68
DOCLING_ENABLED=true
DOCLING_FORMATS=pdf,docx
DOCLING_OCR_ENABLED=false
```

当前客户端调用 OpenAI-compatible `/chat/completions` 接口，并要求模型返回符合 `UserIntentPlan` 的 JSON 对象。上传阶段的 deterministic ingest 不依赖 LLM；对话阶段启用 LLM 后，会先理解用户需求，再通过白名单 Tool 读取 `document_insights` 或执行后续受控工具。

OCR 第一阶段使用本地 PaddleOCR 作为默认 Provider。图片文件会直接进入 OCR；PDF 原生文本为空时会先渲染页面，再进入 OCR，并把识别文本写入 `document_pages.text_content`。`OCR_PADDLE_MODEL_SOURCE` 默认是 `BOS`，服务会在加载 PaddleOCR 前设置 `PADDLE_PDX_MODEL_SOURCE=BOS`，让 PaddleOCR 使用百度 BOS 模型下载源。如需启用 LLM OCR 兜底，必须显式设置 `OCR_LLM_ENABLED=true` 且 `LLM_ENABLED=true`；系统会在本地 OCR 质量低于 `OCR_LLM_FALLBACK_QUALITY_THRESHOLD` 时按页调用多模态模型，不默认外发整份文件。

PDF、DOCX 默认使用 Docling 进行本地结构化解析，并将标题、章节、正文、页眉页脚和位置元素写入 `document_elements`。`DOCLING_OCR_ENABLED=false` 时，扫描件继续使用上述 PaddleOCR/LLM OCR 链路；Docling 缺失、转换失败或正文为空时自动回退现有 PyMuPDF/python-docx 解析器。首次启用或升级 Docling 后，解析器配置指纹会变化，相关文件下一次读取时会生成新的解析运行，旧解析结果继续保留用于历史审计。

升级到结构化解析版本后执行：

```bash
cd apps/api
/opt/homebrew/anaconda3/envs/py311/bin/python -m alembic -c alembic.ini upgrade head
```

分类 LLM 判定由 `LLM_CLASSIFICATION_MODE` 单独控制：

```text
rule_only：默认值，只使用 taxonomy 候选召回和规则建议。
hybrid：LLM 只能从候选 category_id 中选择 0~3 个分类。
review_only：仅当规则结果为“其他”、低置信度或需要复核时调用 LLM。
```

如果需要允许 LLM 自由生成分类路径，必须同时设置：

```text
LLM_CLASSIFICATION_MODE=hybrid
LLM_CLASSIFICATION_ALLOW_FREE_PATHS=true
```

自由生成的分类路径不会自动进入正式 taxonomy，也不会写入正式 `document_categories`。系统会把它保存为 `source=llm_free_path`、`status=NEEDS_REVIEW` 的建议，等待人工确认、纠正或后续维护 taxonomy v2。

2026-06-25 已完成真实模型 smoke test：临时启用 `LLM_ENABLED=true` 后，`MiniMax-M3` 可完成“总结我刚才上传的文件”请求，AgentRun 返回 `COMPLETED`，ToolInvocation 为 `read-document-insights`，且 `graph_state_json.user_intent_plan` 已写入。

## 5. 启动前端服务

首次启动前安装依赖：

```bash
cd apps/web
npm install
```

启动前端开发服务：

```bash
npm run dev
```

前端地址：

```text
http://127.0.0.1:5173
```

Vite 开发端口已固定为 `5173`，不会自动切换到其他端口。如果该端口被占用，请先停止占用进程；确实需要改端口时，必须同步更新 Vite 配置、`VITE_API_BASE_URL` 和后端 CORS 白名单。

前端当前能力：

```text
注册用户
登录用户
保存 access_token 到 localStorage
启动时调用 /api/auth/me 校验登录态
进入 /chat
选择文件并上传到 /api/files/upload
上传后自动执行 deterministic ingest：去重、基础分类、关键词提取
展示已上传文件名、大小、处理状态和去重结果
发送前可删除上传文件，并同步删除后端文件
发送一条消息到 AgentRun
发送消息时携带真实 document_id
发送后附件进入对话并锁定，不再允许删除
展示普通用户任务状态、逐文件整理回执和必要确认，不展示 AgentRun、intent、Skill 或 Tool 内部载荷
退出登录
```

默认 API 地址：

```text
http://127.0.0.1:8000/api
```

如需改后端地址，可设置：

```bash
VITE_API_BASE_URL=http://127.0.0.1:8000/api npm run dev
```

## 6. 当前可用接口

健康检查：

```bash
curl http://127.0.0.1:8000/api/health
```

期望返回：

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

查看 MVP Tool 白名单：

```bash
curl http://127.0.0.1:8000/api/agent/tools \
  -H 'Authorization: Bearer <ops-or-admin-access-token>'
```

Tool 白名单和 AgentRun/ToolInvocation 详情属于内部审计信息，仅 `ops`、`admin` 可访问；普通用户通过
消息接口的 `task_result` 查看整理结果。

注册用户：

```bash
curl -X POST http://127.0.0.1:8000/api/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"zhangsan","password":"password123","display_name":"张三","email":"zhangsan@example.com"}'
```

登录并获取 token：

```bash
curl -X POST http://127.0.0.1:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"zhangsan","password":"password123"}'
```

查看当前用户：

```bash
curl http://127.0.0.1:8000/api/auth/me \
  -H 'Authorization: Bearer <access_token>'
```

上传文件并获取 `document_id`：

```bash
curl -X POST http://127.0.0.1:8000/api/files/upload \
  -H 'Authorization: Bearer <access_token>' \
  -F 'file=@/path/to/file.pdf'
```

删除尚未进入对话的上传文件：

```bash
curl -X DELETE http://127.0.0.1:8000/api/files/<document_id> \
  -H 'Authorization: Bearer <access_token>'
```

读取附件原始内容，用于前端点击附件后预览或下载：

```bash
curl -X GET http://127.0.0.1:8000/api/files/<document_id>/content \
  -H 'Authorization: Bearer <access_token>' \
  --output downloaded-file
```

发送用户消息并启动一次持久化 LangGraph AgentRun：

```bash
curl -X POST http://127.0.0.1:8000/api/conversations/conv-1/messages \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <access_token>' \
  -d '{"content":"帮我读取并分类这批文件","attachments":[{"document_id":"doc-1"}]}'
```

读取会话详情，用于前端刷新后恢复历史消息、附件和 AgentRun 回复：

```bash
curl -X GET http://127.0.0.1:8000/api/conversations/conv-1 \
  -H 'Authorization: Bearer <access_token>'
```

当前期望行为：

```text
message.role = user
message.user_id = 当前登录用户 id
agent_run.status = COMPLETED
agent_run.intent = CLASSIFY_FILES
agent_run.user_id = 当前登录用户 id
tool_invocations = 每个附件各 1 次 extract-document-text
分类依据 = document_pages 中的完整正文，不使用 300 字 text_preview
```

如果 `LLM_ENABLED=true` 且用户需求是总结或查看已上传文件基础信息，当前期望行为：

```text
agent_run.intent = SUMMARIZE_DOCUMENTS 或模型识别出的结构化 intent
selected_skills = llm-understanding, document-insight-read
tool_invocations = read-document-insights
graph_state_json.user_intent_plan = LLM 返回的结构化意图
```

如果用户需求是读取正文、解析 PDF/Excel 内容或 OCR 图片，当前期望行为：

```text
LLM_ENABLED=true 时：agent_run.intent = EXTRACT_DOCUMENT_TEXT 或模型识别出的结构化 intent
deterministic 模式下用户明确说“读取/解析/正文/内容/OCR”时：agent_run.intent = EXTRACT_DOCUMENT_TEXT
“读取并分类 / 解析并归类”等组合意图优先按正文读取处理，分类作为 document_results 的输出要求
LLM 模式 selected_skills = llm-understanding, document-text-extract
deterministic 模式 selected_skills = chat-intake, document-text-extract, document-classification, change-report
tool_invocations = 每个附件各 1 次 extract-document-text
tool_invocations.status = Tool 输出 ok=false 或 status=FAILED 时记为 FAILED
document_extraction_runs 默认复用同一文件最近一次成功解析结果；用户明确说“重新解析 / 重新读取 / 重新处理 / 重跑”时才新建解析运行
document_pages 只在首次成功解析或强制重处理时写入；默认复用不会重复写页
graph_state_json.document_results 写入逐文件解析状态、字符数、分类建议、evidence_items、错误
document_classification_runs / document_category_suggestions 写入本次 AgentRun 的结构化分类建议和 evidence_items
change_sets / change_items 写入本次处理明细和 evidence_items；复用时记录 TEXT_REUSED、DOCUMENT_PAGES_REUSED、CATEGORY_SUGGESTION_REUSED
final_response = 已处理 N 个文件，并逐文件返回解析状态、多个分类建议、置信度、页码/Sheet 和原文 quote。
```

当前运行时分类统一使用项目内生成后的 JSON 分类配置：

```text
apps/api/app/modules/classification/taxonomies/unified_school_file_classification.json
```

该配置由预置 `school_file_classification.json` 与受管目录清洗快照共同生成。当前 taxonomy version 为 `2026-07-v2`，已依次合并 2026-07-15 记录快照和 2026-07-18 挂载卷实时快照。`DocumentClassificationService` 对上传文件和受管文件始终加载这一套分类，不再因为存在 `PATH_AS_CATEGORY` 根而切换为 `managed_global_categories`。目录中的 `CATEGORY`、`DEPARTMENT` 只增强已有稳定分类 ID 的别名和正向信号；年份、临时目录和集合目录不会成为业务分类。分类 matcher 会基于分类名、别名、正向信号、负向信号和一级域上下文生成 Top N 候选；`match_document_text` 仍作为 rule-only 兼容入口，最多保留前 5 个分类建议。对话链路通过 `DocumentClassificationService` 从 `document_pages.text_content` 读取完整正文，Graph 不直接读取全文或调用底层 matcher。分类建议会同时保存在本次 AgentRun 的 `graph_state_json.document_results`、用户回执、`document_classification_runs` 和 `document_category_suggestions` 中。

如需从 Excel 重新生成分类 JSON，可执行：

```bash
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python scripts/convert_taxonomy_excel.py \
  --file "/path/to/文件归类(1).xlsx" \
  --sheet Sheet2 \
  --output apps/api/app/modules/classification/taxonomies/school_file_classification.json
```

预置分类或受管目录快照更新后，生成下一版统一 taxonomy：

```bash
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python scripts/build_unified_taxonomy.py \
  --base apps/api/app/modules/classification/taxonomies/school_file_classification.json \
  --inventory rules/classification-source-inventory/managed-downloads-2026-07-v1.json \
  --inventory rules/classification-source-inventory/managed-downloads-2026-07-v2.json \
  --output apps/api/app/modules/classification/taxonomies/unified_school_file_classification.json \
  --version 2026-07-v2
```

`--inventory` 可以重复传入，构建器按参数顺序增量合并并自动去重。生成新版本时保留历史快照参数，再在末尾追加新快照，禁止直接把 `UNKNOWN`、`TEMPORARY`、`COLLECTION` 或年份目录提升为业务分类。

当前新增文件解析 Tool：

```text
read-original-file：读取当前用户上传原始文件的安全元信息，不返回本地路径或二进制内容
extract-document-text：解析 txt/md/csv/xls/xlsx/doc/docx/pdf/image，并将文本写入 document_pages；旧版 `.xls` 必须先通过 LibreOffice 隔离转换为临时 `.xlsx`，再由 openpyxl 解析，不覆盖原件
```

旧版 `.doc` 使用“持久派生件”链路：首次读取时通过 LibreOffice Headless 转换为 `.docx`，保存到
`FILE_STORAGE_ROOT/derivatives/office/` 并登记到 `document_artifacts`。后续解析、Docling、分类、摘要、
问答和重命名字段提取复用同一派生件；原始下载与真实改名仍操作 `.doc` 原件。用户说“重新解析”时
复用有效派生件，只重建解析结果；说“重新转换”时才同时绕过派生件缓存。

旧版 `.xls` 使用同一 LibreOffice 安全边界，但转换结果只存在于单次解析的独立临时目录：输入副本、
输出目录和 LibreOffice profile 相互隔离，输出必须通过 OOXML 和 openpyxl 校验后才能读取。临时
`.xlsx` 不登记为新原件或 DocumentVersion，退出解析后清理；原 `.xls` 字节始终不变。

配置：

```dotenv
LEGACY_OFFICE_CONVERSION_ENABLED=true
LEGACY_OFFICE_CONVERTER=libreoffice
LIBREOFFICE_EXECUTABLE=
LEGACY_OFFICE_CONVERSION_TIMEOUT_SECONDS=90
LEGACY_OFFICE_MAX_FILE_SIZE_MB=100
LEGACY_OFFICE_DERIVATIVE_DIR=derivatives/office
```

LibreOffice 安装与路径：

```text
Windows: 安装 LibreOffice 64 位版，优先使用
         C:\Program Files\LibreOffice\program\soffice.com
macOS:   安装 LibreOffice.app，默认发现
         /Applications/LibreOffice.app/Contents/MacOS/soffice
Linux:   使用系统包管理器安装 libreoffice，默认发现 /usr/bin/soffice
```

`LIBREOFFICE_EXECUTABLE` 留空时按“PATH -> 平台默认目录”查找。Windows 优先 `soffice.com`，便于获得
可靠退出码；路径包含空格无需手工加引号。LibreOffice 不可用或转换失败时，`.doc` 可按既有规则使用
受控纯文本降级；`.xls` 不允许用文件名或其他库冒充完整正文解析，必须返回结构化转换失败，且不会覆盖原件。

PDF、Excel、doc/docx 和图片 OCR 依赖：

```text
PyMuPDF
openpyxl
python-docx
Pillow
paddleocr
textutil 或 LibreOffice
LibreOffice（旧版 .doc 和 .xls 转换所需；.xls 不再使用 xlrd）
```

图片 OCR 和扫描 PDF OCR 默认使用 PaddleOCR CPU Provider；旧版 `.doc` 优先使用 LibreOffice 生成持久化 `.docx` 派生件，失败后再使用现有纯文本回退；旧版 `.xls` 必须由 LibreOffice 转为临时 `.xlsx` 后交给 openpyxl。如果缺少依赖或 OCR/转换引擎不可用，Tool 会返回结构化错误，不会读取任意路径。

当前对话触发解析已支持多个附件顺序执行。单个文件 Tool 异常会记录为该文件的失败 `document_results.errors`，后续文件继续处理；并发执行、LangGraph map/reduce、步骤级重试和恢复后续单独实现。

查询 AgentRun：

```bash
curl http://127.0.0.1:8000/api/agent-runs/<agent_run_id> \
  -H 'Authorization: Bearer <ops-or-admin-access-token>'
```

查询 Tool 调用：

```bash
curl http://127.0.0.1:8000/api/agent-runs/<agent_run_id>/tool-invocations \
  -H 'Authorization: Bearer <ops-or-admin-access-token>'
```

非法附件示例：

```bash
curl -X POST http://127.0.0.1:8000/api/conversations/conv-1/messages \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <access_token>' \
  -d '{"content":"帮我读取文件","attachments":[{"filename":"bad.pdf"}]}'
```

当前期望返回 HTTP `422`，因为附件缺少 `document_id`。

## 7. 工作副本重命名执行器

受管原始目录只负责保存不可变原始文件。用户对附件或受管目录提出重命名请求时，后端先把确定范围映射为活动工作副本，再生成 `RENAME_WORKING_COPIES` OperationPlan。未完成异步导入时返回 `WAITING_FOR_ASYNC_JOB`，不得改动上传暂存或受管原始文件。

命名字段解析仍使用：

```text
FILE_RENAME_PARSE_MODE=hybrid
FILE_RENAME_MAX_BATCH_SIZE=20
FILE_RENAME_EXECUTION_TIMEOUT_SECONDS=60
```

`FILE_RENAME_PARSE_MODE` 控制命名字段的解析来源，与执行器配置相互独立：

- `hybrid`：Docling 与原生解析器生成候选并逐字段仲裁，默认用于生产。
- `native`：只使用原生解析器，Docling 质量异常时用于紧急回退。
- `docling`：只使用 Docling，主要用于对比测试和问题定位；Docling 不可用时仍安全回退原生解析。

`FILE_RENAME_EXECUTOR`、F2 和旧 Native 受管文件执行器属于历史兼容代码，当前 Agent Runtime 不再创建或确认 `RENAME_FILES` / `RENAME_UPLOADED_FILES`。即使 `.env` 保留旧配置，也不能绕过工作副本白名单。

工作副本执行前必须同时校验计划中的当前相对路径、`DocumentVersion` 和内容 SHA-256。重命名和移动不创建新版本，但会写入 `WorkingCopyPathRecord`、ChangeSet、ChangeItem 和逐文件回执。计划创建时记录为 `PLANNED`，执行时推进到 `RUNNING`，最终为 `COMPLETED`、`FAILED` 或 `STALE`。

可通过以下 API 创建或查询工作副本路径计划：

```text
POST /api/operations/plans
GET  /api/operations/plans/{plan_id}
POST /api/operations/plans/{plan_id}/confirm
```

自动提取缺少年份或正文标题时，该工作副本只返回 `NEEDS_REVIEW`，不会进入执行计划。旧的
`file_rename_review_items -> RENAME_FILES` 即时更正链路已经退役；人工指定名称也必须携带稳定
`working_copy_id` 创建新的 `RENAME_WORKING_COPIES` 计划，不能复用历史待复核项直接改文件。

自动生成建议发生目标冲突时，不得自动分配 `_第二版`，也不得覆盖已有工作副本。系统先保留新文件的
原上传文件名，并通过普通用户回执询问：同时保留、保留已有、替换现有工作副本或删除现有工作副本。
只有用户明确选择“同时保留”后，后续确认流程才可以固化稳定版本后缀；替换或删除只能针对活动工作
副本生成 OperationPlan，不得修改不可变受管原件。

旧版 `.xls` 不使用 `xlrd`。LibreOffice 转换或表格正文解析失败时，如果文件名同时包含可验证年份和
可清理标题，重命名服务仍可使用表格文件名回退生成待确认建议，并继续保留失败的 ExtractionRun。
文件名回退只适用于 `.xls/.xlsx/.xlsm/.csv/.tsv`，可清理前导“附件”、括号日期、末尾提交单位加
八位日期、`new` 标记和“摸底统计表”中的“摸底”。该回退不能伪造正文证据，也不能用于分类结论。

历史 F2/Native 原地重命名测试不属于当前工作副本验收范围；不得通过启用集成测试把旧执行器重新接回生产确认入口。

### 7.1 上传附件工作副本重命名

聊天消息携带上传附件并要求重命名时，Planner 会把后端已解析的 `document_ids` 交给
`generate-rename-suggestions`。Tool 沿 `DocumentVersion -> UploadArchiveRecord -> ManagedFile -> WorkingCopy`
解析活动工作副本，并生成 `RENAME_WORKING_COPIES` OperationPlan。确认前不会修改文件；确认后只更新工作副本路径、工作副本 Document 展示名和同一版本的存储路径。

文件物理位置始终位于：

```text
WORKING_COPY_STORAGE_ROOT/<working_root_relative_path>/<new_basename>
```

目标目录和路径完全由后端计算，OperationPlan 的重命名输入只接受 basename 和稳定 `working_copy_id`。
当前阶段不根据分类选择目录。受管原始文件、上传暂存文件和内容版本均保持不变；成功和失败都写入
`confirmed-file-action` ToolInvocation、ChangeSet、工作副本路径记录和逐文件结果。

每次上传都创建独立的上传 Document 和暂存对象。删除尚未发送到消息的上传仅取消该上传生命周期并异步清理暂存文件；发现重复候选时，由用户逐文件选择继续上传、使用同工作区已有文件或取消上传。未经确认不得自动合并、覆盖或删除原始文件。

## 8. Neo4j 图谱增强分类

图谱分类默认关闭。PostgreSQL、taxonomy v2、分类反馈和受管目录扫描结果仍是事实源；Neo4j 只保存
可重建分类层级、目录角色、可信或弱分类关系及文档级聚合向量。全文和分块正文不会写入 Neo4j。

本地或非 Docker 环境安装可选依赖：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python -m pip install -r requirements-graph.txt
```

服务器构建镜像时配置：

```text
INSTALL_GRAPH_DEPENDENCIES=true
```

Neo4j 服务可以由独立主机或独立部署栈提供；当前生产 compose 不强制创建 Neo4j 容器。连接配置：

```text
GRAPH_CLASSIFICATION_ENABLED=false
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=<password>
NEO4J_DATABASE=neo4j
NEO4J_QUERY_TIMEOUT_SECONDS=3
NEO4J_SYNC_ENABLED=false
GRAPH_CLASSIFICATION_MAX_HOPS=1
GRAPH_CLASSIFICATION_TOP_K=8
GRAPH_CLASSIFICATION_MODE=off
GRAPH_EMBEDDING_ENABLED=false
GRAPH_EMBEDDING_PROVIDER=local
GRAPH_EMBEDDING_MODEL_PATH=/absolute/path/to/local/model
GRAPH_EMBEDDING_MODEL_NAME=<model-name>
GRAPH_EMBEDDING_VERSION=document-semantic-v1
GRAPH_EMBEDDING_DIMENSION=384
GRAPH_VECTOR_INDEX_NAME=document_version_embedding_v1
GRAPH_VECTOR_TOP_K=12
GRAPH_VECTOR_MIN_SCORE=0.0
GRAPH_FEEDBACK_COLLECTION_ENABLED=true
GRAPH_CLASSIFICATION_ROLLOUT_PERCENT=10
GRAPH_FEEDBACK_EVAL_MIN_SAMPLES=100
MANAGED_PATH_CLASSIFICATION_PROFILE_DIR=./rules/managed-root-classification
MANAGED_PATH_DEFAULT_MODE=NONE
MANAGED_PATH_VECTOR_PILOT_LIMIT=1000
MANAGED_FILE_CLASSIFICATION_SYNC_LIMIT=20
MANAGED_FILE_CLASSIFICATION_BATCH_SIZE=20
```

受管目录文件分类不超过 `MANAGED_FILE_CLASSIFICATION_SYNC_LIMIT` 时在当前 AgentRun
内同步完成；超过阈值时创建 `CLASSIFY_MANAGED_FILES` 文件系统任务。部署环境必须同时运行
filesystem worker：

```bash
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python \
  -m app.modules.managed_files.worker
```

三层文件生命周期上线后，生产环境应拆分队列，避免归档或导入占满普通任务资源：

```bash
PYTHONPATH=apps/api FILESYSTEM_WORKER_ID=duplicate-archive-1 \
  FILESYSTEM_WORKER_QUEUES=DUPLICATE_CHECK,ARCHIVE \
  /opt/homebrew/anaconda3/envs/py311/bin/python -m app.modules.managed_files.worker

PYTHONPATH=apps/api FILESYSTEM_WORKER_ID=import-operation-1 \
  FILESYSTEM_WORKER_QUEUES=IMPORT,FILE_OPERATION \
  /opt/homebrew/anaconda3/envs/py311/bin/python -m app.modules.managed_files.worker

PYTHONPATH=apps/api FILESYSTEM_WORKER_ID=reconcile-1 \
  FILESYSTEM_WORKER_QUEUES=RECONCILE \
  /opt/homebrew/anaconda3/envs/py311/bin/python -m app.modules.managed_files.worker

PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python \
  -m app.modules.file_lifecycle.scheduler

PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python \
  -m app.modules.file_lifecycle.watcher
```

API 启动钩子、scheduler 和 watcher 都只创建 `filesystem_jobs`。实际 SHA-256 查重、归档、扫描、导入和暂存清理由 worker 完成。任务通过租约和幂等键恢复，状态接口为：

```text
GET /api/jobs/{job_id}
GET /api/jobs/{job_id}/events
```

部署挂载必须保持以下权限边界：

- API、Agent Runtime 和普通 Tool 对受管原始目录只读，不能挂载 `MANAGED_ROOT_ARCHIVE_WRITE_PATH`。
- 归档 worker 可以使用 `MANAGED_ROOT_ARCHIVE_WRITE_PATH`，但只允许追加，不允许覆盖、改名、移动或删除已有原始文件。
- import worker 读取受管原始目录并写 `WORKING_COPY_STORAGE_ROOT`。
- 文件操作 worker 只写 `WORKING_COPY_STORAGE_ROOT` 和 `TRASH_STORAGE_ROOT`。
- `TRASH_AUTO_PURGE_ENABLED` 在 MVP 必须保持 `false`。

首次上线顺序必须是：执行 `python -m alembic -c apps/api/alembic.ini upgrade head` 并确认当前 head
至少为 `20260722_0001`，准备三个目录并校验权限，启动分队列 worker，再启动 scheduler/watcher，
最后启动 API。API 健康不代表首次全量导入已经完成，应通过 job 状态和事件确认。

worker 使用 `MANAGED_FILE_CLASSIFICATION_BATCH_SIZE` 分页读取文件，并隔离单文件失败。
普通用户可通过 `GET /api/filesystem-jobs/{job_id}` 查询自己创建的任务；任务完成后会回写
原 AgentRun、分类建议、ChangeSet 和逐文件回执，聊天页会自动轮询并刷新。

首次上线顺序：

1. 保持 `GRAPH_CLASSIFICATION_ENABLED=false` 发布 API。
2. 执行数据库迁移：`python -m alembic -c apps/api/alembic.ini upgrade head`。
3. 安装图谱依赖，准备本地 Embedding 模型并验证 Neo4j 网络连接。
4. 为需要弱标签治理的受管根创建 `rules/managed-root-classification/<root_key>.json`；没有 Profile 的弱标签目录保持 `UNKNOWN`。
5. 执行首次事实投影：

   ```bash
   PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python \
     -m app.modules.knowledge_graph.cli sync-all
   ```

6. 访问 `GET /api/health`，确认 `knowledge_graph.status=ok`、`graphrag_package=available` 和 `embedding_package=available`。
7. 设置 `GRAPH_CLASSIFICATION_ENABLED=true`、`GRAPH_EMBEDDING_ENABLED=true`、`GRAPH_CLASSIFICATION_MODE=shadow`，重启并完成分类 smoke test。
8. 分层生成首批最多 1,000 份文档向量：

   ```bash
   PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python \
     -m app.modules.knowledge_graph.cli sync-embeddings --limit 1000
   ```

9. Shadow 链路稳定后才设置 `GRAPH_CLASSIFICATION_MODE=enabled`；初始仅按 `GRAPH_CLASSIFICATION_ROLLOUT_PERCENT` 小范围展示建议。
10. 用户在分类证据展开区明确选择“正确、错误、更正”后，反馈写入 PostgreSQL；未操作不计样本。
11. 有效反馈达到 `GRAPH_FEEDBACK_EVAL_MIN_SAMPLES` 后冻结分层评测集，离线回放通过并人工批准后才能扩大范围。

分类反馈接口：

```text
POST /api/classification/suggestions/{suggestion_id}/feedback
GET  /api/classification/feedback/summary
```

`sync-all` 和 `sync-embeddings` 都会写入 `graph_projection_runs`。单文件向量失败不会阻塞同批其他文件；
相同 SHA-256、模型、版本和维度全部一致时复用已有向量。

如果 Neo4j 查询超时或不可用，分类服务会记录 `classification.graph_query.degraded`，并自动回退到现有
规则/LLM 分类，不中断上传、解析、OCR 和其他文件。立即回滚只需要：

```text
GRAPH_CLASSIFICATION_ENABLED=false
GRAPH_CLASSIFICATION_MODE=off
GRAPH_EMBEDDING_ENABLED=false
NEO4J_SYNC_ENABLED=false
```

第二版本仍不启用自动实体构图、自由 Cypher、Text2Cypher 或 GraphRAG 文件问答。`VectorCypherRetriever`
只使用后端固定遍历模板，普通用户响应不会暴露相似来源文件身份。

## 9. 当前限制

- 当前已接入 OpenAI-compatible LLM 意图理解；默认 `LLM_ENABLED=false` 时仍使用 `DeterministicPlanner`。
- 当前已持久化 user、default workspace、message、AgentRun、ToolInvocation、Document、document_insights、document_extraction_runs、document_pages、document_classification_runs、document_category_suggestions、document_category_feedback、change_sets、change_items、operation_plans 和 operation_confirmations。
- OperationPlan 已支持工作副本重命名、移动、移入回收站和恢复；重命名、移动不新增工作副本版本，所有路径变更写入 `working_copy_path_records`。受管原始目录保持不变；自动永久删除和覆盖仍未开放。
- 没有白名单执行器的 OperationPlan 不能确认，计划保持 `WAITING_CONFIRMATION`，不会伪造 `EXECUTED`。
- 当前已支持读取当前用户自己的原始文件元信息和解析文本内容；其他多数 Tool handler 仍是结构化占位实现。
- 当前已有最小 JWT 鉴权，但没有 refresh token、复杂 RBAC、ACL 或 admin 权限体系。
- 当前前端已有注册、登录、Chat、异步上传状态和逐文件重复确认卡；工作副本与回收站已有后端 API，独立文件管理界面仍待补充。

## 10. 维护规则

以下任一内容发生变化时，必须同步更新本文和 `README.md`：

- 启动命令。
- 服务端口或 host。
- Python 环境或依赖安装方式。
- 前端依赖安装方式或启动命令。
- 测试命令。
- 新增或删除可直接调用的接口。
- 当前限制被解除，例如接入数据库、真实文件解析、大模型 Planner 或鉴权。
