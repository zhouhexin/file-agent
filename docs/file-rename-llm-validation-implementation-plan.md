# 文件重命名差异风险与 LLM 证据校验实施计划

## 1. 目标

在现有“解析、字段仲裁、生成建议、OperationPlan、用户确认、执行”链路中增加命名质量校验层，降低标题取错页面、正文句子被当作标题、解析器冲突和 OCR 低质量导致的错误重命名风险。

LLM 只校验建议名称是否被现有候选和证据支持，不能直接访问文件系统、不能自由生成文件路径、不能替代用户最终确认。

目标链路：

```text
RenameParsingService
-> FilenameMetadataExtractor
-> RenameMetadataArbitrator
-> FilenameBuilder
-> RenameDifferenceAnalyzer
-> 中高风险调用 LLMRenameValidator
-> RenameValidationService 后端证据复核
-> READY / NEEDS_REVIEW
-> OperationPlan
-> 用户确认
-> Native / F2 执行器
```

## 2. 非目标

- 不让 LLM 自由创造正文中不存在的标题。
- 不让 LLM 生成或修改目录路径。
- 不因 LLM 校验通过而跳过 OperationPlan。
- 不在前端展示模型名称、风险分数或“模型校验通过”。
- 不使用 Neo4j 或 Embedding 参与第一版重命名质量校验。
- 不改变 Native/F2 执行器的文件系统安全边界。

## 3. 实施阶段与状态

| 阶段 | 内容 | 状态 | 验证结果 |
|---|---|---|---|
| 阶段 0 | 冻结方案、数据契约和测试基线 | 已完成 | 现有重命名测试：68 passed，1 skipped |
| 阶段 1 | 配置、校验 schema 和确定性差异分析器 | 已完成 | 配置与差异分析测试：19 passed |
| 阶段 2 | 受控 LLM 校验器和后端证据复核 | 已完成 | 校验器与降级测试累计：25 passed |
| 阶段 3 | 受管文件与上传附件链路接入、审计持久化 | 已完成 | 两条链路定向测试：9 passed |
| 阶段 4 | 前端待复核原因展示 | 已完成 | TypeScript 与 Vite 构建通过 |
| 阶段 5 | 定向、全量和前端回归自检 | 已完成 | 后端 390 passed、1 skipped；前端构建通过；git diff --check 通过 |

每完成一个阶段，必须立即更新本表状态和验证结果后再进入下一阶段。

实施结果：全部阶段已经完成。默认关闭质量门禁时只记录审计，不改变既有重命名建议；显式开启后，中高风险建议按本文约束执行模型校验与安全降级。

## 4. 阶段 1：确定性差异分析

新增：

```text
apps/api/app/modules/file_rename/difference_analyzer.py
apps/api/app/modules/file_rename/validation_schemas.py
```

`RenameDifferenceAnalyzer` 输入：

- 原文件名。
- 建议文件名。
- 最终年份、文号和标题字段。
- 解析模式和候选解析器。
- 字段仲裁警告。
- OCR/解析质量警告。

输出：

```json
{
  "risk_level": "HIGH",
  "risk_score": 0.82,
  "reason_codes": [
    "LOW_FILENAME_SIMILARITY",
    "PARSER_TITLE_CONFLICT"
  ],
  "hard_blockers": []
}
```

第一版风险信号：

- 原名称与建议标题的规范化字符相似度。
- 原名称是否为“附件、扫描件、数字编号”等低信息名称。
- 标题证据是否来自首页。
- 标题能否在证据 quote 中规范化定位。
- Docling 与 Native 是否存在标题冲突。
- OCR 或解析质量是否偏低。
- 年份、发文日期和文号年份是否冲突。
- 标题是否过长、截断或呈现正文句式。

默认等级：

```text
LOW: risk_score < 0.35
MEDIUM: 0.35 <= risk_score < 0.65
HIGH: risk_score >= 0.65
```

硬阻断原因：

```text
TITLE_NOT_IN_EVIDENCE
TITLE_FROM_LATER_PAGE
BODY_SENTENCE_AS_TITLE
DOCUMENT_NUMBER_CONFLICT
DOCUMENT_DATE_CONFLICT
TARGET_EXTENSION_CHANGED
```

低信息原名称不能只因字符串相似度低进入高风险；例如“附件1.pdf”正确改为正文标题时，应主要依据首页证据和解析器一致性判断。

## 5. 阶段 2：LLM 校验与证据复核

新增：

```text
apps/api/app/modules/file_rename/llm_validator.py
apps/api/app/modules/file_rename/validation_service.py
```

LLM 输入仅包含：

- 原文件名与建议文件名。
- 后端已经提取的年份、文号、标题候选。
- 与候选直接相关的首页证据 quote、页码、元素标签和解析器来源。
- 确定性风险原因。

文件文本必须作为不可信数据处理，Prompt 明确禁止执行文档内指令。不得发送无关全文、绝对路径、用户凭证和文件二进制。

LLM 输出固定 schema：

```json
{
  "verdict": "PASS",
  "title_supported": true,
  "year_supported": true,
  "document_number_supported": true,
  "selected_title_candidate_id": "title-candidate-1",
  "reason_codes": [],
  "explanation": "建议标题与首页标题证据一致"
}
```

约束：

1. `verdict` 只能是 `PASS`、`NEEDS_REVIEW`、`REJECT`。
2. LLM 只能选择后端提供的候选 ID。
3. 未知候选 ID、非法 JSON、空响应或超时均视为校验不可用。
4. LLM `PASS` 后必须再次执行后端硬校验。
5. 硬阻断原因不能被 LLM 覆盖。

最终状态：

```text
LOW 风险且无硬阻断 -> READY
MEDIUM/HIGH + LLM PASS + 后端复核通过 -> READY
LLM NEEDS_REVIEW / REJECT / 不可用 -> NEEDS_REVIEW
任何硬阻断 -> NEEDS_REVIEW
```

后端内部可以记录 `READY_LLM_VALIDATED` 判定来源，但对现有 API 和前端统一暴露为 `READY`。

## 6. 配置与降级

新增配置：

```env
FILE_RENAME_LLM_VALIDATION_ENABLED=false
FILE_RENAME_LLM_VALIDATION_MODE=risk_based
FILE_RENAME_LLM_VALIDATION_THRESHOLD=0.60
FILE_RENAME_LLM_VALIDATION_TIMEOUT_SECONDS=30
FILE_RENAME_LLM_VALIDATION_MAX_ITEMS_PER_BATCH=20
FILE_RENAME_LLM_VALIDATION_PROMPT_VERSION=rename-validation-v1
```

配置规则：

- `FILE_RENAME_LLM_VALIDATION_ENABLED=false` 或 `off`：只运行并保存确定性风险审计，不改变现有建议状态，便于安全上线和离线评估。
- `risk_based`：只对达到阈值且无硬阻断的文件调用 LLM。
- `all`：对所有可构造名称且无硬阻断的文件调用 LLM，主要用于对比测试。
- 校验开关已开启但 `LLM_ENABLED=false` 时，中高风险项进入 `NEEDS_REVIEW`，低风险项继续生成计划。
- LLM 超时、网络错误和输出错误不能导致整个批次失败。
- 单批超过同步调用上限时，超出项进入 `NEEDS_REVIEW` 并记录 `LLM_VALIDATION_LIMIT_REACHED`；异步批量校验作为后续优化，不在本阶段扩张范围。

## 7. 阶段 3：业务链路与持久化

接入：

- `RenameSuggestionService`
- `UploadedRenameSuggestionService`
- 请求级 Tool Registry / Runtime Context 工厂

校验位置固定为 `FilenameBuilder` 生成建议后、重命名批次和 OperationPlan 固化前。

第一版不新增数据库迁移，复用 `file_rename_batch_items.metadata_json.rename_validation`：

```json
{
  "risk_level": "HIGH",
  "risk_score": 0.82,
  "reason_codes": ["LOW_FILENAME_SIMILARITY"],
  "hard_blockers": [],
  "validation_mode": "risk_based",
  "llm_verdict": "PASS",
  "validator_model": "configured-chat-model",
  "prompt_version": "rename-validation-v1",
  "validated_at": "2026-07-20T10:00:00+08:00"
}
```

OperationPlan 只包含最终可执行项。待复核项仍保存在批次和 review item 中，不阻止其他文件进入计划。

日志事件：

```text
file_rename.validation.started
file_rename.validation.completed
file_rename.validation.degraded
```

日志只记录业务 ID、状态、原因代码、模型标识和耗时，不记录正文、完整 Prompt、API key 或绝对路径。

## 8. 阶段 4：前端展示

前端不显示 `READY_LLM_VALIDATED`、“模型校验通过”、模型名称或风险分数。

当待复核原因包含以下任一代码时：

```text
RENAME_DIFFERENCE_UNVERIFIED
TITLE_NOT_IN_EVIDENCE
TITLE_FROM_LATER_PAGE
PARSER_TITLE_CONFLICT
OCR_QUALITY_LOW
DOCUMENT_NUMBER_CONFLICT
DOCUMENT_DATE_CONFLICT
LLM_VALIDATION_UNAVAILABLE
LLM_VALIDATION_LIMIT_REACHED
```

`RenameSuggestionReceipt` 显示：

```text
名称差异较大，系统未能确认标题依据。
```

其他缺失年份、标题或解析失败继续使用现有通用提示。前端不得直接展示英文错误码。

## 9. 测试要求

### 后端单元测试

1. “附件1.pdf”改为完整首页标题，不因低相似度单独判高风险。
2. 普通有意义原文件名与完全无关标题产生高风险。
3. 第三页标题触发硬阻断。
4. 标题无法在证据中定位触发硬阻断。
5. Docling 与 Native 标题冲突进入中高风险。
6. 年份、文号和日期冲突不能被 LLM 放行。
7. LLM 选择未知候选 ID 被拒绝。
8. LLM 非 JSON、超时和连接失败安全降级。
9. 文档证据中的 Prompt Injection 不改变输出契约。

### 后端集成测试

1. 受管文件和上传附件使用同一校验服务。
2. 低风险文件继续生成 OperationPlan。
3. 中高风险未通过时进入 `NEEDS_REVIEW`。
4. 批量单项失败不影响其他项。
5. `metadata_json.rename_validation` 保存审计数据。
6. OperationPlan 确认时继续校验路径、SHA-256、扩展名和冲突。

### 前端测试

1. READY 项不显示模型校验状态。
2. 指定待复核原因显示“名称差异较大，系统未能确认标题依据”。
3. 普通缺失字段继续显示现有提示。
4. 多文件选择、分页和确认按钮数量保持正确。
5. TypeScript 构建通过。

### 最终自检

```text
重命名定向 pytest
后端全量 pytest
前端 npm run build
git diff --check
```

## 10. 验收标准

- 字符串差异大不能成为唯一拒绝原因。
- 后续页面标题、无证据标题和冲突字段不能进入可执行计划。
- LLM 只能验证候选，不能创造标题或路径。
- LLM 不可用时不返回 500、不放行中高风险项、不影响低风险项。
- 前端只展示用户需要处理的待复核提示，不展示内部模型状态。
- 所有真实重命名仍经过 OperationPlan 和用户确认。
- 单文件校验失败不影响批次中其他文件。
