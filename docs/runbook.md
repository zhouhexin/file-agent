# File Agent Runbook

本文记录当前项目的本地启动、验证方式和可用接口。后续如果端口、命令、环境依赖、启动顺序或接口能力发生变化，必须同步更新本文和 `README.md`。

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
69 passed
```

如果出现 `urllib3` 或 `LangChainPendingDeprecationWarning`，目前属于环境兼容警告，不影响现有测试结果。

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
展示 AgentRun 状态、intent 和 Tool 调用列表
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
curl http://127.0.0.1:8000/api/agent/tools
```

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
extract-document-text：解析 txt/md/csv/xls/xlsx/doc/docx/pdf/image，并将文本写入 document_pages；旧版 `.xls` 优先通过 xlrd 直接读取，失败后才尝试 LibreOffice 临时转换，不覆盖原件
```

旧版 `.doc` 使用“持久派生件”链路：首次读取时通过 LibreOffice Headless 转换为 `.docx`，保存到
`FILE_STORAGE_ROOT/derivatives/office/` 并登记到 `document_artifacts`。后续解析、Docling、分类、摘要、
问答和重命名字段提取复用同一派生件；原始下载与真实改名仍操作 `.doc` 原件。用户说“重新解析”时
复用有效派生件，只重建解析结果；说“重新转换”时才同时绕过派生件缓存。

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
可靠退出码；路径包含空格无需手工加引号。LibreOffice 不可用或转换失败时，系统保留原有纯文本回退，
并在解析警告中说明降级原因，不会覆盖原件。

PDF、Excel、doc/docx 和图片 OCR 依赖：

```text
PyMuPDF
openpyxl
python-docx
Pillow
paddleocr
textutil 或 LibreOffice
xlrd>=2.0.1（默认直接读取旧版 .xls）
LibreOffice（可选兜底；后续遇到 xlrd 无法处理的非标准、损坏或伪装 .xls 时再安装）
```

图片 OCR 和扫描 PDF OCR 默认使用 PaddleOCR CPU Provider；旧版 `.doc` 优先使用 LibreOffice 生成持久化 `.docx` 派生件，失败后再使用现有纯文本回退；旧版 `.xls` 默认使用 xlrd，LibreOffice 只作为可选 headless 转换兜底。如果缺少依赖或 OCR/转换引擎不可用，Tool 会返回结构化错误，不会读取任意路径。

当前对话触发解析已支持多个附件顺序执行。单个文件 Tool 异常会记录为该文件的失败 `document_results.errors`，后续文件继续处理；并发执行、LangGraph map/reduce、步骤级重试和恢复后续单独实现。

查询 AgentRun：

```bash
curl http://127.0.0.1:8000/api/agent-runs/<agent_run_id>
```

查询 Tool 调用：

```bash
curl http://127.0.0.1:8000/api/agent-runs/<agent_run_id>/tool-invocations
```

非法附件示例：

```bash
curl -X POST http://127.0.0.1:8000/api/conversations/conv-1/messages \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <access_token>' \
  -d '{"content":"帮我读取文件","attachments":[{"filename":"bad.pdf"}]}'
```

当前期望返回 HTTP `422`，因为附件缺少 `document_id`。

## 7. 受管文件重命名执行器

受管文件重命名必须先生成 `RENAME_FILES` OperationPlan，再由计划创建者确认。默认执行器为：

```text
FILE_RENAME_PARSE_MODE=hybrid
FILE_RENAME_EXECUTOR=native
FILE_RENAME_MAX_BATCH_SIZE=20
FILE_RENAME_EXECUTION_TIMEOUT_SECONDS=60
```

`FILE_RENAME_PARSE_MODE` 控制命名字段的解析来源，与执行器配置相互独立：

- `hybrid`：Docling 与原生解析器生成候选并逐字段仲裁，默认用于生产。
- `native`：只使用原生解析器，Docling 质量异常时用于紧急回退。
- `docling`：只使用 Docling，主要用于对比测试和问题定位；Docling 不可用时仍安全回退原生解析。

F2 v2.2.2 是可选批量执行器。F2 不提取年份、文号或标题，只执行后端已经确认的同目录
`before -> after` 映射。切换前需要把对应平台的 F2 二进制放入离线部署包，核对发布资产
SHA-256，并配置：

```text
FILE_RENAME_EXECUTOR=f2
F2_BINARY_PATH=/absolute/deployment/path/f2
F2_EXPECTED_VERSION=2.2.2
F2_FALLBACK_TO_NATIVE=false
F2_STDOUT_MAX_BYTES=1048576
```

启动 API 前执行：

```bash
"$F2_BINARY_PATH" --version
sha256sum "$F2_BINARY_PATH"
```

macOS 可使用 `shasum -a 256 "$F2_BINARY_PATH"`。版本或哈希不符合离线包清单时不得启用。
配置为 F2 后，二进制缺失、版本不一致、超时、非 JSON 输出或 dry-run 与 OperationPlan
不一致都会拒绝执行。回退时显式改为 `FILE_RENAME_EXECUTOR=native` 并重启 API。

自动提取缺少年份或正文标题时，文件会进入 `file_rename_review_items`，不会进入原执行批次。
用户可在聊天回执中点击文件查看内容，并使用以下格式更正：

```text
文件原文件名更正为新文件名
```

该消息视为当前更正的执行确认，但服务仍会先创建 `RENAME_FILES` OperationPlan 和
OperationConfirmation，再执行并写入 ChangeSet。原文件名重复时应改用回执返回的完整相对路径；
目标名称冲突只失败当前项，不阻塞同一消息中的其他文件。回复“不需要”会关闭当前会话的待复核项。

自动生成建议时使用 `VERSION_SUFFIX` 冲突策略。基础目标名称视为第一版；如果已经存在，则在扩展名前
生成 `_第二版`，后续依次生成 `_第三版`、`_第四版`。冲突检查同时覆盖真实文件系统、
`managed_files` 索引和同一批次内已经预留的目标名称。该版本名称必须先写入 OperationPlan，
F2 不得使用自身的冲突修复参数再次修改目标名称。用户手工明确指定的目标名称仍采用冲突提示，
避免即时确认链路擅自改变用户输入。

旧版 `.xls` 默认使用 `xlrd>=2.0.1` 直接读取，不要求安装 LibreOffice。只有 xlrd 无法读取时才尝试
LibreOffice 转换；当前阶段允许不安装该可选兜底。如果直接读取、转换或表格正文解析失败，但文件名同时包含可验证年份和
可清理标题，重命名服务允许使用表格文件名回退生成待确认建议，并继续保留失败的 ExtractionRun。
文件名回退只适用于 `.xls/.xlsx/.xlsm/.csv/.tsv`，可清理前导“附件”、括号日期、末尾提交单位加
八位日期、`new` 标记和“摸底统计表”中的“摸底”。该回退不能伪造正文证据，也不能用于分类结论。

只有设置 `RUN_F2_INTEGRATION_TESTS=true` 时，测试套件才会调用本机真实 F2：

```bash
RUN_F2_INTEGRATION_TESTS=true \
F2_BINARY_PATH=/absolute/path/f2 \
PYTHONPATH=apps/api \
/opt/homebrew/anaconda3/envs/py311/bin/python -m pytest \
  apps/api/app/tests/test_rename_executors.py -k real_f2 -q
```

### 7.1 上传附件临时重命名

聊天消息携带上传附件并要求重命名时，Planner 会把后端已解析的 `document_ids` 交给
`generate-rename-suggestions`，生成 `RENAME_UPLOADED_FILES` OperationPlan。确认前不会修改文件；
确认后只更新该 Document 的 `original_filename`，并把本地 FileObject 放入：

```text
FILE_STORAGE_ROOT/<user_id>/<document_id>/<new_basename>
```

目标目录和路径完全由后端计算，OperationPlan 只保存 basename。当前阶段不生成分类建议、不选择
受管目录，也不执行正式归档。若物理内容被其他 Document 或受管快照共享，执行器会写时复制到
当前 Document 私有目录，其他引用不变。成功和失败都写入 `confirmed-file-action` ToolInvocation、
ChangeSet 和逐文件结果。

上传去重分为两个层级：完全相同且尚未发送的同名草稿可以幂等复用同一 Document；不同文件名、
已进入消息或受管快照只复用底层 FileObject，并创建新的 `UPLOADED` Document。因此用户在发送消息前
删除新草稿时，不会再误命中受管快照的 `USED_IN_MESSAGE` 状态，也不会删除仍被其他 Document 引用的
物理内容。

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
- OperationPlan 已支持受管目录文件重命名的 Native/F2 闭环，以及上传附件在私有临时存储中的确认改名和 ChangeSet 审计；附件分类后写入受管目录尚未实现，移动、删除和覆盖也未开放真实执行。
- 没有白名单执行器的 OperationPlan 不能确认，计划保持 `WAITING_CONFIRMATION`，不会伪造 `EXECUTED`。
- 当前已支持读取当前用户自己的原始文件元信息和解析文本内容；其他多数 Tool handler 仍是结构化占位实现。
- 当前已有最小 JWT 鉴权，但没有 refresh token、复杂 RBAC、ACL 或 admin 权限体系。
- 当前前端已有最小注册、登录、Chat、文件上传和附件删除流程，没有会话列表、admin 页面或正式视觉设计。

## 10. 维护规则

以下任一内容发生变化时，必须同步更新本文和 `README.md`：

- 启动命令。
- 服务端口或 host。
- Python 环境或依赖安装方式。
- 前端依赖安装方式或启动命令。
- 测试命令。
- 新增或删除可直接调用的接口。
- 当前限制被解除，例如接入数据库、真实文件解析、大模型 Planner 或鉴权。
