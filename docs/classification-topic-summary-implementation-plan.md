# 分类主题摘要优先的文件分类实施方案

状态：Proposed  
日期：2026-07-21  
适用范围：File Agent 文档解析后的多标签分类链路

## 1. 方案目标

当前分类链路直接使用文件名和完整正文召回 taxonomy 候选。对于干部考察报告、个人履历、会议材料、附件汇编等文档，正文可能包含大量并非文件主旨的“科研”“教学”“学科”“项目”“成果”等词，导致这些偶发内容被错误提升为主要分类。

本方案引入“分类主题摘要”，把分类链路调整为：

```text
document_pages 完整正文
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

## 2. 可行性结论

该方案技术上可行，并且与当前项目架构兼容。

当前项目已经具备以下基础能力：

- `DocumentClassificationService`可以按`extraction_run_id`读取`document_pages`完整正文。
- `LLMDocumentSummaryService`已经实现小文件单次总结和大文件分块后汇总。
- `LLMClassificationJudge`已经限制模型只能从候选分类中选择，并校验引用必须来自原文。
- LangGraph、AgentRuntimeContext、Tool白名单、ChangeSet和分类建议持久化边界已经存在。
- 本地Sentence Transformers向量模型可以用于分类主题摘要与taxonomy描述的语义相似度计算。

但不能直接采用以下简单链路：

```text
完整正文 -> 一段普通自然语言摘要 -> 关键词分类
```

原因是普通摘要可能遗漏关键信息、使用原文中不存在的同义表达或产生幻觉。摘要只能用于提炼分类主题和召回候选，不能替代完整正文，也不能直接成为最终证据。

## 3. 统一术语

本方案统一使用以下名词，后续代码、数据库、日志、测试和页面不得混用其他近义命名。

| 中文名词 | 代码命名 | 含义 |
|---|---|---|
| 分类主题摘要 | `ClassificationTopicSummary` | 从完整正文提炼出的结构化分类输入，不是面向用户的普通摘要 |
| 分类主题摘要服务 | `DocumentClassificationSummaryService` | 读取完整正文并生成分类主题摘要的运行时服务 |
| 分类主题摘要记录 | `DocumentClassificationSummary` | 分类主题摘要的持久化数据库记录 |
| 分类候选 | `CategoryCandidate` | 从固定taxonomy召回的待判定分类，不等于正式分类 |
| 分类建议 | `DocumentCategorySuggestion` | AgentRun生成的`SUGGESTED`或`NEEDS_REVIEW`结果 |
| 偶发主题 | `incidental_topics` | 仅在履历、附件、示例或引用内容中出现，不代表文件主要目的的主题 |
| 原文证据 | `evidence_items` | 能定位到原始页面、工作表和原文片段的分类依据 |

“分类主题摘要”不得简称为“分类结果”。“普通文档摘要”继续指面向用户总结、讲解或问答时生成的自然语言内容。

## 4. 目标架构

### 4.1 总体流程

```text
用户请求读取并分类文件
-> Tool读取或生成document_pages
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

### 4.2 数据边界

分类主题摘要链路必须继续遵守三层数据边界：

- `AgentGraphState`只保存`classification_summary_id`、状态、警告和必要的轻量回执字段，不保存完整正文、分块正文、模型客户端或数据库会话。
- `AgentRuntimeContext`保存`DocumentClassificationSummaryService`、`DocumentClassificationService`、LLM client、Embedding Provider等运行时服务。
- `Persistent Stores`保存`document_pages`、分类主题摘要记录、分类运行、分类建议、原文证据和ChangeSet。

分类主题摘要服务不得由Graph节点直接查询`DocumentPage`。Graph只能传递`document_id`、`extraction_run_id`和受控参数，由请求级运行时服务读取正文。

### 4.3 Tool边界

第一阶段不新增直接面向Planner的Tool名称。分类主题摘要作为`multi-label-classify`或当前等价分类Tool内部的受控步骤执行，继续由同一ToolInvocation覆盖整个分类事务。

这样可以避免Planner绕过证据校验单独生成摘要，也避免摘要模型输出直接触发数据库写入。后续只有在分类主题摘要需要独立重处理、独立运营查看或独立队列调度时，才评估新增`classification-topic-summarize` Tool。

## 5. 分类主题摘要输出契约

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

正文不超过配置阈值时，使用一次模型调用生成分类主题摘要。输入包含：

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
-> 保留page_number、sheet_name和element_id
-> 合并局部摘要
-> 生成整份文件的分类主题摘要
```

分块优先遵循文档结构，不应直接在固定字符位置切断表格行、标题或段落。第一阶段可以沿用当前字符阈值，第二阶段再利用`document_elements`按标题和章节切分。

### 6.3 摘要Provider

Provider按以下顺序演进：

1. 现有OpenAI-compatible LLM client，既可连接本地模型服务，也可连接明确授权的外部服务。
2. 本地Hugging Face生成式模型Adapter。
3. 无生成模型时回退现有全文规则分类。

Sentence Transformers是Embedding模型，不能生成分类主题摘要。它只参与分类主题摘要和taxonomy描述之间的语义召回。

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
```

分类建议缓存继续包含：

```text
taxonomy_key
+ taxonomy_version
+ classifier_version
+ classification_summary_id
```

正文、模型、提示词、摘要结构、taxonomy或分类器任一变化时，不得复用不兼容的旧分类结果。

### 9.3 ChangeSet

分类主题摘要属于分析派生结果，建议扩展`change_type`：

```text
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
LLM_CLASSIFICATION_SUMMARY_ENABLED=false
LLM_CLASSIFICATION_SUMMARY_PROVIDER=openai_compatible
LLM_CLASSIFICATION_SUMMARY_PROMPT_VERSION=classification-topic-summary-v1
LLM_CLASSIFICATION_SUMMARY_SMALL_DOCUMENT_LIMIT=12000
LLM_CLASSIFICATION_SUMMARY_CHUNK_SIZE=8000
LLM_CLASSIFICATION_SUMMARY_MAX_CHUNKS=50
```

安全规则：

- 默认关闭外部分类主题摘要调用。
- 本地OpenAI-compatible模型可以在部署配置明确启用后使用。
- 将正文发送到外部模型必须有明确的部署授权或OperationPlan授权。
- 日志只能记录模型名、状态、耗时、字符数和错误码，不得记录正文、分块内容或完整摘要Prompt。

## 11. 失败与降级策略

| 场景 | 处理方式 |
|---|---|
| 分类主题摘要关闭 | 使用现有全文规则分类，`summary_status=DISABLED` |
| 模型不可用、超时或响应非法 | 回退全文规则，`classification_basis=FULL_TEXT_FALLBACK` |
| 分类主题摘要缺少主旨 | 返回“其他”或`NEEDS_REVIEW` |
| keywords无法在原文定位 | 丢弃对应keyword |
| evidence quote无法定位 | 分类建议降级为`NEEDS_REVIEW` |
| 长文件超过最大分块数 | 进入异步任务或`NEEDS_REVIEW`，不得静默截断 |
| 单文件失败 | 隔离失败，不影响同批次其他文件 |
| 分类主题摘要缓存命中 | 复用摘要并记录`CLASSIFICATION_SUMMARY_REUSED` |

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

1. 新增`ClassificationTopicSummary` Pydantic schema。
2. 为分类主题摘要定义固定Prompt和版本号。
3. 使用deterministic fake LLM测试小文件和大文件输出。
4. 增加原文keyword和quote校验器。
5. 建立包含当前误分类文件模式的测试样本。

### 阶段二：分类主题摘要服务

1. 实现`DocumentClassificationSummaryService`。
2. 从`document_pages`读取完整正文。
3. 实现小文件单次总结和大文件Map-Reduce。
4. 增加缓存、超时、最大分块数和降级。
5. 日志记录状态、耗时、模型版本和错误码。

### 阶段三：数据库和审计

1. 创建`document_classification_summaries`迁移。
2. 扩展`document_classification_runs`。
3. 增加ChangeItem类型和持久化逻辑。
4. 为复用、失败和重处理补充审计测试。

### 阶段四：分类链路接入

1. `DocumentClassificationService`先生成或复用分类主题摘要。
2. 规则Matcher改为使用结构化摘要特征。
3. 建立taxonomy描述向量并加入语义召回。
4. `LLMClassificationJudge`接收摘要和候选集合。
5. EvidenceValidator继续使用完整正文。
6. 递增`classifier_version`和相关taxonomy版本。

### 阶段五：回执与运营观察

1. 逐文件回执显示分类主题摘要状态。
2. “为什么这样分类”展示主旨、候选原因和原文证据。
3. 降级结果展示`NEEDS_REVIEW`和失败原因。
4. 管理页面可以查看摘要版本、模型版本和分类差异，但不展示不必要的完整正文。

## 14. 测试与评测方案

### 14.1 自动测试

至少覆盖：

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
原文证据有效率
NEEDS_REVIEW比例
单文件平均耗时
P95耗时
模型调用次数
长文件平均Token消耗
```

当前“干部考察报告误归科研、教学”必须成为固定回归样本。

## 15. Shadow上线方案

不得直接替换现有生产分类链路。上线分为：

### 第一步：关闭态

完成代码、迁移和测试，默认`LLM_CLASSIFICATION_SUMMARY_ENABLED=false`。

### 第二步：Shadow态

同一文件同时生成：

```text
旧版全文分类结果
新版分类主题摘要结果
人工确认结果
```

用户仍看到旧结果，服务端只记录分类差异、耗时和证据有效性。

### 第三步：小范围展示

按稳定用户桶或admin/ops账号展示新版建议，普通用户不自动切换。

### 第四步：默认启用

满足以下条件后才切换：

- 真实评测集主分类Top-1准确率明显提升。
- 偶发主题误报率下降。
- 原文证据有效率不低于现有链路。
- P95耗时和调用成本在可接受范围内。
- 模型故障时可以稳定降级且不会阻塞批量任务。

## 16. 验收标准

方案完成必须满足：

1. 分类前生成或复用结构化分类主题摘要。
2. 分类主题摘要明确区分主要主题、次要主题和偶发主题。
3. taxonomy候选不再直接由完整正文中的所有泛词决定。
4. LLM默认只能选择固定taxonomy候选。
5. 每个非“其他”分类都有真实原文证据。
6. 摘要和证据无法验证时结果进入`NEEDS_REVIEW`。
7. 分类主题摘要、分类运行、分类建议和ChangeSet均可追踪。
8. 大文件可以分块总结且不会把正文写入AgentGraphState。
9. 外部模型调用默认关闭并遵守明确授权边界。
10. 当前干部考察报告不再被科研、教学履历词误分类。
11. 后端相关测试通过，真实模型Smoke Test单独记录且不进入自动测试。

## 17. 本阶段不做

- 不让摘要模型自由创建正式taxonomy节点。
- 不把分类建议直接写入正式`document_categories`。
- 不根据分类结果自动移动文件。
- 不用分类主题摘要替换`document_pages`或用户可见的普通摘要。
- 不为了摘要功能整体引入LlamaIndex、txtai或另一套Agent Runtime。
- 不在没有明确授权时把学校文件正文发送到外部模型服务。

