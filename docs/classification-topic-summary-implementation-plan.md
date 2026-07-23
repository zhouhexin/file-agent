# 分类主题摘要优先的文件分类实施方案

状态：执行中（CPU-only 抽取式默认 Provider 已完成）
日期：2026-07-21
适用范围：File Agent 工作副本文档解析后的持久化摘要、多标签分类、文档级检索和证据问答链路

## 1. 方案目标

当前分类链路直接使用文件名和完整正文召回 taxonomy 候选。对于干部考察报告、个人履历、会议材料、附件汇编等文档，正文可能包含大量并非文件主旨的“科研”“教学”“学科”“项目”“成果”等词，导致这些偶发内容被错误提升为主要分类。

本方案引入持久化“普通文档摘要”和“分类主题摘要”。普通文档摘要负责文档概览、文档级召回和问答路由；分类主题摘要负责taxonomy候选召回和偶发主题抑制。分类链路调整为：

```text
document_pages 完整正文
-> 生成并保存普通文档摘要
-> 生成分类主题摘要
-> 根据分类主题摘要召回 taxonomy 候选
-> 在固定候选集合内进行语义判定和重排
-> 回到 document_pages 校验原文证据
-> 保存分类建议、分类运行记录和 ChangeSet
```

方案目标：

1. 从完整正文中识别文件的主要目的、文种、业务事项和处理对象。
2. 区分主要主题、次要主题和偶发主题，避免把个人履历、附件清单和引用材料中的词当成文件主旨。
3. 分类目录继续以 taxonomy v2 配置为唯一事实源，摘要模型不得创造正式分类。
4. 分类建议继续提供可定位到页码、工作表和原文片段的证据。
5. 摘要、分类和证据校验全过程可缓存、可追踪、可降级和可评测。
6. 工作副本导入后异步生成摘要，批量历史文件允许低优先级处理，首次内容相关命中时提升同一分析任务优先级。
7. 后续检索和问答优先使用摘要定位文件和缩小原文范围，但最终结论必须回到`document_pages`、原文Chunk或确定性表格工具取证。

## 2. 可行性结论

该方案技术上可行，并且与当前项目架构兼容。

当前项目已经具备以下基础能力：

- `DocumentClassificationService`可以按`extraction_run_id`读取`document_pages`完整正文。
- `LLMDocumentSummaryService`已经实现小文件单次总结和大文件分块后汇总。
- `LLMClassificationJudge`已经限制模型只能从候选分类中选择，并校验引用必须来自原文。
- LangGraph、AgentRuntimeContext、Tool白名单、ChangeSet和分类建议持久化边界已经存在。
- 本地Sentence Transformers向量模型可以用于分类主题摘要与taxonomy描述的语义相似度计算。
- 后台普通摘要和分类主题摘要已经默认使用带固定候选上限的 `Jieba + LexRank`，并与用户主动聊天总结
  的 LLM Provider 分离。

但不能直接采用以下简单链路：

```text
完整正文 -> 一段普通自然语言摘要 -> 关键词分类
```

原因是普通摘要可能遗漏人名、日期、金额、条款和附件细节，也可能使用原文中不存在的同义表达或产生幻觉。摘要只能用于概览复用、文档级召回、问答路由和分类候选召回，不能替代完整正文，也不能直接成为最终证据。

因此，本方案确认采用“摘要优先”，明确拒绝“摘要唯一”：

```text
用户问题
-> 普通文档摘要召回候选文件
-> 分类主题摘要补充文种、主旨和偶发主题判断
-> 在候选文件内检索原文Chunk、页面或Sheet
-> 数字和表格问题交给确定性Tool
-> 基于原文证据生成带引用回答
```

## 3. 统一术语

本方案统一使用以下名词，后续代码、数据库、日志、测试和页面不得混用其他近义命名。

| 中文名词 | 代码命名 | 含义 |
|---|---|---|
| 普通文档摘要 | `DocumentSummary` | 面向用户概览、文档级检索和问答路由的持久化摘要，不是最终问答证据 |
| 普通文档摘要服务 | `DocumentSummaryService` | 从完整正文生成或复用普通文档摘要的运行时服务；现有`LLMDocumentSummaryService`作为过渡实现 |
| 分类主题摘要 | `ClassificationTopicSummary` | 从完整正文提炼出的结构化分类输入，不是面向用户的普通摘要 |
| 分类主题摘要服务 | `DocumentClassificationSummaryService` | 读取完整正文并生成分类主题摘要的运行时服务 |
| 分类主题摘要记录 | `DocumentClassificationSummary` | 分类主题摘要的持久化数据库记录 |
| 分类候选 | `CategoryCandidate` | 从固定taxonomy召回的待判定分类，不等于正式分类 |
| 分类建议 | `DocumentCategorySuggestion` | AgentRun生成的`SUGGESTED`或`NEEDS_REVIEW`结果 |
| 偶发主题 | `incidental_topics` | 仅在履历、附件、示例或引用内容中出现，不代表文件主要目的的主题 |
| 原文证据 | `evidence_items` | 能定位到原始页面、工作表和原文片段的分类依据 |
| 文档分析任务 | `ANALYZE_DOCUMENT_VERSION` | 工作副本版本解析完成后异步生成两类摘要、Chunk、Embedding和分类建议的持久化任务 |
| 摘要优先问答 | `SUMMARY_FIRST` | 先用摘要召回和路由，再回到原文取证的问答模式；不得实现为`SUMMARY_ONLY` |

“分类主题摘要”不得简称为“分类结果”。“普通文档摘要”指面向用户总结、讲解、文档级检索和问答路由的自然语言内容。两者可以由同一个文档分析任务生成并共享分块中间结果，但必须分别持久化、分别版本化，不能混为同一个业务对象。

## 4. 目标架构

### 4.1 总体流程

```text
工作副本导入完成或首次内容相关命中
-> 创建或提升ANALYZE_DOCUMENT_VERSION任务
-> 受控解析服务生成或复用document_pages
-> DocumentSummaryService生成或复用普通文档摘要
-> DocumentClassificationService读取完整正文
-> DocumentClassificationSummaryService生成分类主题摘要
-> 关键词规则召回taxonomy候选
-> 摘要向量与taxonomy描述向量执行语义召回
-> 合并、去重并重排候选
-> LLMClassificationJudge在候选集合内选择0到3个分类
-> EvidenceValidator回到document_pages校验quote和定位信息
-> 保存DocumentClassificationSummary
-> 保存DocumentClassificationRun和DocumentCategorySuggestion
-> ChangeSet写入CATEGORY_SUGGESTED或失败/复用记录
-> Agent输出逐文件回执
```

普通文档摘要完成后还必须建立文档级摘要向量；原文继续建立Chunk、全文索引和Chunk向量。摘要向量负责召回候选文件，原文索引负责候选文件内的精确取证，不能只建立摘要向量而省略原文索引。

### 4.2 数据边界

分类主题摘要链路必须继续遵守三层数据边界：

- `AgentGraphState`只保存`document_summary_id`、`classification_summary_id`、状态、警告和必要的轻量回执字段，不保存完整正文、分块正文、模型客户端或数据库会话。
- `AgentRuntimeContext`保存`DocumentSummaryService`、`DocumentClassificationSummaryService`、`DocumentClassificationService`、LLM client、Embedding Provider等运行时服务。
- `Persistent Stores`保存`document_pages`、普通文档摘要记录、分类主题摘要记录、原文Chunk、分类运行、分类建议、原文证据和ChangeSet。

普通文档摘要服务和分类主题摘要服务都不得由Graph节点直接查询`DocumentPage`。Graph只能传递`document_id`、`document_version_id`、`extraction_run_id`和受控参数，由请求级运行时服务读取正文。

### 4.3 Tool边界

第一阶段不新增直接面向Planner的Tool名称。分类主题摘要作为`multi-label-classify`或当前等价分类Tool内部的受控步骤执行，继续由同一ToolInvocation覆盖整个分类事务。

这样可以避免Planner绕过证据校验单独生成摘要，也避免摘要模型输出直接触发数据库写入。后续只有在分类主题摘要需要独立重处理、独立运营查看或独立队列调度时，才评估新增`classification-topic-summarize` Tool。

普通文档摘要同样不新增允许Planner任意传入正文的Tool。工作副本导入后由生命周期服务创建`ANALYZE_DOCUMENT_VERSION`任务；用户明确要求总结、分类或问答且摘要缺失时，现有受控Tool只能创建或提升该任务，不能把正文直接写入任务参数。

### 4.4 异步触发与首次命中

工作副本复制、摘要和分类必须全部发生在异步 worker，不能进入 API 请求线程。当前第一阶段在
同一个 `IMPORT_WORKING_COPIES` 任务中完成单文件分析和原子提交，避免创建一个随后再次移动的
用户可见工作副本：

```text
IMPORT_WORKING_COPIES
-> 在工作副本目录创建隐藏临时文件
-> 解析document_pages
-> 生成普通文档摘要
-> 生成分类主题摘要
-> 生成分类建议
-> 生成规范文件名建议和主分类目录
-> 原子提交最终文件
-> 创建ACTIVE WorkingCopy和初始路径记录
-> IMPORT_WORKING_COPIES完成
```

隐藏临时文件不是 WorkingCopy 业务对象，不展示给用户，也不产生一次“先创建、再重命名”的
OperationPlan。最终工作副本在首次提交时必须保留上传文件名；命名建议只在回执中展示，用户明确
提出改名后才生成待确认的 OperationPlan。正式工作副本创建后的重命名和移动仍必须确认。后续当历史回填量或模型耗时需要
独立扩缩容时，再把相同分析步骤拆为`ANALYZE_DOCUMENT_VERSION`和独立`ANALYSIS`队列；拆分后
仍不得提前发布未整理的工作副本。

触发规则：

1. 用户上传形成的导入任务使用较高优先级，因为用户通常会立即提问。
2. 服务启动时批量导入的历史文件使用低优先级后台任务，不能占用API或归档线程。
3. 后续拆分独立分析队列后，用户明确打开、总结、分类、重命名或问答某文件，或者文件进入检索Top N时，视为“首次内容相关命中”。
4. 首次内容相关命中发现分析任务正在排队时，只提升同一幂等任务优先级，不创建重复任务。
5. 普通目录列表、受管目录扫描和仅元数据查询不属于内容相关命中，不能触发大批量模型调用。
6. 摘要失败不能把工作副本状态从`ACTIVE`改为不可用；失败只影响分析状态，并按降级策略读取原文。

### 4.5 摘要优先检索与证据问答

问答链路统一采用：

```text
问题
-> 普通文档摘要全文/向量检索，召回候选DocumentVersion
-> 分类主题摘要补充主题过滤和负向偶发主题信号
-> 在候选DocumentVersion范围内执行原文Chunk混合检索
-> EvidenceAnswer读取原文页面、Sheet、单元格或Chunk
-> 输出答案和原文引用
```

不同问题必须执行不同证据策略：

| 问题类型 | 摘要作用 | 最终依据 |
|---|---|---|
| 文件概览、主要内容 | 直接复用普通文档摘要 | 摘要关联的原文引用 |
| 查找相关文件 | 文档级召回和排序 | 命中文档的原文片段 |
| 分类原因 | 提供主旨和偶发主题判断 | `document_pages`原文证据 |
| 人名、日期、文号、具体条款 | 只用于定位候选文件 | 原文Chunk或页面 |
| 金额、合计、计数、排名 | 不参与计算 | 表格确定性Tool |
| 摘要未覆盖的问题 | 不得据此回答“没有” | 全文检索后再判断 |

系统不得提供`SUMMARY_ONLY`生产模式，也不得仅因摘要未出现某项内容就得出“原文不存在”的结论。

## 5. 摘要输出契约

### 5.1 普通文档摘要输出契约

`DocumentSummaryService`必须返回经过Pydantic schema校验的结构化对象，而不是只返回一段无法追踪的文本：

```json
{
  "overview": "本文件报告对三名军转干部的组织考察过程及任职评价。",
  "key_points": [
    {
      "text": "文件对象为三名军转干部。",
      "evidence_refs": [
        {
          "page_number": 1,
          "sheet_name": null,
          "quote": "关于对段东立等三位军转干部考察结果的报告"
        }
      ]
    }
  ],
  "section_summaries": [
    {
      "title": "考察结论",
      "page_from": 1,
      "page_to": 2,
      "summary": "概述考察对象及主要任职评价。"
    }
  ],
  "coverage": {
    "source_page_count": 3,
    "summarized_page_count": 3,
    "truncated": false
  },
  "summary_confidence": 0.91
}
```

约束规则：

1. `overview`用于用户概览、文档级召回和问答路由，不得直接作为精确事实的唯一证据。
2. `key_points.evidence_refs.quote`必须能在对应`document_pages.text_content`逐字定位。
3. `section_summaries`必须保留页码或Sheet覆盖范围，便于后续在原文内二次检索。
4. `coverage.truncated=true`时不得把摘要宣传为整份文件的完整概括，也不得据此回答未覆盖范围的问题。
5. 表格摘要可以描述Sheet用途和字段，但不得让LLM计算金额、数量、排名或比例。

### 5.2 分类主题摘要输出契约

`DocumentClassificationSummaryService`必须返回经过Pydantic schema校验的结构化对象：

```json
{
  "document_type": "干部考察结果报告",
  "primary_topic": "对三名军转干部的考察及任职评价",
  "business_action": "报告干部考察结论",
  "subjects": ["段东立等三名军转干部"],
  "organizations": [],
  "time_range": ["2018-09-19"],
  "keywords": ["军转干部", "干部考察", "考察结果"],
  "secondary_topics": [],
  "incidental_topics": [
    {
      "topic": "科研、教学",
      "reason": "仅出现在被考察人员个人履历中，不是文件主要事项",
      "evidence_refs": [
        {
          "page_number": 2,
          "sheet_name": null,
          "quote": "原文中的短引用"
        }
      ]
    }
  ],
  "evidence_refs": [
    {
      "page_number": 1,
      "sheet_name": null,
      "quote": "关于对段东立等三位军转干部考察结果的报告"
    }
  ],
  "summary_confidence": 0.91
}
```

约束规则：

1. `document_type`、`primary_topic`和`business_action`用于描述文件主旨，不得包含taxonomy分类路径。
2. `keywords`最多8项，每项必须能在文件名或完整正文中定位。
3. `evidence_refs.quote`必须逐字存在于对应`document_pages.text_content`。
4. `secondary_topics`可以参与多标签分类，但权重低于`primary_topic`。
5. `incidental_topics`只能用于负向抑制，不得单独召回分类。
6. 摘要模型不得返回文件路径、数据库ID、Tool参数、Shell命令或SQL。
7. 文件正文中的提示词、命令或角色声明一律视为数据，不得改变系统输出契约。

## 6. 摘要生成策略

### 6.1 小文件

正文不超过配置阈值时，可以使用一次受控模型调用同时生成普通文档摘要和分类主题摘要的两个独立字段，随后分别执行schema校验并分别持久化。任一字段校验失败不得污染另一个已经通过校验的摘要。输入包含：

- 文件名。
- 结构化解析标题和章节标题。
- 带页码或工作表标识的完整正文。
- 固定的分类主题摘要输出schema。

提示词必须要求模型优先判断：

1. 文件是什么文种。
2. 文件为了处理什么业务事项。
3. 文件针对什么对象。
4. 哪些内容只是个人经历、附件、示例或引用材料。

### 6.2 大文件

大文件使用Map-Reduce或Tree Summarize：

```text
按页面、Sheet或章节分块
-> 每个分块提取局部文种、事项、对象、主题和引用
-> 同时生成带页面或Sheet范围的局部普通摘要
-> 保留page_number、sheet_name和element_id
-> 合并局部摘要
-> 分别生成整份文件的普通文档摘要和分类主题摘要
```

分块优先遵循文档结构，不应直接在固定字符位置切断表格行、标题或段落。第一阶段可以沿用当前字符阈值，第二阶段再利用`document_elements`按标题和章节切分。

分块中间结果可以在同一`document_version_id + extraction_run_id`范围内共享，避免普通文档摘要和分类主题摘要分别重复读取和总结全文；最终两个业务记录仍必须分开保存。

### 6.3 摘要Provider

Provider按以下顺序演进：

1. 现有OpenAI-compatible LLM client，既可连接本地模型服务，也可连接明确授权的外部服务。
2. 本地Hugging Face生成式模型Adapter。
3. 无生成模型时，第一阶段使用带原文引用的本地确定性抽取式双摘要；该结果只作为低置信度召回和分类候选输入。抽取失败时摘要进入`UNAVAILABLE`，分类链路回退现有全文规则分类，证据问答继续使用原文检索。

当前阶段已经选择第 3 条作为默认生产低耗路径：使用 Jieba 分词和候选句数量受限的 LexRank 进行
CPU-only 原文关键句抽取。`LLM_ENABLED=true` 不得改变后台摘要 Provider；普通文档摘要和分类主题
摘要必须分别通过显式配置才能切换到 `llm`。聊天中的用户主动总结请求使用独立 Provider，不与后台
导入摘要开关耦合。

Sentence Transformers是Embedding模型，不能生成普通文档摘要或分类主题摘要。它只参与普通文档摘要的文档级召回，以及分类主题摘要和taxonomy描述之间的语义召回。

## 7. 分类候选召回与判定

### 7.1 召回输入调整

当前召回输入为：

```text
文件名 + 完整正文
```

目标召回输入调整为：

```text
文件名和正文标题
+ document_type
+ primary_topic
+ business_action
+ subjects
+ 已通过原文校验的keywords
+ secondary_topics的低权重信号
- incidental_topics的负向信号
```

完整正文不再直接作为规则匹配的主体输入，但仍保留给证据校验和受限LLM判定器。

### 7.2 混合召回

候选召回由两部分组成：

1. 规则召回：分类名、aliases、positive_signals、negative_signals和examples。
2. 语义召回：分类主题摘要向量与taxonomy节点的`name + description + aliases + examples`向量计算相似度。

两路候选取并集、按稳定`category_id`去重，最多保留8个候选。语义召回只能召回taxonomy中已经存在的分类，不得生成新路径。

本次误分类对应的taxonomy还需要同步补充：

```text
军转干部
干部考察
考察结果
考察报告
组织考察
```

只改摘要链路但不完善taxonomy信号，仍可能因为候选集合缺失而无法选择正确分类。

### 7.3 受限判定

`LLMClassificationJudge`继续只允许在候选`category_id`中选择0到3项。输入应同时包含：

- 分类主题摘要。
- 候选分类的id、路径、描述、命中原因和分数。
- 用于校验证据的原文片段或按需加载的完整正文。

默认关闭自由分类路径。需要自由分类时仍必须遵守`LLM_CLASSIFICATION_ALLOW_FREE_PATHS=true`，并只保存为`NEEDS_REVIEW`建议。

## 8. 原文证据校验

分类主题摘要不是证据。每个非“其他”分类必须执行以下校验：

1. `category_id`存在于当前taxonomy版本。
2. 至少有一项`evidence_items`。
3. `quote`能在对应DocumentPage中逐字定位。
4. `page_number`或`sheet_name`来自真实DocumentPage，不得由模型猜测。
5. 引用内容支持文件主旨，而不是仅支持偶发主题。
6. 文件名与正文证据冲突时，以正文和可定位证据为准。

无法完成原文定位时：

- 非“其他”分类状态降级为`NEEDS_REVIEW`。
- 不得伪造页码、工作表或quote。
- 不得写入正式`document_categories`。

## 9. 数据库与缓存方案

### 9.1 新增表

建议新增`document_summaries`，保存面向概览、检索和问答路由的普通文档摘要：

```text
document_summaries
- id uuid primary key
- document_id uuid not null
- document_version_id uuid not null
- extraction_run_id uuid not null
- input_sha256 text not null
- summary_text text not null
- summary_json jsonb not null
- coverage_json jsonb not null
- model_provider text not null
- model_name text not null
- prompt_version text not null
- schema_version text not null
- status text not null
- error_message text null
- created_at timestamptz not null
- updated_at timestamptz not null
```

建议新增`document_classification_summaries`：

```text
document_classification_summaries
- id uuid primary key
- document_id uuid not null
- document_version_id uuid not null
- extraction_run_id uuid not null
- input_sha256 text not null
- summary_json jsonb not null
- model_provider text not null
- model_name text not null
- prompt_version text not null
- schema_version text not null
- status text not null
- error_message text null
- created_at timestamptz not null
- updated_at timestamptz not null
```

`document_classification_runs`增加：

```text
- classification_summary_id uuid null
- classification_basis text not null default 'FULL_TEXT'
- summary_status text not null default 'DISABLED'
```

`classification_basis`建议枚举：

```text
FULL_TEXT
CLASSIFICATION_TOPIC_SUMMARY
FULL_TEXT_FALLBACK
```

### 9.2 缓存键

分类主题摘要缓存必须至少包含：

```text
document_id
+ document_version_id
+ extraction_run_id
+ input_sha256
+ model_provider
+ model_name
+ prompt_version
+ schema_version
```

普通文档摘要使用相同的版本键，并额外校验`coverage_json`。普通文档摘要和分类主题摘要可以引用同一次文档分析运行，但不能用其中一个表替代另一个表。

分类建议缓存继续包含：

```text
taxonomy_key
+ taxonomy_version
+ classifier_version
+ classification_summary_id
```

正文、解析器、OCR结果、模型、提示词或摘要结构任一变化时，不得复用不兼容的旧摘要。taxonomy变化只要求重新生成分类候选和分类建议；普通文档摘要以及与taxonomy无关的分类主题摘要在其他版本键不变时可以继续复用。

重命名和移动不产生新`DocumentVersion`，不得使摘要失效。工作副本内容修改、用户确认采用变化后的原始文件或其他内容变更产生新`DocumentVersion`后，必须为新版本生成摘要，旧版本摘要继续保留供历史对话追溯。

### 9.3 ChangeSet

普通文档摘要和分类主题摘要都属于分析派生结果，建议扩展`change_type`：

```text
DOCUMENT_SUMMARY_CREATED
DOCUMENT_SUMMARY_REUSED
DOCUMENT_SUMMARY_FAILED
CLASSIFICATION_SUMMARY_CREATED
CLASSIFICATION_SUMMARY_REUSED
CLASSIFICATION_SUMMARY_FAILED
```

随后继续记录：

```text
CATEGORY_SUGGESTED
CATEGORY_SUGGESTION_REUSED
DOCUMENT_PROCESSING_FAILED
```

这些ChangeItem都只代表分析结果和分类建议，不代表正式分类关系或文件移动。

## 10. 配置方案

建议新增以下配置：

```text
DOCUMENT_SUMMARY_ENABLED=true
DOCUMENT_SUMMARY_PROVIDER=extractive
DOCUMENT_SUMMARY_PROMPT_VERSION=document-summary-v1
DOCUMENT_SUMMARY_SCHEMA_VERSION=document-summary-schema-v1
DOCUMENT_SUMMARY_TRIGGER_MODE=hybrid
DOCUMENT_SUMMARY_SMALL_DOCUMENT_LIMIT=12000
DOCUMENT_SUMMARY_CHUNK_SIZE=8000
DOCUMENT_SUMMARY_MAX_CHUNKS=50
DOCUMENT_ANALYSIS_QUEUE=ANALYSIS
DOCUMENT_ANALYSIS_UPLOAD_PRIORITY=30
DOCUMENT_ANALYSIS_BACKFILL_PRIORITY=150
DOCUMENT_ANALYSIS_FIRST_HIT_PRIORITY=10
SUMMARY_QA_MODE=summary_first
LLM_CLASSIFICATION_SUMMARY_ENABLED=true
CLASSIFICATION_SUMMARY_PROVIDER=extractive
LLM_CLASSIFICATION_SUMMARY_PROMPT_VERSION=classification-topic-summary-v1
LLM_CLASSIFICATION_SUMMARY_SMALL_DOCUMENT_LIMIT=12000
LLM_CLASSIFICATION_SUMMARY_CHUNK_SIZE=8000
LLM_CLASSIFICATION_SUMMARY_MAX_CHUNKS=50
CHAT_DOCUMENT_SUMMARY_PROVIDER=llm
```

`DOCUMENT_SUMMARY_TRIGGER_MODE=hybrid`表示工作副本导入后创建低优先级分析任务，首次内容相关命中时提升同一任务优先级。`SUMMARY_QA_MODE`生产环境只允许`summary_first`或`full_text_only`，不得提供`summary_only`。

安全规则：

- 默认关闭外部普通文档摘要和分类主题摘要调用。
- 本地OpenAI-compatible模型可以在部署配置明确启用后使用。
- 将正文发送到外部模型必须有明确的部署授权或OperationPlan授权。
- 日志只能记录模型名、状态、耗时、字符数、覆盖率和错误码，不得记录正文、分块内容或完整摘要Prompt。
- 第一阶段分析由异步`IMPORT_WORKING_COPIES` worker执行，不能进入API请求线程；第二阶段拆分后必须使用独立`ANALYSIS`队列和并发限制，不能占用归档线程。

## 11. 失败与降级策略

| 场景 | 处理方式 |
|---|---|
| 工作副本刚导入 | 创建低优先级`ANALYZE_DOCUMENT_VERSION`，不阻塞工作副本进入`ACTIVE` |
| 首次内容相关命中且任务排队中 | 提升同一幂等任务优先级，不创建重复任务 |
| 普通文档摘要关闭或生成失败 | 文档级召回回退文件名、元数据和原文索引；问答不得伪装成摘要命中 |
| 分类主题摘要关闭 | 使用现有全文规则分类，`summary_status=DISABLED` |
| 模型不可用、超时或响应非法 | 回退全文规则，`classification_basis=FULL_TEXT_FALLBACK` |
| 分类主题摘要缺少主旨 | 返回“其他”或`NEEDS_REVIEW` |
| keywords无法在原文定位 | 丢弃对应keyword |
| evidence quote无法定位 | 分类建议降级为`NEEDS_REVIEW` |
| 长文件超过最大分块数 | 进入异步任务或`NEEDS_REVIEW`，不得静默截断 |
| 单文件失败 | 隔离失败，不影响同批次其他文件 |
| 分类主题摘要缓存命中 | 复用摘要并记录`CLASSIFICATION_SUMMARY_REUSED` |
| 摘要未覆盖用户问题 | 在候选文件内检索完整原文Chunk，不得直接回答“没有找到” |
| 数字、金额、计数或表格汇总 | 调用确定性Tool，不允许从摘要推算 |

全文规则回退结果不得伪装成分类主题摘要结果。回执中必须明确展示降级原因。

## 12. 开源项目调研与选型

### 12.1 LlamaIndex

项目地址：[run-llama/llama_index](https://github.com/run-llama/llama_index)

可参考能力：

- `DocumentSummaryIndex`按文档生成摘要，并把摘要映射回底层Nodes。
- 默认使用`TREE_SUMMARIZE`递归合并分块摘要。
- 摘要可以单独用于检索或Embedding。

相关源码：

- [DocumentSummaryIndex](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/indices/document_summary/base.py)
- [TreeSummarize](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/response_synthesizers/tree_summarize.py)

选型结论：参考其“每文档摘要、分块递归汇总、摘要映射原始节点”的设计，不整体引入LlamaIndex，避免和现有LangGraph、解析、StorageService及检索边界重复。

### 12.2 LangGraph

项目地址：[langchain-ai/langgraph](https://github.com/langchain-ai/langgraph)

LangGraph是低层状态编排框架，适合实现分块、并发、汇总、重试、异步等待和checkpoint，但不提供可以直接替代本项目分类服务的完整文档摘要器。

选型结论：继续作为分类任务编排层，不新增第二套工作流框架。

### 12.3 txtai

项目地址：[neuml/txtai](https://github.com/neuml/txtai)

官方文档：[Summary Pipeline与工作流](https://neuml.github.io/txtai/pipeline/)

txtai提供基于Transformers的本地Summary Pipeline，并且使用Python、FastAPI和Sentence Transformers，和本项目技术栈接近。

选型结论：暂不整体引入。后续如果需要一个标准化本地摘要Provider，可以实现txtai Adapter，但不能让txtai接管Agent Runtime、数据库或文件解析。

### 12.4 sumy

项目地址：[miso-belica/sumy](https://github.com/miso-belica/sumy)

sumy提供LexRank、LSA、Luhn等抽取式摘要，输出来自原文句子，幻觉风险低。

选型结论：可以作为无生成模型时的实验性本地降级Provider，但中文学校公文、表格和短文的主题识别质量需要单独评测，不能直接作为默认分类依据。

### 12.5 Hugging Face Transformers

项目地址：[huggingface/transformers](https://github.com/huggingface/transformers)

Transformers可以加载本地中文生成模型。其v5迁移文档已经建议从旧`SummarizationPipeline`转向现代Chat模型的文本生成方式：

- [Transformers v5 Migration Guide](https://github.com/huggingface/transformers/blob/main/MIGRATION_GUIDE_V5.md)

选型结论：本地Provider应基于现代生成式模型和结构化JSON输出实现，不再围绕旧SummarizationPipeline设计新代码。

### 12.6 最终选型

第一阶段采用：

```text
现有LangGraph
+ 现有OpenAI-compatible LLM client
+ 自研DocumentClassificationSummaryService
+ 参考LlamaIndex Tree Summarize
+ 现有Sentence Transformers语义召回
+ 现有taxonomy和证据校验
```

第一阶段不新增LlamaIndex、txtai或sumy生产依赖。

## 13. 实施步骤

### 阶段一：数据契约与确定性测试

1. 新增`DocumentSummary`和`ClassificationTopicSummary` Pydantic schema。
2. 为两类摘要分别定义固定Prompt、schema版本和缓存键。
3. 使用deterministic fake LLM测试小文件、大文件以及一次调用生成两个独立结果。
4. 增加普通摘要coverage校验器以及原文keyword、quote校验器。
5. 建立包含当前误分类文件模式和摘要遗漏精确事实问题的测试样本。

### 阶段二：双摘要服务

1. 把现有`LLMDocumentSummaryService`收敛为可持久化的`DocumentSummaryService`。
2. 实现`DocumentClassificationSummaryService`。
3. 两个服务都只能从`document_pages`读取完整正文，不接受Graph State全文。
4. 实现小文件单次总结和大文件Map-Reduce，并共享可审计的分块中间结果。
5. 增加缓存、覆盖率、超时、最大分块数和降级。
6. 日志记录状态、耗时、模型版本、覆盖率和错误码。

### 阶段三：数据库和审计

1. 创建`document_summaries`和`document_classification_summaries`迁移。
2. 扩展`document_classification_runs`。
3. 增加两类摘要的ChangeItem类型和持久化逻辑。
4. 为复用、失败、强制重处理和版本失效补充审计测试。

### 阶段四：异步分析与生命周期接入

1. 第一阶段由`IMPORT_WORKING_COPIES`异步任务在隐藏临时文件上串联解析、普通摘要、分类主题摘要、命名建议和分类建议；命名建议不得自动改变工作副本文件名。
2. 只有最终文件原子提交成功后才创建`ACTIVE WorkingCopy`，不向用户暴露中间路径和处理状态。
3. 上传工作副本使用较高优先级，历史批量导入使用低优先级。
4. 摘要或分类失败时使用原文件名和内部`待整理`目录降级，仍保证工作副本可用。
5. 第二阶段再增加`ANALYZE_DOCUMENT_VERSION`和独立`ANALYSIS`队列，并实现首次内容命中的幂等提优。
6. 单文件失败不阻塞其他文件，API、归档和正常对话不等待分析完成。

### 阶段五：分类链路接入

1. `DocumentClassificationService`先生成或复用分类主题摘要。
2. 规则Matcher改为使用结构化摘要特征。
3. 建立taxonomy描述向量并加入语义召回。
4. `LLMClassificationJudge`接收摘要和候选集合。
5. EvidenceValidator继续使用完整正文。
6. 递增`classifier_version`和相关taxonomy版本。

### 阶段六：摘要优先检索与证据问答

当前第一阶段已经让`hybrid-search`按最终文件名、分类建议和持久化普通文档摘要执行当前用户范围内的确定性文档级召回，并返回可解释的命中原因。以下全文索引、Embedding和原文Chunk二次检索继续作为下一阶段工作：

1. 为普通文档摘要建立PostgreSQL文档级全文索引和Embedding；未建立前保留当前确定性摘要召回作为安全降级。
2. 保留原文Chunk全文索引和Chunk Embedding，不能用摘要索引替代。
3. `hybrid-search`先召回DocumentVersion，再在候选版本内检索原文Chunk。
4. `evidence-answer`只消费可定位原文证据；普通摘要只用于路由和概览复用。
5. 对人名、日期、文号、条款等问题强制执行原文检索。
6. 对金额、计数、排名和表格汇总强制调用确定性Tool。
7. 摘要未覆盖问题时回退全文，不得仅凭摘要返回无依据结论。

### 阶段七：回执与运营观察

1. 普通用户回执只显示整理后的文件名和分类，不显示原文件名、处理状态、Skill或Tool。
2. “为什么这样分类”展示主旨、候选原因和原文证据。
3. 问答回执区分摘要召回、原文证据和确定性计算来源。
4. 只有重复、冲突、低置信度或失败需要用户决策时才展示确认或异常说明。
5. 管理页面可以查看摘要版本、模型版本、覆盖率和分类差异，但不展示不必要的完整正文。

## 14. 测试与评测方案

### 14.1 自动测试

至少覆盖：

- 工作副本导入成功后只创建异步分析任务，不在导入请求内同步解析或调用模型。
- 上传工作副本的分析优先级高于历史批量回填，且两者不会阻塞正常API、导入和归档任务。
- 首次内容相关命中可以幂等创建或提升同一`ANALYZE_DOCUMENT_VERSION`任务，不产生重复摘要和重复分类运行。
- 工作副本重命名、移动后复用同一`DocumentVersion`摘要；文件内容变化并创建新版本后旧摘要不得被错误复用。
- 普通文档摘要和分类主题摘要分别校验、分别持久化，一类摘要失败不污染另一类摘要。
- 概览问题可以复用普通文档摘要；人名、日期、文号、条款等精确问题必须回到原文Chunk取证。
- 摘要没有出现某项内容时必须继续检索原文，不能据此回答“原文不存在”。
- 文档级摘要召回命中文件后，仍能在对应版本内命中正确的原文页码、Sheet或单元格证据。
- 金额、计数、排名和表格汇总不从摘要推算，必须调用确定性Tool。
- 干部考察报告正文含科研、教学履历，但主分类为干部工作。
- 奖学金文件中出现教师、项目等泛词。
- 通知、工作安排、审批表、会议纪要等泛化文件名。
- 同一文件存在多个真实业务主题。
- PDF、DOC、DOCX、XLSX、TXT、MD和CSV。
- OCR文本存在错字和断行。
- Excel多个Sheet具有不同局部主题。
- 长文件Map-Reduce顺序和合并结果。
- 正文包含提示注入内容。
- 模型超时、非法JSON、空摘要和虚假quote。
- 摘要复用、强制重处理和版本失效。
- 单文件失败不影响批次中其他文件。

LLM和Embedding测试必须使用deterministic fake，不能依赖外部模型服务。

### 14.2 评测指标

建立人工标注的真实学校文件评测集，记录：

```text
主分类Top-1准确率
候选Top-3召回率
多标签Precision和Recall
偶发主题误报率
普通文档摘要覆盖率
文档级摘要检索Top-K召回率
摘要优先问答的原文证据命中率
精确事实问答准确率
原文证据有效率
NEEDS_REVIEW比例
首次命中触发后的可用等待时长
分析队列积压量和最长等待时长
单文件平均耗时
P95耗时
模型调用次数
长文件平均Token消耗
```

当前“干部考察报告误归科研、教学”必须成为固定回归样本。

## 15. Shadow上线方案

不得直接替换现有生产分类链路。上线分为：

### 第一步：本地安全启用态

当前实现默认`DOCUMENT_SUMMARY_ENABLED=true`、`LLM_CLASSIFICATION_SUMMARY_ENABLED=true`，但`LLM_ENABLED=false`时只运行本地确定性抽取式双摘要，不向外部发送正文。部署明确配置`LLM_ENABLED=true`并提供OpenAI-compatible模型后，才调用生成式双摘要。精确问答继续使用现有原文链路，不能从摘要直接生成事实结论。

### 第二步：Shadow态

同一文件同时生成：

```text
现有文档级召回结果
新版普通文档摘要召回结果
旧版全文分类结果
新版分类主题摘要结果
现有原文证据问答结果
新版摘要优先路由后的原文证据问答结果
人工确认结果
```

用户仍看到旧结果，服务端只记录文档召回差异、分类差异、问答证据差异、耗时和证据有效性。Shadow态不得因为生成摘要增加用户请求的同步等待时间。

### 第三步：小范围展示

按稳定用户桶或admin/ops账号展示新版建议，普通用户不自动切换。

### 第四步：默认启用

满足以下条件后才切换：

- 真实评测集主分类Top-1准确率明显提升。
- 偶发主题误报率下降。
- 文档级摘要检索Top-K召回率不低于现有检索链路。
- 摘要优先问答的精确事实准确率和原文证据命中率不低于现有链路。
- 原文证据有效率不低于现有链路。
- P95耗时和调用成本在可接受范围内。
- 模型故障时可以稳定降级且不会阻塞API、导入、归档和批量任务。

## 16. 验收标准

方案完成必须满足：

1. 每个已成功解析的工作副本版本可以生成或复用持久化普通文档摘要和结构化分类主题摘要。
2. 工作副本导入后通过独立异步分析任务生成摘要；首次内容相关命中可以幂等提升任务优先级，不阻塞正常API、导入和归档。
3. 同一`DocumentVersion`重命名或移动后继续复用摘要；内容变化产生新版本后按版本键重新生成。
4. 分类主题摘要明确区分主要主题、次要主题和偶发主题。
5. taxonomy候选不再直接由完整正文中的所有泛词决定。
6. LLM默认只能选择固定taxonomy候选。
7. 每个非“其他”分类都有真实原文证据。
8. 摘要和证据无法验证时结果进入`NEEDS_REVIEW`。
9. 普通文档摘要、分类主题摘要、分类运行、分类建议和ChangeSet均可追踪。
10. 摘要优先问答先通过普通文档摘要召回候选文件，再回到原文页、Chunk、Sheet或单元格取证。
11. 系统不提供`SUMMARY_ONLY`生产模式；摘要遗漏不能作为原文不存在的依据。
12. 数字、金额、计数、排名和表格汇总由确定性Tool计算，不能从摘要推算。
13. 大文件可以分块总结且不会把正文或分块内容写入`AgentGraphState`。
14. 普通文档摘要失败时问答可回退原文检索，分类主题摘要失败时分类可回退全文规则，两者都不影响工作副本`ACTIVE`状态。
15. 外部模型调用默认关闭并遵守明确授权边界。
16. 当前干部考察报告不再被科研、教学履历词误分类。
17. 后端相关测试通过，真实模型Smoke Test单独记录且不进入自动测试。

## 17. 本阶段不做

- 不让摘要模型自由创建正式taxonomy节点。
- 不把分类建议直接写入正式`document_categories`。
- 不根据分类结果自动移动文件。
- 不用普通文档摘要或分类主题摘要替换`document_pages`、原文Chunk、Evidence或确定性计算结果。
- 不实现`SUMMARY_ONLY`生产问答模式，不要求后续所有问答仅基于摘要。
- 不因为摘要遗漏某项内容就断言原文不存在该内容。
- 不在工作副本导入、目录扫描或普通API请求中同步执行全文总结。
- 不因文件重命名或移动重复生成摘要，也不跨`DocumentVersion`错误复用摘要。
- 不为了摘要功能整体引入LlamaIndex、txtai或另一套Agent Runtime。
- 不在没有明确授权时把学校文件正文发送到外部模型服务。
