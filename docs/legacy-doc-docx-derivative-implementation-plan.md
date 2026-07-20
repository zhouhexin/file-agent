# 旧版 DOC 转 DOCX 派生件复用实施计划

> 制定日期：2026-07-20  
> 实施状态：已完成  
> 适用范围：上传文件与已登记为 `Document` 的受管目录文件

## 1. 目标

首次读取旧版 `.doc` 文件时，通过 LibreOffice Headless 将其转换为 `.docx`，把转换结果作为可追溯、可复用的派生件保存。后续解析、重命名字段提取、分类、摘要和问答统一读取派生 `.docx`；下载、预览和真实重命名仍以原始 `.doc` 为对象。

核心原则：

- 原始 `.doc` 永不被转换结果覆盖。
- 当前项目以 `Document.id + Document.sha256` 表示不可变源文件版本。
- 转换结果必须持久化，不能只保存在进程临时目录。
- 转换、复用和失败必须可审计。
- Docling、python-docx 和其他读取方不得分别重复实现 `.doc` 转换。
- Windows、macOS、Linux 使用同一服务接口，仅可执行文件发现和进程清理策略不同。

## 2. 开源选型

### 2.1 第一版：LibreOffice Headless

第一版直接调用 LibreOffice `soffice --headless --convert-to`：

- 开源、跨平台，支持旧版 Word 导入和 OOXML 导出。
- Docling 对 `.doc/.xls/.ppt` 的新支持也要求 LibreOffice。
- 当前项目已有 LibreOffice 临时转换代码，可以抽取公共实现。
- 首次转换后会持久化复用，当前阶段不需要常驻转换服务。

### 2.2 第二阶段候选：unoserver

批量转换压力明确后，再把转换执行器扩展为 `unoserver`：

- 常驻 LibreOffice，避免每个文件重复启动。
- 需要独立进程监管、端口隔离、超时恢复和安全配置。
- 第一版只保留 `LEGACY_OFFICE_CONVERTER` 扩展点，不启动 unoserver。

### 2.3 不采用

- `unoconv`：项目已标记 deprecated，并推荐迁移到 unoserver。
- Gotenberg：主要面向 Docker 化 Office 转 PDF，不符合本阶段 `.doc -> .docx` 和本地无 Docker 的约束。
- 仅提取纯文本的工具：无法为 Docling 和结构化重命名提供 DOCX 结构。

## 3. 总体链路

```text
原始 .doc
-> 校验 Document、FileObject、源文件路径和 SHA-256
-> 查询 CONVERTED_DOCX 派生件
   -> 命中且文件有效：复用
   -> 未命中或失效：
      -> 独立临时目录和 LibreOffice profile
      -> LibreOffice 转换为 docx
      -> OOXML ZIP 与 python-docx 双重校验
      -> 原子写入 storage/derivatives/office/
      -> 写入 document_artifacts
-> 返回 ReadableDocumentSource
-> Docling 解析派生 docx
-> Docling 失败时 python-docx 回退
-> document_pages / document_elements 关联原始 Document
```

## 4. 数据模型

新增 `document_artifacts`：

```text
id                       uuid/string(36)
document_id              FK documents.id
artifact_type            CONVERTED_DOCX
storage_backend          local
storage_path             相对 file_storage_root
content_type             DOCX MIME
size_bytes               bigint
sha256                    派生件哈希
source_sha256             原件哈希
converter_name           libreoffice
converter_version        LibreOffice 版本
converter_config_hash    转换器和参数指纹
created_at
updated_at
```

唯一约束：

```text
(document_id, artifact_type, source_sha256, converter_config_hash)
```

跨用户同内容文件允许复用相同物理 `storage_path`，但每个用户对应的 `Document` 必须拥有独立 `document_artifacts` 记录，不能借此绕过文档权限。

## 5. 转换服务

新增：

```text
apps/api/app/modules/files/artifact_repository.py
apps/api/app/modules/files/office_conversion.py
apps/api/app/modules/files/readable_source.py
```

`LegacyOfficeConversionService.get_or_create_docx()` 负责：

1. 校验源文件扩展名、哈希和大小限制。
2. 读取当前 Document 的有效派生件。
3. 检查全局相同源哈希和转换指纹的可复用物理文件。
4. 在隔离目录中转换。
5. 校验 DOCX 后原子写入派生目录。
6. 返回 `artifact_id`、`parse_path`、转换器版本和是否复用。

禁止事项：

- 禁止 `shell=True`。
- 禁止把原始绝对路径、正文或命令完整参数写入日志。
- 禁止在原件目录直接生成输出。
- 禁止覆盖原件。
- 禁止无限等待 LibreOffice。

## 6. 跨平台 LibreOffice 发现

统一函数：

```python
def resolve_libreoffice_executable() -> Path | None:
    """跨平台查找 LibreOffice 命令行程序。"""
```

查找顺序：

1. `LIBREOFFICE_EXECUTABLE` 显式配置。
2. 当前 `PATH`。
3. 平台默认目录。

平台默认值：

```text
macOS:
  /Applications/LibreOffice.app/Contents/MacOS/soffice

Windows:
  %ProgramFiles%\LibreOffice\program\soffice.com
  %ProgramFiles%\LibreOffice\program\soffice.exe
  %ProgramFiles(x86)%\LibreOffice\program\soffice.com
  %ProgramFiles(x86)%\LibreOffice\program\soffice.exe

Linux:
  /usr/bin/soffice
  /usr/bin/libreoffice
  /opt/libreoffice/program/soffice
```

Windows 优先 `soffice.com`，以便可靠获得命令行退出码和标准输出。所有路径都通过参数列表传给 `subprocess`，不得手工添加引号。

LibreOffice profile URI 必须通过 `Path.as_uri()` 生成，兼容 Windows 盘符和空格路径。

## 7. 存储与并发

物理路径：

```text
storage/derivatives/office/{source_sha256[:2]}/{source_sha256}/{converter_config_hash}.docx
```

转换过程：

1. 创建请求级临时目录。
2. 复制源文件到临时目录并使用稳定文件名。
3. 为本次进程创建独立 LibreOffice profile。
4. 转换并校验输出。
5. 原子移动到最终路径。
6. 数据库登记派生件。

并发请求允许产生重复临时转换，但最终物理路径和数据库唯一约束必须收敛为同一个结果。失败或超时后清理临时目录，不留下半成品。

## 8. 解析链路接入

新增 `ReadableDocumentSource`：

```text
original_document_id
original_path
parse_path
original_filename
parse_filename
original_content_type
parse_content_type
artifact_id
converted
reused
converter_name
converter_version
```

接入边界：

- `_extract_document_text_handler()` 在调用解析器前解析可读源。
- `.doc` 使用派生 `.docx` 及 DOCX MIME 调用现有 `extract_document_text()`。
- `RenameParsingService` 消费同一个可读源，不再直接把 `.doc` 交给 Docling。
- 分类、摘要和问答继续读取 `document_pages`，无需直接感知派生件。
- 原件下载和确认后的真实改名继续操作 `.doc`。

解析页面元数据增加：

```json
{
  "source_format": "doc",
  "parsed_format": "docx",
  "conversion_artifact_id": "artifact-id",
  "converter": "libreoffice",
  "converter_version": "...",
  "conversion_reused": true
}
```

## 9. 配置与指纹

新增配置：

```dotenv
LEGACY_OFFICE_CONVERSION_ENABLED=true
LEGACY_OFFICE_CONVERTER=libreoffice
LIBREOFFICE_EXECUTABLE=
LEGACY_OFFICE_CONVERSION_TIMEOUT_SECONDS=90
LEGACY_OFFICE_MAX_FILE_SIZE_MB=100
LEGACY_OFFICE_DERIVATIVE_DIR=derivatives/office
```

转换配置指纹包含：

- 转换器名称和版本。
- 输出格式和过滤器。
- 转换参数规则版本。
- 源文件扩展名。

解析配置指纹还必须包含转换指纹，避免复用旧转换策略产生的解析结果。

## 10. 复用与失效

满足以下条件才能复用：

```text
source_sha256 相同
+ converter_config_hash 相同
+ 派生文件存在
+ 派生文件 SHA-256 一致
+ DOCX 结构校验通过
```

失效条件：

- 源文件 SHA-256 变化。
- 转换器主版本或转换规则版本变化。
- 派生文件丢失、损坏或哈希不符。
- 用户显式要求重新转换。

“重新解析”默认复用有效 DOCX，只重新生成解析运行；“重新转换”才绕过派生件缓存。

## 11. 失败回退

错误码：

```text
LIBREOFFICE_NOT_AVAILABLE
DOC_CONVERSION_DISABLED
DOC_CONVERSION_FILE_TOO_LARGE
DOC_CONVERSION_TIMEOUT
DOC_CONVERSION_FAILED
DOCX_OUTPUT_MISSING
DOCX_OUTPUT_INVALID
DERIVATIVE_WRITE_FAILED
```

转换失败时保留当前回退：

```text
macOS textutil 纯文本
-> 现有 LibreOffice 纯文本回退
-> 结构化失败结果
```

回退成功不伪装为 DOCX/Docling 解析，必须在 warnings 和解析器字段中说明来源。

## 12. 审计与清理

日志事件：

```text
file.derivative.convert.started
file.derivative.convert.completed
file.derivative.convert.reused
file.derivative.convert.failed
```

统一带：

```text
document_id / artifact_id / source_format / parsed_format /
converter / converter_version / status / duration_ms / error_code
```

ChangeSet 增加：

```text
DOCX_DERIVATIVE_CREATED
DOCX_DERIVATIVE_REUSED
```

删除 Document 时删除它的派生件记录；只有当没有其他记录引用同一 `storage_path` 时，才删除物理派生文件和空父目录。

## 13. 测试要求

### 13.1 单元测试

- 三个平台的 LibreOffice 路径发现顺序。
- Windows `soffice.com` 优先和 profile URI。
- 转换命令参数不经过 shell。
- 首次转换、同 Document 复用、跨 Document 同哈希物理复用。
- 超时、非零退出、空输出、伪 DOCX、损坏 DOCX。
- 原子写入和数据库唯一约束。
- 派生件失效后重新生成。
- 删除时引用计数保护。

### 13.2 链路测试

- 首次读取 `.doc` 创建派生件和 `document_pages`。
- 第二次读取不再调用 LibreOffice。
- Docling 收到 `.docx` 文件名和 MIME。
- Docling 失败后 python-docx 读取派生件。
- 重命名字段来自派生 DOCX，真实操作目标仍是原始 DOC。
- 分类、摘要和问答复用派生 DOCX 产生的页面正文。
- 转换不可用时现有纯文本回退不回归。

### 13.3 可选真实集成测试

本机或服务器存在 LibreOffice 时运行带标记的真实 `.doc -> .docx` smoke test；默认 pytest 不依赖本机安装 LibreOffice。

## 14. 实施阶段与状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| A | 基线测试，证明无持久派生件和跨请求复用 | 已完成 |
| B | `document_artifacts` 模型、迁移和仓储 | 已完成 |
| C | 跨平台 LibreOffice 转换服务 | 已完成 |
| D | 可读源解析器与解析/重命名链路接入 | 已完成 |
| E | 配置、日志、ChangeSet、删除清理 | 已完成 |
| F | 定向测试、完整回归和真实 smoke test说明 | 已完成 |

实施验证记录：

- 定向回归：209 项通过。
- 完整后端回归：407 项通过，1 项按既有条件跳过。
- Alembic 迁移链：`20260720_0001` 为唯一 head。
- 当前开发机未安装 LibreOffice，仓库内也没有真实 `.doc` 测试样本，因此可选真实转换 smoke test 未执行；模拟转换已覆盖合法 DOCX 输出、持久复用、跨 Document 物理复用、缺失输出、读取链路和重命名链路。
- 真实环境安装 LibreOffice 后，可用一份非敏感 `.doc` 依次执行“读取文件”“重新解析文件”“重新转换文件”，核对第一次创建派生件、第二次复用派生件、第三次强制重新转换。

## 15. 完成标准

- 第一次读取 `.doc` 后存在有效 `CONVERTED_DOCX` 派生件。
- 第二次读取以及后续重命名、分类、摘要不重复转换。
- Docling 能通过派生 `.docx` 参与 `.doc` 的结构化解析。
- 原始 `.doc` 未被覆盖，下载和真实重命名仍指向原件。
- Windows、macOS、Linux 的路径发现均有测试。
- LibreOffice 缺失或失败时现有读取能力仍可用。
- 数据库迁移、定向测试、完整后端测试和 `git diff --check` 全部通过。
