# Docling 与原生解析混合重命名实施计划

## 1. 目标

在保持 `DOCLING_ENABLED=true` 的前提下，将文件重命名从“Docling 整体优先”改为“Docling 与原生解析器生成候选，按标题、日期、年份和文号逐字段仲裁”。

本次改造只调整重命名元数据提取链路，不改变分类、正文总结、OCR 和通用文件解析的既有行为。

## 2. 配置契约

新增配置：

```env
# 重命名解析模式：
# hybrid：Docling 与原生解析字段级仲裁，默认生产模式。
# native：只使用原生解析器，Docling 质量异常时可紧急回退。
# docling：只使用 Docling，主要用于对比测试和问题定位。
FILE_RENAME_PARSE_MODE=hybrid
```

约束：

- `DOCLING_ENABLED=false` 时，即使配置为 `hybrid` 或 `docling`，重命名也必须安全使用原生解析，不得导致任务失败。
- 未知配置值必须在配置层回退为 `hybrid`，不能传入业务层。
- `FILE_RENAME_EXECUTOR` 继续控制 Native/F2 文件执行器，与解析模式互不影响。

## 3. 目标链路

```text
受管文件或上传文件
-> RenameParsingService
   -> Docling 结构化结果（按配置启用）
   -> 原生解析结果（按配置启用）
-> FilenameMetadataExtractor 分别提取字段候选
-> RenameMetadataArbitrator 逐字段仲裁
-> FilenameBuilder 生成文件名
-> READY / AMBIGUOUS / NEEDS_REVIEW
-> OperationPlan 与字段证据
```

## 4. 实施阶段与状态

| 阶段 | 内容 | 状态 | 验证结果 |
|---|---|---|---|
| 阶段 0 | 建立计划、配置契约和测试基线 | 已完成 | `test_config.py`：12 passed；三种合法值及未知值回退已覆盖 |
| 阶段 1 | 实现重命名专用原生解析入口与多解析器候选服务 | 已完成 | `test_file_rename_parsing_service.py` + `test_file_extraction_tools.py`：21 passed |
| 阶段 2 | 实现字段候选评分和逐字段仲裁 | 已完成 | 重命名字段、解析器与抽取回归：55 passed；模块编译通过 |
| 阶段 3 | 接入受管文件、上传文件和审计输出 | 已完成 | 重命名、OperationPlan 与执行器链路：56 passed，1 skipped |
| 阶段 4 | 完成回归测试、相关全量测试和部署文档 | 已完成 | 后端全量：358 passed，1 skipped；前端构建和 `git diff --check` 通过 |

每完成一个阶段，必须立即更新本表状态和验证结果后再进入下一阶段。

## 5. 阶段 0：计划与配置契约

### 修改范围

- `apps/api/app/core/config.py`
- `.env.example`
- `deploy/.env.production.example`
- `docs/runbook.md`

### 验收条件

- `Settings` 暴露经过白名单校验的 `file_rename_parse_mode`。
- 示例配置中包含三种模式的中文注释。
- 配置测试覆盖默认值、三个合法值和未知值回退。

## 6. 阶段 1：多解析器候选

### 修改范围

- `apps/api/app/modules/files/extractors.py`
- 新增 `apps/api/app/modules/file_rename/parsing_service.py`

### 实现要求

1. 新增明确绕过 Docling 的原生解析入口，避免递归调用 Docling。
2. `hybrid` 模式同时提供 Docling 和原生候选。
3. `native` 模式只提供原生候选。
4. `docling` 模式优先只提供 Docling；Docling 不可用时安全回退原生解析并记录警告。
5. 解析器候选只在重命名服务中使用，不写入 `AgentGraphState`。
6. 单个解析器失败不影响另一个解析器，也不影响批次中的其他文件。

### 验收条件

- 三种模式的候选集合符合配置语义。
- Docling 异常时可回退原生解析。
- XLS/XLSX 等非 Docling 格式继续使用原生解析。

## 7. 阶段 2：字段级仲裁

### 修改范围

- `apps/api/app/modules/file_rename/metadata_extractor.py`
- 新增 `apps/api/app/modules/file_rename/metadata_arbitrator.py`
- `apps/api/app/modules/file_rename/schemas.py`

### 标题规则

- 首页 `title` 是 Docling 高质量候选。
- `section_header` 是次级候选，不能仅凭标签覆盖可靠原生标题。
- 允许合并最多 5 个连续、同页、同层级标题元素。
- 排除页眉、页脚、目录、正文小节和表格内容。
- 标题候选必须保留页码、元素标签、位置和解析器来源。

### 文号规则

- 只接受首页、独立成行、位于标题附近并完整匹配格式的文号。
- `reference`、正文引用、脚注和页眉页脚中的文号不得作为本文件文号。
- 多个高质量文号冲突时返回 `AMBIGUOUS`。

### 日期和年份规则

```text
落款区独立日期
-> 本文件文号年份
-> 文件名完整日期
-> 首页标题区域年份
```

- 落款日期必须位于文档尾部正文区，并排除表格、附件和正文引用。
- 日期与文号年份冲突时，日期决定命名年份，同时保留冲突警告。

### 仲裁规则

- Docling 与原生字段一致：合并证据并提高置信度。
- 一方缺失：使用另一方可靠结果。
- 两个高置信度候选冲突：字段标记 `AMBIGUOUS`，文件进入待复核。
- 不再给所有 Docling 字段固定 `0.99` 置信度。

### 验收条件

- 仲裁以字段为单位，不以整个解析器为单位。
- 每个最终字段都能说明来源、分数和选择原因。
- 高置信度冲突不得自动生成可执行重命名项。

## 8. 阶段 3：业务链路接入

### 修改范围

- `apps/api/app/modules/file_rename/suggestion_service.py`
- `apps/api/app/modules/file_rename/uploaded_suggestion_service.py`
- OperationPlan 序列化和前端兼容字段（如有必要）

### 实现要求

- 受管文件和上传文件统一使用 `RenameParsingService -> Extractor -> Arbitrator`。
- OperationPlan 保存最终字段、证据来源和仲裁警告。
- 旧 OperationPlan 保持快照语义，不重新计算也不改变。
- `NEEDS_REVIEW` 项继续从执行批次中跳过。

### 验收条件

- 两条重命名入口对同一文件生成一致结果。
- 批量任务中单文件解析或仲裁失败不会阻断其他文件。
- 前端现有重命名计划和待复核卡片不需要解析新的自由文本。

## 9. 阶段 4：回归与文档

### 必测场景

1. Docling 错把章节标题识别为标题，原生解析识别主标题。
2. Docling 将完整标题拆成 2 至 5 个元素。
3. 正文引用文号不被识别为本文件文号。
4. 正文引用 2023 年、落款日期 2024 年时使用 2024 年。
5. Docling 与原生高置信度标题冲突时进入待复核。
6. Docling 失败时回退原生解析。
7. DOCX、PDF、XLS、XLSX 批量失败隔离。
8. 上传文件与受管文件结果一致。
9. 同标题文件继续按照完整日期和版本后缀消解冲突。

### 最终验收

- 开启 Docling 后，现有重命名回归样本质量不低于原生解析基线。
- 标题不因 Docling 分块而丢失后半部分。
- 正文引用不得覆盖真实发文日期和文号。
- 配置可在不改代码的情况下切换三种解析模式。
- 所有相关 pytest 测试通过，`git diff --check` 无错误。

## 10. 最终实施结果

- 已新增 `FILE_RENAME_PARSE_MODE` 白名单配置，默认 `hybrid`。
- 已在本地和生产示例配置中写明 `hybrid`、`native`、`docling` 的用途和回退边界。
- 通用文件解析仍保持原有 Docling 优先行为；重命名链路新增明确绕过 Docling 的原生解析入口。
- `native` 模式的主解析入口使用独立配置指纹，不加载 Docling，也不复用 Docling 解析快照。
- 已实现 `RenameParsingService`，按配置收集 Docling 和原生候选。
- 已实现 `RenameMetadataArbitrator`，按日期、年份、文号和标题逐字段仲裁。
- Docling 标题最多支持合并 5 个连续结构元素；`section_header` 不再无条件覆盖原生主标题。
- 已限制文号候选的首页标题区域，并排除正文引用和页眉页脚。
- 已排除表格、引用和脚注中的落款日期候选。
- 受管文件和上传附件均已接入统一仲裁服务。
- OperationPlan 已保存解析模式、候选解析器、字段证据和结构化仲裁警告。
- 高质量字段冲突会进入 `AMBIGUOUS / NEEDS_REVIEW`，不会自动进入执行批次。

最终验证：

```text
pytest -q: 358 passed, 1 skipped
npm run build: passed
git diff --check: passed
```
