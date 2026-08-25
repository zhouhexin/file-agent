# 旧版 Office 统一持久化派生件实施方案

> 制定日期：2026-08-25
> 实施状态：待实施
> 适用格式：`.doc -> .docx`、`.xls -> .xlsx`
> 适用范围：上传文件、受管源目录分析、工作副本解析、重命名分析、表格分析与表格工作台

## 1. 目标

将旧版 Word 和旧版 Excel 的 LibreOffice 转换统一为可追溯、可校验、可跨处理运行复用的持久化派生件：

- `.doc` 首次转换生成 `CONVERTED_DOCX`，后续解析复用持久化 `.docx`。
- `.xls` 首次转换生成 `CONVERTED_XLSX`，后续正文抽取、Profile、统计分析和质量校验复用同一持久化 `.xlsx`。
- 普通查询直接读取已有 `document_pages`、chunks 和索引，不触发文件转换。
- “重新解析”只重建页面、分类或索引，默认复用有效派生件。
- 只有源文件变化、转换指纹变化、派生件损坏/丢失或用户显式要求“重新转换”时，才重新调用 LibreOffice。
- 原始 `.doc`、`.xls` 永不被覆盖；下载、改名、移动等真实文件操作仍以原件为对象。
- 每个派生件都关联明确的 `DocumentVersion`、源 SHA-256、转换器版本和转换规则指纹。

## 2. 当前状态与问题

### 2.1 `.doc`

当前 `.doc` 已有持久化能力：

```text
ReadableDocumentSourceResolver
-> LegacyOfficeConversionService.get_or_create_docx()
-> document_artifacts(CONVERTED_DOCX)
-> storage/derivatives/office/...
-> python-docx / Docling 读取派生 DOCX
```

它已经支持同 Document 复用、相同源内容跨 Document 复用、哈希校验和转换审计。实施时保留现有行为，并完成以下统一：

- 派生文件落盘前始终创建和校验目标父目录。
- 将只面向 DOC 的服务抽象成旧版 Office 通用派生件服务。
- 显式关联 `DocumentVersion`，不再只依靠 `Document.id + source_sha256` 间接追溯。
- 日志、ChangeSet 和元数据使用格式无关的公共字段。

### 2.2 `.xls`

当前 `.xls` 每个读取入口都会创建临时目录并执行一次转换：

```text
extract_document_text
profile_workbook
SpreadsheetAnalysisService
SpreadsheetWorkbenchService
executor.iter_data_rows
-> prepared_spreadsheet_path()
-> TemporaryDirectory
-> convert_xls_to_xlsx()
-> 临时 source.xlsx
-> 临时目录清理
```

由此产生四个问题：

1. 显式重新解析或不同 Tool 读取同一 `.xls` 时会重复启动 LibreOffice。
2. Profile、统计执行和公式校验可能分别转换一次，单个任务内也可能重复。
3. 转换结果没有 `document_artifacts` 记录，无法从文件谱系核对具体转换版本。
4. `.doc` 和 `.xls` 的错误码、审计、缓存和清理策略不一致。

## 3. 非目标

本次只统一旧版 Office 的可读格式转换，不扩大到以下能力：

- 不持久化普通 `.xlsx`、`.docx` 的无变化副本。
- 不把 CSV/TSV 转成 XLSX。
- 不执行 Office 宏、外部链接或用户脚本。
- 不实现表格编辑、公式重算或格式修复。
- 不改变搜索、分类、证据回答的业务语义。
- 不让 LLM 获取绝对路径、直接调用 LibreOffice 或决定缓存键。
- 不用持久化转换结果代替 `document_pages`、chunks 或证据索引。

## 4. 总体设计

### 4.1 统一链路

```text
已授权 Document + DocumentVersion + 原件路径
-> ReadableDocumentSourceResolver
-> 判断是否为旧版格式
   -> 非旧版格式：返回原件可读源
   -> .doc：请求 CONVERTED_DOCX
   -> .xls：请求 CONVERTED_XLSX
-> OfficeDerivativeService.get_or_create(...)
   -> 校验原件路径、大小、版本 SHA-256
   -> 查询当前版本派生件记录
   -> 查询相同源哈希与转换指纹的共享物理派生件
   -> 命中：验证文件并建立当前版本引用
   -> 未命中：隔离转换、结构校验、原子发布、登记派生件
-> 返回 ReadableDocumentSource
-> 现有解析器、表格 Profile、分析器或校验器读取 parse_path
-> 正文与证据仍写入原始 Document/DocumentVersion 的页面和索引
```

### 4.2 分层职责

#### `LibreOfficeConversionRunner`

只负责一次隔离转换：

- 输入为后端已校验的源路径、输出格式规格和临时目录。
- 使用独立 LibreOffice profile。
- 通过参数列表调用进程，禁止 `shell=True`。
- 返回临时输出路径，不访问数据库、不决定持久化路径。
- `.docx` 使用 OOXML ZIP + `python-docx` 校验。
- `.xlsx` 使用 OOXML ZIP + `openpyxl(read_only=True, data_only=True)` 校验。

#### `OfficeDerivativeService`

负责派生件生命周期：

- 校验 `DocumentVersion.sha256` 与原件实际 SHA-256 一致。
- 计算转换指纹。
- 查询、验证、创建和复用 `DocumentArtifact`。
- 生成内容寻址存储路径。
- 在目标卷同目录写入 `.part`，再用 `os.replace` 原子发布。
- 处理并发收敛、失败清理和结构化日志。

保留 `LegacyOfficeConversionService.get_or_create_docx()` 作为短期兼容入口，内部委托给新服务；所有调用方迁移完成后再考虑移除，避免一次重构破坏已有 DOC 链路。

#### `ReadableDocumentSourceResolver`

作为生产读取的唯一转换入口：

- 接收请求级 `Session`、`Document`、`DocumentVersion` 和后端解析出的原件路径。
- 对 `.doc` 返回持久化 `.docx`。
- 对 `.xls` 返回持久化 `.xlsx`。
- 对其他格式直接返回原件。
- 返回 `artifact_id`、`artifact_type`、原格式、解析格式、转换器版本、是否复用和解析配置指纹。
- 请求内缓存键使用 `(document_version_id, purpose, force_reconvert)`，避免同一任务重复解析可读源。

#### 解析器与表格业务服务

只消费已经解析好的 `parse_path`：

- 不创建数据库 Session。
- 不自行查找 LibreOffice。
- 不在底层函数中隐式生成临时转换结果。
- 不接触原件权限判断。

## 5. 派生件规格

| 源格式 | 派生类型 | 输出格式 | MIME | 转换规则版本 |
|---|---|---|---|---|
| `.doc` | `CONVERTED_DOCX` | `.docx` | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | `legacy-doc-to-docx-v2` |
| `.xls` | `CONVERTED_XLSX` | `.xlsx` | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | `legacy-xls-to-xlsx-v1` |

物理路径：

```text
derivatives/office/{source_sha256[0:2]}/{source_sha256}/{converter_config_hash}.docx
derivatives/office/{source_sha256[0:2]}/{source_sha256}/{converter_config_hash}.xlsx
```

路径只保存为相对 `FILE_STORAGE_ROOT` 的 POSIX 路径。任何解析后的目标路径都必须仍位于存储根目录内。

## 6. 缓存键与复用规则

转换指纹至少包含：

```text
converter_name
converter_runtime_version
source_format
target_format
LibreOffice export filter
固定命令参数版本
格式校验器版本
conversion_rule_version
```

只有同时满足以下条件才允许复用：

```text
artifact_type 相同
+ source_sha256 相同
+ converter_config_hash 相同
+ storage_backend 可读取
+ 文件存在且位于受控存储根目录
+ size_bytes 与记录一致
+ 派生文件 SHA-256 与记录一致
+ 对应 DOCX/XLSX 结构校验通过
```

复用分两级：

1. 当前 `DocumentVersion` 已有关联记录：直接复用。
2. 其他 Document/Version 有相同物理派生件：为当前版本创建独立数据库引用，但共享 `storage_path`。

独立记录用于权限、谱系和审计；共享物理文件只用于节省空间，不能据此授予跨文件访问权限。

## 7. 数据库与 Alembic

### 7.1 `document_artifacts` 扩展

新增：

```text
document_version_id varchar(36) null -> FK document_versions.id
metadata_json jsonb not null default '{}'
```

`metadata_json` 只保存非敏感转换事实，例如：

```json
{
  "source_format": "xls",
  "parsed_format": "xlsx",
  "conversion_rule_version": "legacy-xls-to-xlsx-v1",
  "validation": "ooxml+openpyxl"
}
```

迁移策略：

1. 新增可空字段和索引，避免直接阻塞已有数据。
2. 对现有 `CONVERTED_DOCX` 按 `document_id + source_sha256` 回填匹配的 `document_versions.id`。
3. 无法唯一回填的历史记录保留为空并输出迁移审计统计，不猜测版本。
4. 新代码创建派生件时强制提供 `document_version_id`。
5. 唯一约束增加版本维度：

```text
(document_version_id, artifact_type, source_sha256, converter_config_hash)
```

6. 在历史空值清理完成前保留现有 Document 级唯一约束；确认所有生产数据均可追溯后，再通过独立迁移收紧为非空并移除旧约束。

不新增单独的 XLSX 表，也不把二进制内容存入数据库。

### 7.2 Repository 接口

`DocumentArtifactRepository` 增加版本参数：

```python
get_for_version(...)
get_reusable_physical_artifact(...)
upsert_version_link(...)
list_for_version(...)
count_by_storage_path(...)
```

旧 `get_for_document()`、`upsert_link()` 在迁移期保留兼容，但新转换链路不得继续使用它们创建记录。

## 8. 读取链路改造

### 8.1 上传文件与工作副本正文抽取

调整 `extract-document-text` handler：

1. 由 `FileExtractionRepository` 解析已授权原件和当前 `DocumentVersion`。
2. 调用 `ReadableDocumentSourceResolver.resolve()`。
3. 把 `parse_path`、`parse_filename` 和 `parse_content_type` 传给 `extract_document_text()`。
4. 把转换元数据写入 extraction result、page metadata 和 ChangeSet。
5. 复用成功 extraction 时不调用 resolver；只有需要真实解析时才解析可读源。

### 8.2 受管源目录分析

`ManagedSourceAnalysisService` 已为每个源修订创建逻辑 `Document + DocumentVersion`，因此直接复用统一 resolver：

- 使用 `ManagedFileRevision.content_sha256`/本次计算哈希校验源版本。
- `.xls` 首次 SOURCE_ANALYSIS 创建 `CONVERTED_XLSX`。
- 后续工作副本物化如果源 SHA-256 和转换指纹相同，可共享物理派生件，但工作副本 DocumentVersion 保留独立引用。
- 源文件变化会创建新 revision/version，因此不会误用旧派生件。

### 8.3 表格正文抽取

删除 `_extract_legacy_xls_text()` 内直接创建 `TemporaryDirectory` 的生产行为。`extract_document_text()` 接收到的应当已经是 `.xlsx` 可读源，并继续复用现有 Excel 解析逻辑。

为兼容独立解析单元测试，可保留一个明确命名的低层转换函数，但它只能由 `OfficeDerivativeService` 调用，不能继续由业务模块直接调用。

### 8.4 表格 Profile、统计与校验

当前以下入口会直接调用 `prepared_spreadsheet_path()`：

- `spreadsheet_analysis.profiler`
- `spreadsheet_analysis.executor`
- `spreadsheet_workbench.service`

改造方式：

1. Tool handler 在权限校验后统一解析一次 `ReadableDocumentSource`。
2. `SpreadsheetAnalysisService` 和 `SpreadsheetWorkbenchService` 接收原始显示信息以及 `parse_path`/`parse_format`。
3. Profile、执行器和公式检查直接读取持久化 `.xlsx`。
4. 同一分析任务的 Profile、数据扫描和公式校验共享一个 parse path。
5. 对外结果仍显示原始 `.xls` 文件名和文件类型，证据仍定位到原文件对应的 Sheet/单元格，不向用户暴露派生路径。
6. 移除 `prepared_spreadsheet_path()` 的生产调用；完成迁移后将其删除或降为测试私有辅助函数。

## 9. 解析指纹与重处理语义

`expected_parser_config_hash()` 对旧格式组合两层指纹：

```text
hash(
  readable-source-schema-version
  + conversion_config_hash
  + downstream_parser_config_hash
)
```

行为定义：

| 用户/系统动作 | 页面/索引 | 派生件 | LibreOffice |
|---|---|---|---|
| 普通检索或问答 | 复用 | 不读取或复用 | 不调用 |
| 表格分析/Profile/校验 | 按需读取 | 复用 | 仅首次/失效时调用 |
| 重新解析/重新读取/重跑 | 重建 | 复用 | 不调用 |
| 重新建索引 | 重建索引 | 不处理 | 不调用 |
| 显式重新转换 | 后续重建 | 重新生成 | 调用 |
| 源 SHA-256 变化 | 新版本重建 | 新派生件 | 调用 |
| 转换器或规则指纹变化 | 重新解析 | 新派生件 | 调用 |
| 派生件丢失或校验失败 | 重新解析 | 修复/重建 | 调用 |

`force_reconvert=true` 必须同时导致本次解析不复用旧 extraction，否则会出现“要求重新转换但直接返回旧页面”的假执行。

## 10. 审计、ChangeSet 与回执

统一 extraction 元数据：

```text
conversion_artifact_id
conversion_artifact_type
conversion_reused
conversion_source_format
conversion_parsed_format
conversion_converter
conversion_converter_version
conversion_config_hash
```

ChangeItem 类型：

```text
DOCX_DERIVATIVE_CREATED
DOCX_DERIVATIVE_REUSED
XLSX_DERIVATIVE_CREATED
XLSX_DERIVATIVE_REUSED
OFFICE_DERIVATIVE_REBUILT
OFFICE_DERIVATIVE_VALIDATION_FAILED
```

创建/复用派生件是低风险分析副作用，不需要 OperationPlan，但必须记录 ToolInvocation、ChangeSet/ChangeItem 或 SOURCE_ANALYSIS 审计事实。用户回执应继续声明原件未修改。

日志事件沿用：

```text
file.derivative.convert.started
file.derivative.convert.completed
file.derivative.convert.reused
file.derivative.convert.failed
```

日志包含 `document_id`、`document_version_id`、`artifact_id`、源/目标格式、转换器、版本、状态、耗时和错误码；禁止记录正文、绝对原件路径、完整命令行或密钥。

## 11. 错误与降级

公共错误码：

```text
OFFICE_CONVERSION_DISABLED
LIBREOFFICE_NOT_AVAILABLE
OFFICE_SOURCE_NOT_FOUND
OFFICE_SOURCE_HASH_MISMATCH
OFFICE_SOURCE_TOO_LARGE
OFFICE_CONVERSION_TIMEOUT
OFFICE_CONVERSION_FAILED
OFFICE_OUTPUT_MISSING
OFFICE_OUTPUT_INVALID
DERIVATIVE_PATH_INVALID
DERIVATIVE_WRITE_FAILED
DERIVATIVE_RECORD_FAILED
```

对外可保留现有 DOC/XLS 细分错误码映射，避免破坏 API 和测试。

降级策略：

- `.doc` 保留当前受控纯文本 fallback，但明确标记转换失败和解析质量下降。
- `.xls` 没有可靠的原生 fallback；转换失败时保留文件名、相对目录、类型等元数据索引，并返回可重试警告，不能伪装成成功正文解析。
- 持久化写入失败不得返回临时转换路径，因为临时目录会被清理，且会绕过派生件审计。
- 已有有效派生件时，即使 LibreOffice 当前不可用，也允许复用；只有必须新转换时才报告转换器不可用。

## 12. 并发、原子性和清理

### 12.1 原子发布

1. 在系统临时目录隔离转换。
2. 验证输出。
3. 创建目标父目录并确认其位于存储根目录内。
4. 在目标同目录创建唯一 `.part`。
5. copy + flush + fsync。
6. `os.replace(.part, final)` 原子提交。
7. 最后写入/刷新数据库引用。

任何异常都删除本次 `.part` 和临时目录，不删除已存在且校验有效的正式派生件。

### 12.2 并发收敛

- 物理路径由源哈希和转换指纹确定。
- 多 worker 可以并行完成临时转换，但发布前再次检查目标文件。
- 数据库唯一约束冲突时回滚局部写入并重新查询获胜记录。
- 禁止一个失败 worker 删除另一个 worker 已发布的有效文件。

### 12.3 引用清理

- 删除 DocumentVersion/Document 时先删除其派生件引用。
- 只有 `count_by_storage_path == 0` 时才删除物理派生件。
- 删除后仅清理 `derivatives/office` 内的空父目录，不能越过配置的派生根目录。
- 增加运维审计任务：报告“数据库有记录但文件缺失”和“物理文件存在但无引用”，默认只报告，不自动删除。

## 13. 配置

继续复用现有配置：

```dotenv
LEGACY_OFFICE_CONVERSION_ENABLED=true
LEGACY_OFFICE_CONVERTER=libreoffice
LIBREOFFICE_EXECUTABLE=
LEGACY_OFFICE_CONVERSION_TIMEOUT_SECONDS=90
LEGACY_OFFICE_MAX_FILE_SIZE_MB=100
LEGACY_OFFICE_DERIVATIVE_DIR=derivatives/office
MANAGED_SOURCE_LIBREOFFICE_CONCURRENCY=1
```

不增加 DOC/XLS 两套重复路径配置。配置说明更新为同时覆盖 `.doc` 和 `.xls` 持久化派生件。

## 14. 代码改动清单

### 核心实现

- `apps/api/app/modules/files/office_conversion.py`
  - 抽取通用规格、runner 和 `OfficeDerivativeService`。
  - 增加 `CONVERTED_XLSX` 支持。
  - 保留 DOC 兼容接口。
  - 统一原子写入和校验。
- `apps/api/app/modules/files/readable_source.py`
  - 接收 `DocumentVersion`。
  - 同时解析 `.doc` 和 `.xls`。
  - 扩展转换元数据和 parser config hash。
- `apps/api/app/modules/files/artifact_repository.py`
  - 增加版本级查询、登记和并发收敛。
- `apps/api/app/modules/files/extractors.py`
  - 移除生产链路的临时 XLS 转换。
- `apps/api/app/modules/spreadsheet_analysis/conversion.py`
  - 只保留隔离转换和 XLSX 校验底层能力，或合并入统一 runner。
- `apps/api/app/modules/spreadsheet_analysis/profiler.py`
- `apps/api/app/modules/spreadsheet_analysis/executor.py`
- `apps/api/app/modules/spreadsheet_analysis/service.py`
- `apps/api/app/modules/spreadsheet_workbench/service.py`
  - 改为消费已解析的持久化可读源。
- `apps/api/app/modules/agent/tool_registry.py`
  - 表格 Tool 和正文抽取 Tool 在 handler 边界解析可读源。
- `apps/api/app/modules/managed_files/source_analysis.py`
  - 传递明确 `DocumentVersion` 并记录 XLSX 派生信息。
- `apps/api/app/modules/changesets/service.py`
  - 按源/目标格式生成 DOCX 或 XLSX ChangeItem。

### 数据库与文档

- `apps/api/app/db/models.py`
- `apps/api/alembic/versions/<revision>_versioned_office_derivatives.py`
- `apps/api/.env.example`
- `.env.example`
- `docs/database-schema.md`
- `docs/api-contract.md`
- `docs/legacy-doc-docx-derivative-implementation-plan.md`

## 15. 实施阶段

### 阶段 A：基线与数据库

1. 固化当前 DOC 持久化和 XLS 临时转换测试。
2. 新增 Alembic 迁移、模型和 Repository 版本关联。
3. 验证迁移同时兼容上传 Document、受管源分析 Document 和工作副本 Document。

### 阶段 B：统一转换服务

1. 抽取格式规格和通用转换流程。
2. 接入 `CONVERTED_XLSX`、XLSX 双重校验和内容寻址路径。
3. 修复并覆盖目标父目录首次不存在、跨卷发布和并发写入场景。
4. 保持现有 DOC 行为和错误兼容。

### 阶段 C：统一可读源

1. Resolver 接入 `.xls`。
2. 正文抽取和受管源分析改用持久化 XLSX。
3. 扩展解析指纹和转换元数据。

### 阶段 D：表格能力去临时转换

1. `analyze-spreadsheet` 在 Tool handler 只解析一次可读源。
2. Profile 和 executor 共享同一路径。
3. 表格工作台 Profile、公式校验共享同一路径。
4. 移除所有生产 `prepared_spreadsheet_path()` 调用。

### 阶段 E：审计、清理和文档

1. 扩展 ChangeSet、日志和文件谱系。
2. 增加引用安全清理和孤儿审计。
3. 更新环境示例、数据库和 API 文档。

### 阶段 F：回归与真实样本验证

1. 运行定向测试。
2. 运行完整后端测试。
3. 使用非敏感真实 `.doc`、`.xls` 样本各执行首次解析、重新解析、重新转换。
4. 核对 LibreOffice 调用次数、artifact 数量、物理文件数量和页面结果。

## 16. 测试方案

### 16.1 单元测试

- `.doc/.xls` 转换规格、过滤器和指纹互不混淆。
- 显式 `LIBREOFFICE_EXECUTABLE` 优先，Windows 优先可等待的 `soffice.com`。
- 输出目录首次不存在时能够安全创建。
- DOCX/XLSX 合法、缺失、伪 ZIP、结构损坏和 openpyxl/python-docx 打开失败。
- 同版本复用、同 Document 不同版本隔离、跨 Document 相同内容物理复用。
- 源哈希变化、转换器版本变化和 `force_reconvert` 失效旧缓存。
- 派生文件大小/哈希不一致时重建。
- `.part` 失败清理、跨卷写入、并发唯一约束冲突收敛。
- 最后引用删除前不删除共享物理文件。

### 16.2 链路测试

- 首次抽取 `.xls` 创建 `CONVERTED_XLSX` 和 `document_pages`。
- 第二次普通读取复用成功 extraction，不访问 LibreOffice。
- 强制重新解析重建页面但复用 XLSX。
- 强制重新转换新建/替换派生件并重建页面。
- `profile-spreadsheet`、`validate-spreadsheet`、`analyze-spreadsheet` 复用同一 artifact。
- 同一个表格分析任务只解析一次可读源。
- 受管源 SOURCE_ANALYSIS 的 `.xls` 建立派生件；工作副本不重复物理转换。
- `.doc` 现有解析、重命名、分类和 fallback 行为不回归。
- ChangeSet 分别生成 DOCX/XLSX CREATED/REUSED。
- 原始 `.doc/.xls` 字节和 SHA-256 始终不变。

### 16.3 迁移测试

- 空数据库升级到 head。
- 从现有双 head 合并后的正式 head 升级。
- 已有 `CONVERTED_DOCX` 数据回填版本关联。
- 无法回填的历史记录不导致迁移失败，并产生可核对统计。
- downgrade 只移除新增字段/约束，不删除物理派生文件。

### 16.4 验证命令

实施时至少执行：

```text
pytest app/tests/test_office_conversion_service.py
pytest app/tests/test_spreadsheet_workbench.py
pytest app/tests/test_file_extraction_tools.py
pytest app/tests/test_managed_source_analysis_fallbacks.py
pytest app/tests/test_persistent_runtime.py
pytest app/tests
alembic upgrade head
git diff --check
```

## 17. 验收标准

- 首次读取 `.doc` 或 `.xls` 后存在合法、可追溯到 DocumentVersion 的持久化派生件。
- 第二次正文抽取、Profile、表格分析、校验、分类或重命名读取不重复调用 LibreOffice。
- 普通问答和检索只消费已持久化页面/索引，不触发转换。
- “重新解析”不重新转换；“重新转换”确实重新调用 LibreOffice。
- 同源同转换指纹可共享物理文件，不共享权限记录。
- 源变化、派生件损坏和转换版本变化不会误用旧结果。
- `.doc`、`.xls` 原件字节保持不变。
- 受管源分析和工作副本链路行为一致。
- 所有生产 `.xls` 入口不再调用临时 `prepared_spreadsheet_path()`。
- ChangeSet、日志和文件谱系可以区分创建、复用、重建与失败。
- 定向测试、完整后端测试、Alembic 升级和 `git diff --check` 全部通过。

## 18. 风险与回滚

### 风险

- 表格分析调用点较多，遗漏一个入口会继续发生临时转换。
- 持久化派生件增加磁盘占用，需要引用安全清理和孤儿审计。
- LibreOffice 不同版本可能产生字节不同但语义等价的输出，因此缓存必须包含运行时版本。
- 并发 worker 同时处理相同源文件时可能重复消耗一次转换 CPU，但最终记录和物理路径必须收敛。
- 旧 `document_artifacts` 缺少版本字段，回填必须保守，不能猜测。

### 回滚

- 代码回滚时保留新增 `CONVERTED_XLSX` 文件和数据库记录，不自动删除。
- 旧代码会忽略未知 artifact type，不影响原件和已有页面。
- 数据库字段先保持可空，便于应用回滚。
- 如 XLS 持久化链路出现生产问题，可通过格式级内部开关暂时回退到“转换失败并保留元数据索引”，不回退到未经审计的临时成功路径。
