# 图片与扫描件动态结构化抽取 Autonomous Loop 实施方案

- 文档状态：待实施
- 编写日期：2026-08-24
- 适用范围：图片、扫描 PDF 及由受控文件解析链生成的页面图片
- 上位规范：`agent.md`
- 关联方案：`docs/adaptive-planner-execution-loop-implementation-plan.md`
- 关联能力：`skills/file-ingest/SKILL.md`

## 1. 背景

File Agent 已具备图片和扫描 PDF 的基础 OCR 能力，也已具备 Catalog 驱动的 Adaptive Planner、
LangGraph 规划执行循环、Tool Registry、ToolInvocation、ChangeSet、异步文件任务和普通用户回执。
现有 OCR 链路主要把识别结果保存为页面文本和文字块，还不能稳定满足以下实际需求：

```text
从图片中提取申请人、金额、日期并以表格展示。
读取扫描登记表中的全部列，返回 JSON。
找出每一行的项目名称、负责人、联系电话和审批结果，导出 Excel。
提取图片中的所有字段；看不清的内容不要猜，单独标记。
```

实际业务字段不是固定的“申请人、资助金额、申请日期”。字段名称、字段类型、表单结构和展示格式都可能
随用户请求变化。因此本能力必须支持用户动态字段 Schema，不能为每一种学校业务表单硬编码独立接口。

本方案使用 PP-StructureV3 Python SDK 完成通用版面解析、表格结构恢复和 OCR，不使用 MCP 调用
PP-StructureV3。LLM 通过现有 Adaptive Planner 驱动 Autonomous Loop，但 LLM 不直接访问 Python SDK、
文件系统或数据库；所有执行仍集中经过白名单 Tool。

## 2. 目标

实现如下闭环：

```text
用户消息 + 已授权附件
-> Adaptive Planner LLM 理解目标字段、记录结构和输出格式
-> 生成符合 PlannerDecision 的声明式 ToolPlan
-> 后端校验文件范围、Skill、Tool 和动态字段 Schema
-> Tool 创建结构化抽取运行与异步任务
-> Worker 使用 PP-StructureV3 Python SDK 解析图片版面
-> 结构化抽取 LLM/VLM 按动态 Schema 映射字段
-> 后端确定性归一化、证据校验和质量判定
-> 持久化结果、字段证据、ToolInvocation 和 ChangeSet
-> 生成不含正文的 ExecutionObservation
-> Adaptive Planner 在预算内选择 FINISH、局部增强或 CLARIFY
-> 前端按用户要求展示 TABLE、JSON、CSV、XLSX 或 TEXT
```

完成后应满足：

1. 用户可以在自然语言中自由指定需要提取的字段。
2. 用户没有指定字段时，可以在受控边界内自动发现本次文档 Schema。
3. 后端统一保存结构化 JSON，前端展示格式遵循用户明确要求。
4. 每个值保留原始识别文本、归一化值、置信度和图片位置证据。
5. 模糊、冲突和缺失字段进入 `NEEDS_REVIEW`，不得编造。
6. Autonomous Loop 最多执行一次初始抽取和一次有针对性的局部增强。
7. 原始图片和扫描件始终不变。

## 3. 非目标

本阶段不实现：

- 让 LLM 直接导入或调用 `paddlex`。
- 让 Planner 生成 Python、Shell、SQL、正则表达式或任意 Prompt。
- 把 PP-StructureV3 暴露为公网服务或 MCP Server。
- 仅凭目录名、文件名或 OCR 摘要编造结构化业务事实。
- 自动把本次发现的字段发布为全局正式模板。
- 自动修改、覆盖或删除原始图片。
- 无限重试 OCR、无限放大图片或无限调用多模态模型。
- 在当前阶段训练或微调 PP-StructureV3 模型。

## 4. 核心设计原则

### 4.1 Planner LLM 与抽取 LLM 分离

| 角色 | 输入 | 职责 | 禁止事项 |
|---|---|---|---|
| Adaptive Planner LLM | 用户消息、附件摘要、Catalog、脱敏 Observation | 选择 Skill/Tool，声明字段 Schema、记录模式和展示格式，决定结束、局部增强或澄清 | 不能读取图片正文、调用 SDK、生成事实值或路径 |
| Structured Extraction LLM/VLM | 固定系统 Prompt、已校验字段 Schema、受控图片或裁剪、PP-Structure 版面结果 | 将表格和文字块映射为 Schema 规定的字段 | 不能选择 Tool、改变文件范围、生成新字段、执行副作用 |
| 后端确定性代码 | Tool 输入、Provider 输出、Persistent Stores | 权限校验、类型归一化、金额计算、日期解析、证据核验和质量判定 | 不能用猜测替代不可读事实 |

两个 LLM 可以使用同一个 OpenAI-compatible 网关，但必须使用不同 Provider 配置、固定 Prompt、调用日志和
业务契约。`LLM_ENABLED=true` 不得隐式开启图片外发。

### 4.2 Python SDK 是运行时 Provider

PP-StructureV3 只存在于运行时 Provider：

```text
StructuredExtractionWorker
-> PpStructureV3Provider
-> paddlex.create_pipeline(...)
-> pipeline.predict(...)
```

Pipeline、SDK 结果对象、模型实例和临时图片不得写入 `AgentGraphState`、checkpoint 或
`graph_state_json`。它们属于 `AgentRuntimeContext` 或 worker 进程内运行依赖。

### 4.3 内部 JSON 与用户展示分离

无论用户要求表格、JSON、CSV、XLSX 还是普通文本，后端都先生成并保存同一份规范化 JSON。展示格式只由
`presentation` 决定，不能改变事实值和证据。

### 4.4 原始值与归一化值并存

每个字段至少保存：

```text
raw_text
normalized_value_json
confidence
status
page_number
bbox_json
evidence_element_ids_json
```

例如手写金额 `10,000-` 可以归一化为十进制定点值，但必须保留 `raw_text`。无法确认的 `25xxx` 不能被
静默归一化为 `25000`。

## 5. 总体架构

```text
POST conversation message
        |
        v
LangGraph planning
        |
        v
AdaptivePlannerService
  PlannerDecision / ToolPlan
        |
        v
Tool Registry schema + scope validation
        |
        v
extract-image-structured-data
  create StructuredExtractionRun
  enqueue STRUCTURED_IMAGE_EXTRACTION
        |
        v
AgentRun: WAITING_FOR_ASYNC_JOB
        |
        v
StructuredExtractionWorker
  1. resolve authorized immutable source
  2. PpStructureV3Provider.predict
  3. persist/reuse pages and elements
  4. StructuredExtractionLlmProvider.extract
  5. normalize and validate evidence
  6. persist result + ChangeSet
        |
        v
resume original AgentRun
        |
        v
safe ExecutionObservation
        |
        v
Adaptive Planner
  FINISH / TOOL_PLAN(VISION_CROP) / CLARIFY
        |
        v
UserTaskReceipt + requested presentation
```

## 6. Skill 设计

新增：

```text
skills/image-structured-extraction/
├── SKILL.md
└── manifest.json
```

建议 `manifest.json`：

```json
{
  "id": "image-structured-extraction",
  "version": "1.0.0",
  "status": "ACTIVE",
  "description": "从图片和扫描件中按用户动态字段抽取结构化数据",
  "trigger_hints": [
    "识别图片中的字段",
    "从扫描件提取",
    "整理成表格",
    "返回 JSON",
    "提取全部信息"
  ],
  "allowed_tools": [
    "extract-image-structured-data"
  ],
  "required_capabilities": [
    "image-layout-parsing",
    "dynamic-schema-extraction"
  ],
  "risk_ceiling": "medium"
}
```

Skill 规则必须明确：

- 用户列出字段时使用 `EXPLICIT_FIELDS`，不得自行增加字段。
- 用户要求“全部信息”时使用 `AUTO_DISCOVER`。
- 用户明确要求输出格式时必须遵循。
- 自动发现的 Schema 只属于本次抽取运行。
- 低置信度结果必须保留待复核状态。
- 原始文件不得被覆盖。

## 7. Tool 设计

### 7.1 Tool 名称

```text
extract-image-structured-data
```

职责：对一个已授权图片或扫描文档创建或复用结构化抽取运行，必要时提交异步任务。

副作用：

- 创建结构化抽取运行和字段结果。
- 创建或复用页面、文档元素和派生裁剪图。
- 创建 ToolInvocation、ChangeSet 和 ChangeItem。
- 不修改原件。

确认要求：

- 本地 PP-StructureV3 和已明确授权的本地模型不需要确认。
- 如果图片将发送到新的外部服务，必须满足显式部署授权；没有部署级授权时创建外发 OperationPlan，不能执行。

### 7.2 动态字段输入契约

建议在 `apps/api/app/modules/agent/tool_schemas.py` 中新增：

```python
class StructuredFieldSpec(BaseModel):
    """动态抽取字段的受控声明，不接受任意代码或 Prompt。"""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=80)
    field_type: Literal[
        "string",
        "integer",
        "decimal",
        "money",
        "date",
        "datetime",
        "person_name",
        "phone",
        "id_number",
        "organization",
        "boolean",
        "enum",
    ]
    required: bool = False
    multiple: bool = False
    aliases: list[str] = Field(default_factory=list, max_length=10)
    enum_values: list[str] = Field(default_factory=list, max_length=30)


class StructuredImageExtractionInput(StrictToolInput):
    """图片结构化抽取白名单 Tool 的严格输入。"""

    document_id: str = Field(min_length=1, max_length=36)
    schema_mode: Literal["EXPLICIT_FIELDS", "AUTO_DISCOVER"]
    record_mode: Literal[
        "AUTO",
        "SINGLE_RECORD",
        "TABLE_ROWS",
        "KEY_VALUE_GROUPS",
    ] = "AUTO"
    fields: list[StructuredFieldSpec] = Field(default_factory=list, max_length=40)
    presentation: Literal[
        "AUTO",
        "TABLE",
        "JSON",
        "CSV",
        "XLSX",
        "TEXT",
    ] = "AUTO"
    retry_strategy: Literal["INITIAL", "REOCR", "VISION_CROP"] = "INITIAL"
    target_field_keys: list[str] = Field(default_factory=list, max_length=20)
```

模型校验规则：

1. `EXPLICIT_FIELDS` 必须至少有一个字段。
2. `AUTO_DISCOVER` 的 `fields` 必须为空。
3. `enum` 必须提供非空且不重复的 `enum_values`。
4. 非 `enum` 字段不得携带 `enum_values`。
5. `VISION_CROP` 必须提供非空 `target_field_keys`。
6. `target_field_keys` 必须属于第一次运行已经验证的低置信度字段集合。
7. 字段 `key`、别名、标签和枚举值必须去重并限制长度。
8. 输入中不得出现路径、模型名、API key、Prompt、正则或脚本。

### 7.3 Tool 输出契约

```python
class StructuredExtractionToolOutput(GenericToolOutput):
    """图片结构化抽取结果的严格业务输出。"""

    kind: Literal["structured_image_extraction"] = "structured_image_extraction"
    document_id: str
    structured_extraction_run_id: str
    schema_mode: Literal["EXPLICIT_FIELDS", "AUTO_DISCOVER"]
    record_mode: str
    presentation: str
    record_count: int = Field(ge=0)
    field_count: int = Field(ge=0)
    review_count: int = Field(ge=0)
    missing_required_field_count: int = Field(ge=0)
    quality_band: Literal["HIGH", "MEDIUM", "LOW"]
    retryable: bool = False
    recommended_retry_strategy: Literal["NONE", "REOCR", "VISION_CROP"] = "NONE"
    low_confidence_field_keys: list[str] = Field(default_factory=list, max_length=20)
    records: list[dict[str, Any]] = Field(default_factory=list)
    review_items: list[dict[str, Any]] = Field(default_factory=list)
    original_unchanged: bool = True
```

Tool 返回给 Planner 的 Observation 不得包含 `records` 和 `review_items` 中的具体值。

## 8. PP-StructureV3 Python Provider

新增模块：

```text
apps/api/app/modules/structured_extraction/pp_structure_provider.py
```

建议接口：

```python
class LayoutParsingProviderProtocol(Protocol):
    """图片版面解析 Provider 的最小受控接口。"""

    name: str
    version: str

    def parse(self, *, file_path: Path, page_number: int | None = None) -> LayoutParseResult:
        """解析一个已授权文件并返回普通可序列化结构。"""
```

Python SDK 调用骨架：

```python
from functools import lru_cache

from paddlex import create_pipeline


@lru_cache(maxsize=1)
def _load_pipeline(*, device: str, pipeline_config: str):
    """每个 worker 进程只加载一次重量级模型。"""

    return create_pipeline(
        pipeline=pipeline_config or "PP-StructureV3",
        device=device,
    )


def parse(self, *, file_path: Path, page_number: int | None = None) -> LayoutParseResult:
    """调用本地 PP-StructureV3，并把 SDK 对象立即投影为稳定结构。"""

    pipeline = _load_pipeline(
        device=self.settings.pp_structure_device,
        pipeline_config=self.settings.pp_structure_pipeline_config,
    )
    outputs = pipeline.predict(
        input=str(file_path),
        use_doc_orientation_classify=True,
        use_doc_unwarping=True,
        use_textline_orientation=True,
    )
    return self._normalize_outputs(outputs, page_number=page_number)
```

实现规则：

- `create_pipeline()` 必须延迟执行，服务启动不能因模型缺失而整体失败。
- 每个 worker 进程缓存一个 Pipeline；不得为每张图片重复加载模型。
- 默认使用单个专用 worker，避免 CPU 和内存争抢。
- 模型源默认使用受控 BOS 配置，部署阶段预下载模型。
- Provider 输出必须立刻转换为普通 Pydantic/JSON 数据。
- 日志只记录 Provider、版本、耗时、页面数、元素数和错误码。
- 不记录图片 Base64、OCR 全文、绝对路径或完整 SDK 结果。
- SDK 初始化失败、模型缺失和输入不支持必须返回结构化错误。

### 8.1 稳定版面结构

建议内部定义：

```text
LayoutParseResult
- provider
- provider_version
- pages[]
  - page_number
  - width
  - height
  - rotation
  - elements[]
    - element_index
    - element_type
    - text
    - confidence
    - bbox
    - reading_order
    - parent_ref
    - table_id
    - row_start
    - row_end
    - column_start
    - column_end
- warnings[]
```

PP-Structure 的字段不得直接泄漏到其他业务模块。Provider Adapter 负责版本兼容，业务服务只依赖稳定结构。

## 9. 页面与文档元素复用

PP-StructureV3 结果继续写入项目既有 Persistent Stores：

- 页面聚合文本写入 `document_pages`。
- 标题、正文、表格和单元格写入 `document_elements`。
- 坐标写入 `document_elements.bbox_json`。
- 表格 ID、行列范围、置信度和阅读顺序写入 `metadata_json`。

解析复用键至少包含：

```text
document_version_id
input_sha256
provider_name
provider_version
pipeline_config_fingerprint
```

同一内容版本和相同配置已有成功结果时必须复用，不重新调用 PP-StructureV3。图片旋转、去畸变、模型版本或
Pipeline 配置发生变化时，必须创建新解析运行，不能覆盖旧结果。

## 10. 结构化抽取 LLM/VLM

新增：

```text
apps/api/app/modules/structured_extraction/llm_provider.py
```

输入只能包含：

```json
{
  "field_schema": [],
  "record_mode": "TABLE_ROWS",
  "layout": {
    "page_number": 1,
    "tables": [],
    "cells": [],
    "text_blocks": []
  },
  "output_schema": {}
}
```

需要视觉补充时，由 Provider 受控附加整页图片或低置信度区域裁剪。Planner 不能指定任意本地路径、裁剪坐标
或自定义 Prompt。

固定 System Prompt 至少要求：

1. 只从给定图片、OCR 和表格结构中提取。
2. 只能返回已校验字段 Schema 中的 key。
3. `raw_text` 必须来自可定位图片内容或 OCR 候选。
4. 看不清时返回 `null` 和低置信度，不得补全常识值。
5. 不计算合计，不推测缺失日期，不纠正姓名。
6. 返回严格 JSON，不返回 Markdown 和解释文字。

输出示意：

```json
{
  "records": [
    {
      "record_index": 1,
      "fields": {
        "applicant": {
          "raw_text": "金润逸",
          "value": "金润逸",
          "confidence": 0.92,
          "evidence_element_ids": ["element-id"]
        },
        "amount": {
          "raw_text": "10000",
          "value": "10000",
          "confidence": 0.96,
          "evidence_element_ids": ["element-id"]
        }
      }
    }
  ],
  "warnings": []
}
```

后端必须拒绝：

- Schema 外字段。
- 重复记录编号。
- 不存在的 Evidence Element ID。
- 非法置信度。
- `raw_text` 与证据元素完全不相关但声称高置信度的值。
- VLM 自报的页码、文件名或绝对路径。

## 11. 确定性归一化和证据校验

新增：

```text
apps/api/app/modules/structured_extraction/normalization.py
apps/api/app/modules/structured_extraction/evidence.py
```

### 11.1 类型归一化

| 类型 | 归一化规则 |
|---|---|
| `string` | 去除首尾空白，保留原始内部字符 |
| `integer` | 仅接受可确定解析的整数 |
| `decimal` | 使用 `Decimal`，禁止二进制浮点累计 |
| `money` | 使用 `Decimal`，分离币种和原始金额文本 |
| `date` | 使用确定性日期解析；歧义日期不猜年份 |
| `datetime` | 保存时区或明确标记无时区 |
| `phone` | 只做字符归一化，不自动补区号 |
| `id_number` | 做长度和校验位检查，回执默认脱敏 |
| `boolean` | 只接受后端白名单映射 |
| `enum` | 必须属于已校验枚举值或进入待复核 |

### 11.2 证据规则

每个非空字段必须至少关联一个真实 `document_element`，并满足：

- element 属于本次 `document_id` 和解析运行。
- `page_number` 来自解析事实。
- bbox 来自 PP-StructureV3，不由 LLM 猜测。
- 证据文本能够支持 `raw_text` 或明确标记为视觉复核来源。
- 合并单元格可以关联多个 Element，但必须记录行列范围。

无法定位证据的字段必须降级为 `NEEDS_REVIEW`。

### 11.3 质量判定

建议初始阈值：

```text
HIGH：review_count = 0，所有 required 字段存在，平均置信度 >= 0.85
MEDIUM：存在可局部增强字段，或平均置信度在 [0.65, 0.85)
LOW：required 字段缺失、版面失败、平均置信度 < 0.65 或证据冲突
```

阈值必须配置化并通过真实学校材料评测后调整。

## 12. 数据模型

### 12.1 `structured_extraction_runs`

```text
id uuid primary key
document_id uuid not null
document_version_id uuid not null
layout_extraction_run_id uuid not null
agent_run_id uuid nullable
schema_mode varchar(40) not null
field_schema_json jsonb not null
schema_fingerprint varchar(64) not null
record_mode varchar(40) not null
presentation varchar(40) not null
provider varchar(80) not null
model_name varchar(160) not null
prompt_version varchar(80) not null
retry_strategy varchar(40) not null
parent_run_id uuid nullable
status varchar(40) not null
record_count integer not null default 0
review_count integer not null default 0
quality_score double precision nullable
error_code varchar(120) nullable
error_message text nullable
created_at timestamptz not null
updated_at timestamptz not null
```

缓存查找键：

```text
document_version_id
+ layout_extraction_run_id
+ schema_fingerprint
+ provider
+ model_name
+ prompt_version
+ retry_strategy
```

该组合只用于查找可复用的成功运行，不设置数据库唯一约束。失败运行必须允许用户使用相同参数重新
提交；并发 AgentRun 也必须分别拥有可恢复的异步任务和 `agent_run_id`，不能因为缓存唯一约束共享
另一个用户创建的 PENDING 任务。只有状态为 `COMPLETED`、`PARTIAL` 或 `NEEDS_REVIEW` 的运行可
作为只读缓存复用。

### 12.2 `structured_extraction_fields`

```text
id uuid primary key
structured_extraction_run_id uuid not null
record_index integer not null
field_key varchar(64) not null
field_label varchar(80) not null
field_type varchar(40) not null
raw_text text nullable
normalized_value_json jsonb not null
confidence double precision nullable
status varchar(40) not null
page_number integer nullable
bbox_json jsonb not null
evidence_element_ids_json jsonb not null
warning_codes_json jsonb not null
created_at timestamptz not null
```

字段状态：

```text
EXTRACTED
NORMALIZED
NEEDS_REVIEW
MISSING
CONFLICTED
REJECTED
```

结构化字段结果属于长期事实候选，但没有经过用户确认时不得自动变成正式业务数据库记录。

## 13. Autonomous Loop

### 13.1 第一轮：Planner 声明目标

用户请求：

```text
识别图片中的项目名称、负责人、金额和联系电话，以表格展示。
```

PlannerDecision 示例：

```json
{
  "decision_type": "TOOL_PLAN",
  "intent": "EXTRACT_STRUCTURED_IMAGE_DATA",
  "user_goal": "提取图片中的项目名称、负责人、金额和联系电话并展示为表格",
  "selected_skill_ids": ["image-structured-extraction"],
  "scope": {
    "document_ids": ["document-uuid"],
    "source": "current_attachments",
    "requires_backend_resolution": false
  },
  "tool_plan": {
    "plan_id": "structured-extraction-1",
    "steps": [
      {
        "step_id": "extract-1",
        "skill_id": "image-structured-extraction",
        "tool_name": "extract-image-structured-data",
        "literal_input": {
          "document_id": "document-uuid",
          "schema_mode": "EXPLICIT_FIELDS",
          "record_mode": "AUTO",
          "fields": [
            {"key": "project_name", "label": "项目名称", "field_type": "string"},
            {"key": "principal", "label": "负责人", "field_type": "person_name"},
            {"key": "amount", "label": "金额", "field_type": "money"},
            {"key": "phone", "label": "联系电话", "field_type": "phone"}
          ],
          "presentation": "TABLE",
          "retry_strategy": "INITIAL",
          "target_field_keys": []
        }
      }
    ]
  },
  "confidence": 0.91
}
```

Registry 仍是最终事实来源，必须重新验证：

- `document_id` 属于本次附件或后端授权上下文。
- Skill 存在且允许调用该 Tool。
- Tool 已启用且 `adaptive_ready=true`。
- 输入满足严格 Pydantic Schema。
- Planner 没有降低风险和确认要求。

### 13.2 Tool 提交异步任务

Tool 创建：

```text
structured_extraction_run: PENDING
filesystem_job:
  job_type = STRUCTURED_IMAGE_EXTRACTION
  queue_name = STRUCTURED_EXTRACTION
  deduplication_key = structured-extraction:<run_id>
```

Tool 返回：

```json
{
  "kind": "filesystem_job",
  "ok": true,
  "status": "WAITING_FOR_ASYNC_JOB",
  "async_job_id": "job-uuid",
  "structured_extraction_run_id": "run-uuid"
}
```

AgentRun 进入 `WAITING_FOR_ASYNC_JOB`，同步 HTTP 请求不阻塞模型推理。

### 13.3 第二轮：安全 Observation

Worker 完成后构造：

```json
{
  "tool_name": "extract-image-structured-data",
  "observation_kind": "structured_image_extraction",
  "status": "PARTIAL",
  "ok": true,
  "document_ids": ["document-uuid"],
  "result_count": 6,
  "record_count": 6,
  "field_count": 4,
  "review_count": 2,
  "missing_required_field_count": 0,
  "quality_band": "MEDIUM",
  "retryable": true,
  "recommended_retry_strategy": "VISION_CROP",
  "low_confidence_field_keys": ["amount", "phone"],
  "available_next_decisions": ["TOOL_PLAN", "CLARIFY", "FINISH"]
}
```

Observation 不包含姓名、金额、电话、OCR 全文、图片 Base64、文件路径、bbox 或抽取结果 JSON。

### 13.4 第三轮：局部增强

Planner 只能使用后端 Observation 允许的字段：

```json
{
  "document_id": "document-uuid",
  "schema_mode": "EXPLICIT_FIELDS",
  "record_mode": "TABLE_ROWS",
  "fields": [
    {"key": "amount", "label": "金额", "field_type": "money"},
    {"key": "phone", "label": "联系电话", "field_type": "phone"}
  ],
  "presentation": "TABLE",
  "retry_strategy": "VISION_CROP",
  "target_field_keys": ["amount", "phone"]
}
```

局部增强规则：

- 只处理第一次运行标记的低置信度字段。
- 裁剪 bbox 来自 Persistent Stores，不接受 Planner 坐标。
- 每个字段保留适当上下文边距，不能扩张为任意整盘目录读取。
- 第二次运行通过 `parent_run_id` 关联第一次运行。
- 合并结果只替换置信度更高且证据有效的字段。
- 无论成功与否，第三轮后必须结束或要求用户确认，不能继续自动重试。

### 13.5 循环预算

沿用现有 Agent Runtime 全局预算：

```text
MAX_PLANNING_ROUNDS = 3
MAX_TOOL_CALLS = 5
```

本 Skill 额外限制：

```text
initial_extraction_calls <= 1
enhancement_calls <= 1
structured_extraction_calls <= 2
```

相同 `document_id + schema_fingerprint + retry_strategy + target_field_keys` 的重复 Tool 调用必须被现有
`DUPLICATE_TOOL_CALL` 机制拒绝。

## 14. Planner Prompt 扩展

`ADAPTIVE_PLANNER_SYSTEM_PROMPT` 增加：

```text
图片或扫描件结构化抽取规则：
1. 用户明确列出字段时选择 image-structured-extraction 和 EXPLICIT_FIELDS，只使用用户要求的字段。
2. 用户要求提取全部字段但没有列出字段时使用 AUTO_DISCOVER，不得在 Planner 中预先编造字段。
3. presentation 必须遵循用户明确要求；没有明确要求时多行记录优先 TABLE，单记录优先 JSON/TEXT。
4. 首次调用使用 INITIAL。只有观察明确返回 retryable=true 和推荐策略时，才允许一次增强调用。
5. VISION_CROP 的 target_field_keys 只能来自 observation.low_confidence_field_keys。
6. quality_band=HIGH 且 review_count=0 时选择 FINISH。
7. LOW、必要字段缺失或版面不可恢复时选择 CLARIFY 或 FINISH，并保留待复核项。
8. 不得重复相同输入，不得自行提供图片路径、bbox、模型名、Prompt 或执行参数。
```

## 15. ExecutionObservation 扩展

不使用自由 `metadata: dict`。在 `tool_contracts.py` 中添加严格字段：

```python
class StructuredExtractionObservation(BaseModel):
    """供 Planner 重规划的脱敏结构化抽取观察。"""

    model_config = ConfigDict(extra="forbid")

    record_count: int = Field(ge=0)
    field_count: int = Field(ge=0)
    review_count: int = Field(ge=0)
    missing_required_field_count: int = Field(ge=0)
    quality_band: Literal["HIGH", "MEDIUM", "LOW"]
    retryable: bool
    recommended_retry_strategy: Literal["NONE", "REOCR", "VISION_CROP"]
    low_confidence_field_keys: list[str] = Field(default_factory=list, max_length=20)
```

可以将该模型作为 `ExecutionObservation.structured_extraction` 的可选字段；其他 Tool 保持 `None`。

后端重新计算 `available_next_decisions`：

- `HIGH`：`FINISH`。
- `MEDIUM + retryable`：`TOOL_PLAN | CLARIFY | FINISH`。
- `LOW + user_action_required`：`CLARIFY | FINISH`。
- 外发等待确认：`FINISH`。
- 异步任务仍在运行：`FINISH`，不能重复调用。

## 16. 异步 Worker 与 AgentRun 恢复

新增：

```text
apps/api/app/modules/structured_extraction/worker.py
```

或在现有统一 filesystem worker 中注册 `STRUCTURED_IMAGE_EXTRACTION` handler，但推理队列必须独立为：

```text
STRUCTURED_EXTRACTION
```

Worker 步骤：

1. 获取并锁定 PENDING job。
2. 读取 `structured_extraction_run`。
3. 通过 StorageService 解析已授权不可变内容版本。
4. 校验 MIME、文件大小、图片像素、PDF 页数和内容 hash。
5. 复用或调用 PP-StructureV3。
6. 写入或复用 `document_pages`、`document_elements`。
7. 调用 Structured Extraction LLM/VLM。
8. 执行字段归一化、证据校验和质量判定。
9. 写字段结果、ChangeSet 和 ChangeItem。
10. 更新 ToolInvocation 和 job 状态。
11. 恢复原 AgentRun，让 Adaptive Planner 消费安全 Observation。

恢复边界：

- 使用数据库行锁串行恢复同一 AgentRun。
- AgentRun 必须仍为 `WAITING_FOR_ASYNC_JOB`。
- 复用原 `agent_run_id`、消息、规划轮数和 Tool 调用预算。
- 不创建重复的用户消息或助手消息。
- 不创建第二条相同 ToolInvocation；更新原调用的终态。
- 失败任务不得被用户重复查询隐式无限重开。
- 有副作用执行失败后只允许 `CLARIFY` 或 `FINISH`。
- worker 重启后可以依据 job 与 run 的持久化状态恢复。

当前受管文件 worker 中检索续跑是专用实现。本能力落地时应抽取通用
`WaitingAgentRunResumeService`，由检索和结构化抽取分别提供结果恢复 Adapter，避免继续复制整段续跑代码。

## 17. ChangeSet 与审计

一次成功初始抽取至少创建：

```text
ChangeSet.operation_type = STRUCTURED_IMAGE_EXTRACTION
```

ChangeItem 建议新增：

```text
IMAGE_LAYOUT_PARSED
TABLE_STRUCTURE_EXTRACTED
STRUCTURED_FIELDS_EXTRACTED
STRUCTURED_FIELD_NEEDS_REVIEW
STRUCTURED_EXTRACTION_REUSED
STRUCTURED_EXTRACTION_FAILED
```

每个 ChangeItem 记录：

- 目标 `document_id`。
- 解析/抽取运行 ID。
- Provider、模型和版本。
- 字段数量、记录数量、待复核数量。
- Schema fingerprint。
- 证据元素 ID 摘要。
- 原始文件未发生变化。

不得把完整图片、OCR 全文、字段原始值或模型 Prompt 写入普通 JSONL 日志。

## 18. 统一结果 JSON

内部结果示例：

```json
{
  "schema_key": null,
  "schema_mode": "EXPLICIT_FIELDS",
  "document_id": "document-uuid",
  "document_type": "学科经费资助使用登记表",
  "record_mode": "TABLE_ROWS",
  "field_schema": [
    {"key": "applicant", "label": "申请人", "field_type": "person_name"},
    {"key": "amount", "label": "资助金额", "field_type": "money"},
    {"key": "application_date", "label": "申请日期", "field_type": "date"}
  ],
  "records": [
    {
      "record_index": 1,
      "fields": {
        "applicant": {
          "raw_text": "金润逸",
          "normalized_value": "金润逸",
          "confidence": 0.92,
          "status": "NORMALIZED"
        },
        "amount": {
          "raw_text": "10000",
          "normalized_value": {
            "amount": "10000.00",
            "currency": "CNY"
          },
          "confidence": 0.96,
          "status": "NORMALIZED"
        },
        "application_date": {
          "raw_text": "2026.6.5",
          "normalized_value": "2026-06-05",
          "confidence": 0.94,
          "status": "NORMALIZED"
        }
      },
      "evidence": {
        "page_number": 1,
        "element_ids": ["element-id"],
        "bbox": {"left": 100, "top": 200, "right": 900, "bottom": 260}
      }
    }
  ],
  "review_items": [
    {
      "record_index": 4,
      "field_key": "amount",
      "raw_text": "25xxx",
      "reason": "手写内容模糊，无法确定完整金额",
      "status": "NEEDS_REVIEW"
    }
  ],
  "quality_band": "MEDIUM",
  "original_unchanged": true
}
```

## 19. 用户回执与前端

### 19.1 UserTaskReceipt

新增字段：

```json
{
  "structured_extraction": {
    "presentation": {
      "type": "TABLE",
      "columns": [
        {"key": "applicant", "label": "申请人", "field_type": "person_name"},
        {"key": "amount", "label": "资助金额", "field_type": "money"},
        {"key": "application_date", "label": "申请日期", "field_type": "date"}
      ]
    },
    "records": [],
    "review_items": [],
    "quality_band": "MEDIUM",
    "original_unchanged": true,
    "export_artifact": null
  }
}
```

### 19.2 前端组件

新增：

```text
apps/web/src/features/chat/StructuredExtractionReceipt.tsx
```

展示规则：

- `TABLE`：使用动态列头渲染，低置信度单元格显示警告。
- `JSON`：显示规范化 JSON，不显示内部数据库字段。
- `CSV`：由后端生成受控派生件并提供下载。
- `XLSX`：由后端生成 Excel Artifact，并同时显示表格预览。
- `TEXT`：按记录和字段分组展示。
- 点击字段可以查看页码和原图证据区域。
- 身份证号、手机号等敏感字段默认脱敏，按权限决定是否允许展开。
- 必须显示待复核数量和“原始文件未修改”。

HTTP API 始终返回 JSON envelope；`presentation` 表示前端展示方式，不改变 API Content-Type。

### 19.3 目标满足与回执真实性保护

- 用户明确要求“图片/扫描件字段抽取 + 表格/JSON/CSV/XLSX/结构化展示”时，只有
  `extract-image-structured-data` 可以满足目标；`read-document-insights`、普通 OCR 和
  `hybrid-search` 不能被视为等价降级。
- 专用 Tool 未启用时必须关闭式说明本次没有执行结构化抽取，不能声称已经识别、整理或生成表格。
- 专用 Tool 已启用但 Planner 生成了其他 Tool 计划时，后端目标守卫不能执行替代 Tool。若当前消息
  已明确列出字段或明确要求自动发现全部字段，并且附件已经由后端解析为确定的 `document_id`，守卫
  应只把这些用户明示参数规范化为 `extract-image-structured-data` 计划；无法可靠解析字段或附件范围时
  仍关闭式结束。该规范化步骤不读取图片、不生成字段值，字段值继续由 PP-StructureV3、结构化抽取
  LLM Provider 和有预算的 Autonomous Loop 生成。
- 通用 LLM 回执只能提示用户查看确定性明细，不能自行声明“已识别、已提取、已整理、已生成”等
  执行事实；真正的字段、记录数、复核数和导出件只能来自专用 ToolInvocation。
- “没有看到你展示的表格/结果”属于上一轮输出反馈，不得因包含“展示 + 表格”扩大为共享工作区
  文件检索。

## 20. 配置

建议新增：

```env
PP_STRUCTURE_ENABLED=false
PP_STRUCTURE_DEVICE=cpu
PP_STRUCTURE_PIPELINE_CONFIG=PP-StructureV3
PP_STRUCTURE_MODEL_SOURCE=BOS
PP_STRUCTURE_MAX_IMAGE_PIXELS=24000000
PP_STRUCTURE_MAX_PDF_PAGES=50

STRUCTURED_EXTRACTION_ENABLED=false
STRUCTURED_EXTRACTION_LLM_PROVIDER=disabled
STRUCTURED_EXTRACTION_LLM_BASE_URL=
STRUCTURED_EXTRACTION_LLM_API_KEY=
STRUCTURED_EXTRACTION_LLM_MODEL=
STRUCTURED_EXTRACTION_LLM_TIMEOUT_SECONDS=120
STRUCTURED_EXTRACTION_MAX_FIELDS=40
STRUCTURED_EXTRACTION_MAX_RETRY_FIELDS=20
STRUCTURED_EXTRACTION_MAX_RECORDS=1000
STRUCTURED_EXTRACTION_PROMPT_VERSION=v1
STRUCTURED_EXTRACTION_HIGH_CONFIDENCE=0.85
STRUCTURED_EXTRACTION_RETRY_CONFIDENCE=0.65
```

结构化识别并发由部署时启动的 `STRUCTURED_EXTRACTION` Worker 进程数量控制，不提供一个无法改变
实际进程数的伪并发环境变量。强制推理超时必须由可终止的子进程或容器监管实现；在完成该隔离前，
不声明只能停止等待、却无法停止底层 PP-StructureV3 推理的线程级超时开关。

配置规则：

- `LLM_ENABLED=true` 不隐式开启 `STRUCTURED_EXTRACTION_LLM_PROVIDER`。
- 只有显式设置 `STRUCTURED_EXTRACTION_LLM_PROVIDER=openai_compatible` 后，专用 Provider 才允许调用
  外部模型。此时 `STRUCTURED_EXTRACTION_LLM_BASE_URL`、`STRUCTURED_EXTRACTION_LLM_API_KEY` 和
  `STRUCTURED_EXTRACTION_LLM_MODEL` 中未单独配置的项复用对应的全局 `LLM_*` 网关参数；专用参数优先。
- 当前全局 Adaptive Planner 可以保持 `shadow`。明确的图片结构化请求先由后端把用户明示字段与授权
  附件规范化为严格初始 Tool 计划，后台字段映射与可选一次局部增强仍由 LLM Autonomous Loop 完成，
  不要求为了单项能力把全局 Planner 切换为 `enabled`。
- 真实 `.env` 不提交；`.env.example` 只放空密钥。
- Provider 设置变化必须改变缓存 fingerprint。
- 配置或依赖缺失时能力清单显示不可用，不能注册空成功 Tool。

## 21. 安全边界

1. 图片文件先经过现有上传、隔离、MIME 和原件保护链路。
2. Tool 只接受 `document_id`，不接受路径和 URL。
3. StorageService 解析真实文件位置，避免路径穿越。
4. LLM 不直接读取文件系统、数据库或 PP-Structure SDK。
5. 图片、OCR 全文、裁剪和 SDK 对象不进入 AgentGraphState。
6. Planner Observation 只包含计数、质量等级和字段 key。
7. 外部多模态 Provider 默认关闭；外发必须有明确部署授权或 OperationPlan。
8. Prompt 注入文本只作为图片数据，不能成为系统指令。
9. 金额合计、日期和枚举校验由确定性代码完成。
10. 低置信度、证据冲突和空结果不得伪装为成功事实。
11. 批量文件逐文件隔离失败，单个文件失败不得回滚其他文件。
12. 结果导出属于派生件；大批量导出仍需遵守 OperationPlan 规则。

## 22. 失败与降级

| 场景 | 状态 | 降级策略 |
|---|---|---|
| PaddleX 未安装 | `FAILED` | 返回 `PP_STRUCTURE_NOT_AVAILABLE`，不伪造 OCR 结果 |
| 模型加载失败 | `FAILED` | 记录脱敏错误，允许 ops 重处理 |
| 图片过大 | `NEEDS_REVIEW` | 请求用户提供更清晰或分辨率合适的图片 |
| 版面解析为空 | `PARTIAL` | 可在配置允许时回退现有 PaddleOCR 文本块 |
| 表格结构失败但文字可读 | `PARTIAL` | 使用 KEY_VALUE/TEXT_BLOCK 抽取并降低置信度 |
| LLM/VLM 不可用 | `PARTIAL` | 保留 PP-Structure 页面和元素，说明尚未完成动态字段映射 |
| LLM JSON 不合法 | `FAILED` | 不保存字段事实，不自动无限重试 |
| 必填字段缺失 | `NEEDS_REVIEW` | Planner 可澄清或结束，不猜测 |
| 少量字段低置信度 | `PARTIAL` | 最多一次 `VISION_CROP` |
| 证据元素不存在 | `NEEDS_REVIEW` | 拒绝高置信度字段并记录冲突 |
| 外部 Provider 未授权 | `WAITING_FOR_CONFIRMATION` | 创建外发 OperationPlan 或使用本地能力 |
| 部署未启用结构化抽取 | `UNAVAILABLE` | 明确说明未执行，不回退为基础洞察或全局检索 |
| Planner 选择了非专用 Tool | `FAILED` | 后端目标守卫关闭式拒绝，不生成虚假完成回执 |

## 23. 代码目录规划

```text
apps/api/app/modules/structured_extraction/
├── __init__.py
├── schemas.py
├── repository.py
├── service.py
├── pp_structure_provider.py
├── llm_provider.py
├── normalization.py
├── evidence.py
├── worker.py
├── resume_service.py
└── receipt.py

apps/api/app/tests/
├── test_pp_structure_provider.py
├── test_structured_extraction_service.py
├── test_structured_extraction_tool.py
├── test_structured_extraction_autonomous_loop.py
├── test_structured_extraction_worker.py
├── test_structured_extraction_receipt.py
└── test_structured_extraction_security.py

apps/web/src/features/chat/
└── StructuredExtractionReceipt.tsx

skills/image-structured-extraction/
├── SKILL.md
└── manifest.json
```

同时修改：

```text
apps/api/app/core/config.py
apps/api/app/db/models.py
apps/api/app/modules/agent/adaptive_planner.py
apps/api/app/modules/agent/tool_schemas.py
apps/api/app/modules/agent/tool_contracts.py
apps/api/app/modules/agent/tool_registry.py
apps/api/app/modules/agent/graph.py
apps/api/app/modules/agent/runtime.py
apps/api/app/modules/agent/user_receipt.py
apps/web/src/types.ts
.env.example
README.md
docs/runbook.md
```

## 24. 实施顺序

### 阶段 1：严格契约和数据模型

1. 新增动态字段 Pydantic 输入模型。
2. 新增严格 Tool 输出和安全 Observation 模型。
3. 新增数据库迁移和 ORM。
4. 新增 Skill 文档和 manifest，但 Tool 未实现前保持 `DISABLED`。

验收：非法字段、Schema 外字段、路径、Prompt 和过大输入全部在调用前被拒绝。

### 阶段 2：PP-StructureV3 Python Provider

1. 实现延迟加载和进程级缓存。
2. 实现 SDK 输出到稳定 LayoutParseResult 的转换。
3. 实现页面、元素、表格单元格和 bbox 持久化。
4. 实现配置 fingerprint 和成功结果复用。

验收：固定 fake SDK 输入可以确定性生成页面和单元格；原件 hash 不变。

### 阶段 3：动态结构化抽取

1. 实现固定 Prompt 的 LLM/VLM Provider。
2. 实现 EXPLICIT_FIELDS 和 AUTO_DISCOVER。
3. 实现类型归一化、证据校验和质量等级。
4. 实现一次局部视觉增强及父子运行合并。

验收：模糊字段进入待复核，金额和日期由后端归一化，Schema 外字段被拒绝。

### 阶段 4：Tool、worker 和恢复

1. 注册真实 `extract-image-structured-data` handler。
2. 创建专用异步队列和 worker handler。
3. 抽取通用等待 AgentRun 恢复服务。
4. Worker 完成后恢复原 AgentRun。

验收：API 请求不阻塞；进程重启后任务可恢复；不会产生重复 ToolInvocation 或消息。

### 阶段 5：Adaptive Autonomous Loop

1. 扩展 Planner Prompt 和 Catalog。
2. 扩展安全 Observation 投影。
3. 后端强制一次初始抽取和一次增强的额外预算。
4. 覆盖 FINISH、VISION_CROP、CLARIFY 和失败闭环。

验收：Planner 只能使用 Observation 提供的低置信度字段，不能自造路径、bbox 或重试参数。

### 阶段 6：回执与前端

1. 聚合结构化抽取结果到 `result_summary`。
2. 扩展 UserTaskReceipt。
3. 新增动态表格、JSON、CSV、XLSX 和文本展示。
4. 增加证据区域预览和低置信度提示。

验收：用户明确要求的展示格式得到遵守；刷新会话后结果不丢失。

### 阶段 7：真实材料评测与灰度

1. 建立脱敏学校表单评测集。
2. 对印刷体、手写体、倾斜、阴影、模糊、合并单元格和多页扫描件分别评测。
3. 标定置信度阈值和局部增强收益。
4. 先 Shadow，再按用户稳定分桶灰度启用 Autonomous Loop。

验收：达到第 27 节指标后才能默认启用。

## 25. 测试方案

### 25.1 单元测试

- 动态字段 Schema 正常和非法矩阵。
- AUTO_DISCOVER 不接受 Planner 预填字段。
- `enum`、日期、金额、电话和身份证归一化。
- PP-Structure SDK 对象转换。
- Pipeline 只加载一次。
- Evidence Element ID 权限和归属校验。
- 低置信度质量等级。
- 局部增强只替换更高质量字段。
- Provider 异常脱敏。

### 25.2 Autonomous Loop 测试

所有 Planner 和抽取 LLM 使用 deterministic fake：

1. `INITIAL -> HIGH -> FINISH`。
2. `INITIAL -> MEDIUM -> VISION_CROP -> FINISH`。
3. `INITIAL -> LOW -> CLARIFY`。
4. Planner 试图加入用户未要求字段时被拒绝。
5. Planner 试图传路径或 Prompt 时被拒绝。
6. Planner 试图对非低置信度字段重试时被拒绝。
7. 重复 Tool 输入触发 `DUPLICATE_TOOL_CALL`。
8. 超出两次结构化抽取调用时关闭式结束。
9. 异步任务未完成时 Planner 不重复创建任务。
10. 写入型失败后 Planner 只能 FINISH 或 CLARIFY。
11. 明确结构化抽取请求不能被 `read-document-insights` 或 `hybrid-search` 满足。
12. “没有看到表格”反馈不能触发共享工作区文件检索。
13. 通用回执模型声称“已识别/已整理”时必须被过滤，保留确定性回执。

### 25.3 API 和前端测试

- 用户要求 TABLE、JSON、CSV、XLSX、TEXT。
- 多附件逐文件结果和部分失败。
- 会话刷新恢复结构化结果。
- 低置信度单元格提示。
- 原始文件未修改提示。
- 普通 user 不看到内部 Tool、模型、路径和 Prompt。
- ops/admin 可以查看抽取运行和脱敏失败诊断。

### 25.4 安全测试

- 图片中的 Prompt 注入文本不能改变 ToolPlan。
- 超大图片、伪造 MIME、损坏图片和加密 PDF。
- 路径穿越和跨用户 document_id。
- 外部 Provider 未授权时不外发。
- 日志不包含 OCR 全文、字段值、API key 和 Base64。
- 身份证号、电话等敏感字段默认脱敏。

## 26. 手工烟测

```text
1. 以普通 user 登录 /chat。
2. 上传清晰的印刷体表格，并明确要求提取若干动态字段，以表格展示。
3. 确认 AgentRun 创建并进入后台处理状态。
4. 确认结果列名与用户要求一致，没有额外字段。
5. 上传包含手写内容的同类表格。
6. 确认模糊字段被标记，不出现猜测值。
7. 确认 Autonomous Loop 最多执行一次局部增强。
8. 要求同一图片返回 JSON，确认事实值与表格结果一致。
9. 要求导出 XLSX，确认生成派生件且原图不变。
10. 上传倾斜、阴影、模糊和多页扫描 PDF，检查逐页证据。
11. 使用图片内 Prompt 注入文本，确认系统忽略该文本指令。
12. 关闭结构化抽取 LLM，确认 PP-Structure 结果保留且系统明确降级。
13. 以 ops/admin 查看 ToolInvocation、ChangeSet、运行状态和错误码。
```

## 27. 验收标准

功能验收：

- 支持用户动态指定 1 至 40 个字段。
- 支持 `EXPLICIT_FIELDS` 和 `AUTO_DISCOVER`。
- 支持单记录、表格行、键值组和自动记录模式。
- 支持 TABLE、JSON、CSV、XLSX 和 TEXT 展示。
- 每个非空字段具有页码和 bbox/Element 证据，或明确进入待复核。
- Autonomous Loop 能完成结束、一次增强和澄清三类路径。

安全验收：

- LLM 不直接调用 PP-StructureV3、文件系统或数据库。
- SDK 对象、图片和正文不进入 AgentGraphState。
- 相同输入不会重复执行。
- 外部图片发送默认关闭。
- 原始文件 hash 在处理前后保持一致。

质量验收建议：

```text
印刷体字段精确匹配率 >= 95%
清晰规则表格行列恢复率 >= 95%
清晰手写字段精确匹配率 >= 85%
无法确认字段的错误强填率 <= 1%
有效证据定位覆盖率 >= 98%
局部增强后的低置信度字段改善率有正收益
```

性能验收需要按实际部署 CPU 和材料规模标定，不在代码中写死不现实的统一秒数。必须记录 P50、P95、
模型加载时间、单页推理耗时、峰值内存和队列等待时间。

## 28. 上线策略

配置阶段：

```text
STRUCTURED_EXTRACTION_ENABLED=false
ADAPTIVE_PLANNER_MODE=shadow
```

灰度顺序：

1. 本地离线测试，Tool 不进入 Planner Catalog。
2. ops/admin 内测，Tool 可直接调用但 Adaptive 仍为 Shadow。
3. 5% 用户稳定分桶启用 Autonomous Loop。
4. 观察错误强填率、待复核率、重试率、任务失败率和内存。
5. 逐步提升至 25%、50%、100%。

任一阶段出现以下问题必须关闭该能力：

- 原件被修改。
- 图片未经授权外发。
- Planner 绕过动态 Schema 或重试预算。
- OCR/VLM 结果无证据却被标记为高置信度。
- Worker 内存或队列导致其他文件任务明显退化。

## 29. 最终决策

本项目采用以下实现边界：

```text
PP-StructureV3：Python SDK，本地专用 worker 内直接调用
Autonomous Loop：复用现有 LangGraph Adaptive Planner 循环
LLM 权限：只生成 PlannerDecision 或按固定 Schema 抽取，不直接执行
字段：用户动态 Schema，非固定业务字段
事实存储：PostgreSQL + document_pages/document_elements + structured_extraction 表
展示：统一 JSON 事实，前端按用户要求渲染
低置信度：最多一次局部增强，之后待复核或澄清
MCP：当前不使用，仅在未来多个外部 Agent 共享该能力时考虑 Adapter
```

该方案在保持 Tool 白名单、原件保护、Evidence、ChangeSet、AgentRun 和审计边界的同时，使 LLM 可以在
有限预算内自主选择字段抽取、观察结果和局部增强，从而满足不同图片、不同扫描件和不同用户字段需求。
