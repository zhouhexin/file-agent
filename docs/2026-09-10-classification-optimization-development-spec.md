# 文件分类优化开发实施文档

版本：v1；日期：2026-09-10；状态：待开发。本文是开发与验收契约，不表示下述能力已上线。

上位方案：[分类规则优化方案 v2](2026-09-10-workdata-classification-rule-optimization-plan.md)。本轮用户明确要求：所有分类兜底进入“其他”，不出现分类“待复核/待分类”；明确更正和明确移动不二次确认。与旧规范冲突的地方，执行本轮要求。

## 1. 固定交付范围和完成条件

本次交付包含候选规则修复、统一其他兜底、主分类选择、分类/目录联动、直接请求授权、前后端/MCP 接入、历史兼容、测试和发布工具。保留多标签、原件保护、逐文件审计、真实失败状态。

| 编号 | 必须成立的结果 |
| --- | --- |
| INV-01 | 每个纳入新版策略且发布成功的 ACTIVE 工作副本，其当前版本恰好有一个有效 PRIMARY；可以为业务父类、业务叶类、已授权用途类或 `system.other` |
| INV-02 | PRIMARY 的版本化 organization_path 与真实目录一致，允许受控收纳段；工作副本、版本存储引用、回执一致 |
| INV-03 | 分类证据不足、无业务候选、组织范围不明、质量策略不接受细分时，完成 OTHER 收纳，不创建分类复核任务 |
| INV-04 | 明确 SET_PRIMARY/MOVE 校验成功后即执行；客户端无需也不得补发 `/confirm` 才能完成 |
| INV-05 | 对象/目标不唯一先选择；选择完成后继续原请求执行，不再确认；不得将模型自造目标当明确授权 |
| INV-06 | 内容不变的更正/移动不创建内容版本、不重复 OCR/Chunk；实际关系或路径变化只递增一次 revision |
| INV-07 | 人工有效主类不被后台规则覆盖；本轮明确更正可替换，保留原关系、反馈和授权历史 |
| INV-08 | 文件系统移动失败或数据库提交失败不得提前返回已完成；重试不重复写有效关系、路径审计或 ChangeItem |
| INV-09 | 自动业务精度和 OTHER 比例分别计量；不能把全部 OTHER 当“业务分类准确率 100%” |

排除项：不重排 `E:\workdata` 原始文件；不执行生产迁移或全量重分类；不增加模型外发默认授权；不自动启用全局学习规则。本文中的发布、迁移命令均为实施阶段步骤，不是本次已执行操作。

### 1.1 对“其他”的统一解释

新自动兜底只使用 `system.other`，其显示路径、organization_path 均为 `["其他"]`，文件落在共享工作根的 `其他/文件名`，必要时使用第 8 节的受控冲突收纳。稳定 ID 的 `system` 前缀不作为显示目录层。

历史 `school.other`、`college.other`、各业务 `.other/.issued` 保持原 ID 和关系可查询；不因升级而直接改写人工记录。新版不再自动生成这些分支兜底。不能通过向“学校/其他”塞入未知文件而捏造学校范围。

“其他”是完成的分类结果；`classification_outcome=OTHER`，可有内部 `classification_quality=INSUFFICIENT`。解析失败仍 `extraction_status=FAILED`；移动错误仍 `placement_status=ERROR`。这些真实技术失败不属于分类复核，不准掩盖。

### 1.2 直接执行授权矩阵

| 请求 | 行为 | 授权记录 |
| --- | --- | --- |
| 冻结清单的首次自动整理 | 按入口已有范围自动落位，包括 OTHER | `INITIAL_ORGANIZE`，关联真实 ingest item/任务 |
| “把这份文件主分类改为财务”且对象、目标唯一 | 更新主类并按对应目录移动，不二次确认 | `EXPLICIT_REQUEST` |
| 结构化主类选择按钮提交 | 点击提交即明确请求，直接执行 | `EXPLICIT_REQUEST`，UI 提交事件 |
| “移动到学院/教学”且文件和路径唯一 | 反向解析目标主类，直接联动执行 | `EXPLICIT_REQUEST` |
| 明确移入其他 | 主类改为 `system.other` 并移动 | `EXPLICIT_REQUEST` |
| “分类错了”但未指定目标，或存在多个同名文件 | 返回对象/目标选择，不先更正到其他 | 保存原请求；选择产生 `EXPLICIT_REQUEST` |
| 只添加/拒绝辅助标签 | 更新建议/反馈或对应辅助关系，位置不变 | 保留现有逻辑反馈审计 |
| “先给我方案/建议，别移动” | 只生成预览或建议 | `PREVIEW_ONLY`，不可执行 |
| 上传重复、目标重名且未指定处理方式 | 保留必要重复/冲突选择 | 选择作用于当前冻结冲突，不允许静默覆盖 |
| 删除、覆盖、复制、独立重命名、恢复、外发 | 继续使用既有权限和确认规则 | 不继承 SET_PRIMARY/MOVE 例外 |

“明确更正”在本文指分类更正，独立文件重命名沿用现有重命名规则；移动默认保留名称。一次对话包含移动和删除时拆开授权，不能整个计划自动通过。固定集合的明确批量更正/移动也无需二次确认，需持久化成员清单、逐项检查并返回 PARTIAL；歧义项不影响其他已唯一解析的项。

## 2. 当前代码基线与开工检查

读取基线 HEAD：`dffae7daf1b6745ce9f87c5a6b50f0d3b68fc00c`，taxonomy `2026-09-v9`，加载后 116 节点。工作树已有其他开发改动，不允许覆盖或重置。每个工作包开始前执行 `git status --short` 并核对本包相关 diff。

本轮再次核查发现以下可复用事实，不能按旧方案重复创建：

- `WorkingCopy.revision` 已存在，默认 1。
- `WorkingCopyPathRecord.operation_plan_id`、`operation_confirmation_id` 都已可空。
- `DocumentOrganizationDecision` 已有 authorization_source、source_request_id、before_revision、after_revision、idempotency_key。
- `DocumentCategory` 已有当前工作副本/内容版本的有效 PRIMARY 唯一索引。
- `DocumentCategoryFeedback.suggestion_id`、`DocumentClassificationRun.agent_run_id` 当前非空。直接按文件 ID 更正不得伪造建议或 AgentRun 来满足外键。
- `WorkingCopyOperationService.execute()` 会查询可空 Confirmation，但其自身缺少本文要求的独立授权检查；不能把“无 Confirmation 也运行”当完整直接授权。
- `agent/tool_registry.py` 的动作入口描述已提到显式整理可直接移动；`file_lifecycle/conversation_operations.py` 是必须核对和归并的既有入口，禁止旁建第二套执行逻辑。
- `conversation_operations.py` 已对部分明确分类整理调用 `confirm_plan(confirmation_text=message)` 并执行。本次保留“不二次询问”的用户行为，改用真实 EXPLICIT_REQUEST 授权快照；旧确认记录保留历史，不追溯删除。
- 当前分类树硬插入 `__needs_review__`，且前端 `ClassificationFilesPage.tsx` 和后端 `classification/result_builder.py` 有复核文案，必须一起移除新版分类投影中的对应流程。

迁移目录已见 `20260908_0005`，但开发前必须运行 `alembic heads` 核实真实迁移图，不能把文件名排序视为 head。使用用户现有 Python 环境，Windows 本次检查使用 `D:\anaconda\envs\myenv\python.exe`。

## 3. 模块边界和改动清单

以下新文件名称为本次固定建议；如合并文件，必须在评审中说明职责映射并同步本文，不允许漏掉职责。

| 文件/目录（相对 apps/api/app） | 开发动作 |
| --- | --- |
| `modules/classification/schemas.py` | 扩展节点角色、候选/主类/可见性控制，合法 fallback 配置 |
| `modules/classification/loader.py`、`unified_builder.py` | 固定快照加载；物化节点角色；可重现构建与映射校验 |
| `modules/classification/matcher.py` | 兜底退出召回、局部简历、业务/组织分离、去除候选早退 |
| 新 `modules/classification/document_features.py` | 从页面/结构元素构造分来源的特征与证据定位，不负责写入 |
| 新 `modules/classification/rule_policy.py` | 加载版本化规则、字段校验、组合信号和业务竞争规则 |
| 新 `modules/classification/purpose_policy.py` | 校验包成员与用途授权，生成独立用途候选 |
| 新 `modules/classification/primary_selection.py` | 根据固定优先级决定唯一主类、辅助建议、OTHER 结果 |
| 新 `modules/classification/input_fingerprint.py` | 分类内容指纹与上下文指纹，避免 OCR 更新复用旧缓存 |
| `modules/classification/classifier_service.py`、`runtime_factory.py` | 统一全文入口、使用新服务、缓存和结果契约 |
| `modules/classification/auto_placement_policy.py` | 质量结果区分 business accepted 与 OTHER fallback，不再返回分类复核任务 |
| 新 `modules/classification/placement_schemas.py`、`placement_service.py`、`placement_repository.py`、`placement_reconciler.py` | 冻结命令、持久化操作、事务协调及中断恢复 |
| 新 `modules/operations/authorization.py` | 服务端核验明确请求/首次任务/已有确认，签发运行时授权上下文 |
| 新 `modules/file_lifecycle/working_copy_executor.py` | 受控文件发布、移动、同名保护及操作身份日志 |
| `modules/file_lifecycle/service.py`、`operations.py`、`conversation_operations.py` | 首次发布、旧移动和更正入口共同复用协调器/执行器 |
| `modules/classification/decision_service.py`、`conversation_decision.py`、`clarification_service.py`、`feedback_service.py` | 反馈接收和关系应用拆分；明确目标后直执行；辅助关系不移动 |
| `modules/classification/router.py`、`feedback_schemas.py`、`organization_schemas.py`、`organization_query_service.py`、`result_builder.py` | API、OTHER 树/列表/计数、兼容层与真实回执 |
| `modules/agent/tool_registry.py`、`tool_schemas.py`、`tool_contracts.py`、`catalog.py`、`graph.py`、`planner.py`、`adaptive_planner.py` | 注册窄范围新 Tool、消费异步结果、消除二次确认路由 |
| `modules/agent/user_receipt.py`、`file_task_receipt.py` | 分类 OTHER 不制造 NEEDS_REVIEW；不把已接受任务显示为已完成 |
| `modules/integrations/router.py`、`modules/ingestion/workflow.py` | 集成 API、首次整理、重复复用与外层状态归并 |
| `db/models.py`、`../alembic/versions/` | 第 6 节增量模型和迁移 |
| `modules/retrieval/search_profile.py`、`modules/classification/graph_outbox.py` | 提交后投影与幂等消费，旧分类移除、新类增加 |
| `apps/mcp/file_agent_mcp/{server,client,conversation_tools}.py` | 只转接窄范围更正/移动接口和操作查询，后端处理路径和分类 |
| `apps/web/src/features/files/ClassificationFilesPage.tsx`、`apps/web/src/api/client.ts`、聊天分类卡及操作卡组件 | OTHER 展示、直接提交、轮询操作、只保留歧义/冲突选择 |

所有新增模块、公开方法和边界逻辑加中文说明。Graph State 仅保存轻量业务 ID、候选摘要及结果；服务实例、全文、数据库会话、授权上下文对象通过请求级 RuntimeContext/factory 注入。

## 4. Taxonomy 与规则的实现契约

### 4.1 节点字段和编译规则

新增字段：

| 字段 | 类型/默认值 | 必须满足 |
| --- | --- | --- |
| node_kind | `GROUP/BUSINESS/FALLBACK/REFERENCE` | 根分组 GROUP；`.other/.issued` 按配置编译为 FALLBACK，不靠运行时关键词猜 |
| recall_enabled | bool | FALLBACK/GROUP 必须 false |
| primary_enabled | bool | true 则必须有合法 organization_path |
| selectable | bool | 新更正选项过滤 false；旧 ID 查询不受此限制 |
| visible | bool | 控制新树节点；隐藏历史节点的文件要通过第 10 节兼容投影可查 |

旧 JSON 未填字段时按配置的显式 fallback 节点集合/模板生成规则编译；不可仅用 ID 后缀误判将来合法业务节点。加载后输出的快照必须字段齐全。

新增顶层可落位节点 `system.other`，name=`其他`，organization_path=`["其他"]`，kind=FALLBACK、recall_enabled=false、primary_enabled=true、selectable=true、visible=true。顶层 GROUP 才禁止主类，不能继续将深度 1 一律排除。不得创建 `system.unclassified` 或虚拟复核节点。

新 fallback 配置固定 `target_category_id=system.other`；不再自动按部门物化新的“发文/其他”。旧物化 ID 必须在兼容快照中保留，以解析历史映射。该节点不加“未分类/待分类”召回别名。

校验必须覆盖 ID 唯一、节点角色、正反映射、Unicode NFC 与大小写归一化路径冲突、Windows 保留名、路径段长度、嵌套分类边界。系统根路径不可与任何来源归档根重叠。规范化只用于比较，不静默重写用户历史文件名。

首版业务新增固定采用方案 §5.2 的信息化双节点、学院专业认证/教学评估、学校本科评估、办公参考资料节点；分别添加真实正文正反样本并更新构建输入。发布版本拟 `2026-09-v10`，已被占用时递增并统一所有快照引用。

### 4.2 规则配置文件

新增 `rules/classification-policies/workdata-v1.json` 及对应 JSON Schema。仅接受固定操作符，不执行表达式、脚本或动态 Python。

每条规则必填：`rule_id`、`version`、`candidate_category_ids`、`subject_signals`、`action_signals`、`title_signals`、`negative_contexts`、`scope_policy`、`evidence_requirements`、`regression_sample_ids`。信号组合固定为 `all_groups` / `any_group` / `same_section`，不得由 LLM 生成可执行条件。

生产配置与评测标签隔离，`regression_sample_ids` 仅用于验证，不成为在线匹配特征。所有权重、窗口、候选 K 和门槛进入有版本配置；改变配置必须改变指纹。

### 4.3 特征与简历检测

`DocumentFeaturesV2` 包含 filename、body_title、section refs、sheet/header refs、issuer/recipient/subject refs、document_type、resource_type、source_context_ref。完整文本仅在服务运行时按引用访问。

局部简历初始配置：连续不超过 12 个段落或同一表单、窗口不超过 3,000 字符；至少 3 种独立履历结构组，且窗口/真实题名出现简历表达，或已验证招聘包用途。窗口不能跨不相关章节拼接；中英文同类字段计一组。无法定位到单一主体时不触发高优先级简历规则。该数值是可回放初始参数，校准后版本化发布。

整篇题名为评估报告、制度汇编、教程，且履历片段属于引用/示例/人员介绍时，简历只能成为局部内容特征，不能决定整篇主类。`Research Statement` 作为研究陈述文种，只有独立招聘用途证据才能成为招聘主类。

去掉 `return [recruitment_resume]` 早退，专用规则仅追加候选；职称表单也不得直接锁死学校层级。业务候选先基于对象/动作构建，再结合主体范围，学校/学院不提前硬过滤。

### 4.4 评分与业务竞争

现有 rule_score 保留诊断兼容；新增 `business_score`、`scope_score`、`purpose_basis`、`evidence_support` 分项，禁止将 scope_score 作为某个业务类别的独立成立条件。

同一证据位置命中短词与长词只保留最长同类表达。FALLBACK 不参加 matcher、LLM 候选、语义原型和图谱候选竞争。通用文种候选只有在具体业务不成立或明确综合行政主体时才可为 PRIMARY；有正文依据仍可作为文种元数据。

固定实现规则集：职称/招聘/聘任、招聘/人才、审计/财务/劳资、本科/研究生/学科、工会/教学、信息化/教学/安全、认证/费用/新闻、教程/业务文件、外校参考/本校业务；判据采用上位方案 §4.4，每组至少两个正例和两个反例。

保留最多 8 个去重候选，并优先保留强业务及组织镜像候选；质量比较使用不同业务意图的最强竞争者，同一主类的父子候选和重复规则不互相制造假歧义。

### 4.5 证据门槛与 fallback 算法

新增 `PrimarySelectionResult`：primary_candidate、secondary_candidates、classification_outcome、classification_quality、selection_basis、reason_codes、policy_version、input_fingerprint。`classification_outcome` 仅 `CLASSIFIED/OTHER`，不含复核状态。

按顺序执行：

1. 本轮明确 SET_PRIMARY/MOVE 的合法目标优先；用户用途无需正文重复出现，但需真实授权。
2. 无新明确目标则保留有效人工 PRIMARY，后台只能记录新建议。
3. 校验通过的用途包决定主用途，同时保留合格正文辅助候选。
4. 对业务候选检查证据与发布质量政策；唯一通过者或经过竞争裁决的通过者为主类。
5. 叶类不足时仅选有独立业务证据且可落位的父类；不取多个冲突类别的宽泛共同祖先充数。
6. 其余返回 `system.other`、outcome=OTHER、basis=FALLBACK；原因为 NO_BUSINESS_EVIDENCE / AMBIGUOUS_PRIMARY / UNKNOWN_SCOPE / LOW_QUALITY / PARSE_UNAVAILABLE 等。

OTHER 的 evidence_json 可以为空；不捏造“其他”原文引文。拒绝的业务候选仅进入诊断持久化，不显示为已完成业务标签。新候选保存 SUGGESTED；不新建分类 NEEDS_REVIEW 记录。已有历史 NEEDS_REVIEW 只读兼容，不驱动用户队列。

生产质量策略必须从校准产物加载，不允许复用注释中的阈值作为已验证值。校准未发布时使用 `policy_mode=conservative_rules`：仅接受上述经过回归验证的强结构/对象动作规则及明确用途/人工目标，其余 OTHER。不得阻塞归档等待人工分类。

完整正文取得失败时可生成 OTHER 收纳结果，但仍保留 extraction FAILED/PARTIAL；宏、加密等检查不允许发布的文件维持原处理限制。模型失败可以回退确定性规则，不能把外部模型不可用变成整批阻塞。

## 5. 用途包、缓存和服务接口

### 5.1 用途包不可变快照

新增 `classification_purpose_packages`：id、workspace_id、root_key、source_container_id、purpose_category_id、taxonomy_version、policy_id/version、manifest_digest、members_json、authorization_source、source_request_id、created_at。不可变行，更新创建新行；members 保存 managed_file_id/document_version_id/sha256 等已解析成员引用，按有稳定事实的入口使用，禁止只存可变路径或匹配任意子目录。

首批启用招聘/职称，认证/评估/审计通过对应规则回归后启用同一机制。目录名只用于识别 Profile 候选，最终包身份由冻结导入清单与受控政策决定。不得把源路径中的“应聘”子串直接当整个包已授权。

包文件的 content_candidates 不得被 package_candidate 覆盖；自动包用途不用置信度 1.0 表示语义概率。成员补充创建新 manifest；原请求只能处理原成员。

### 5.2 缓存分两层

内容候选指纹：DocumentVersion/sha256、实际提取文本摘要哈希、元素结构版本、解析配置、taxonomy 内容哈希、正文规则、摘要 Provider/模型/提示配置、参与召回的语义/图谱代次、原始命名特征。

主类选择指纹：内容候选指纹 + 包快照/政策 + 有效人工关系版本 + 本次明确目标（如有）。来自不同包的同一字节内容可复用解析与正文候选，不能复用另一用途的主类。

`DocumentClassificationService.classify()` 仍为全文入口，先获得有效抽取指纹，再查内容缓存；摘要缺失时按本地 Provider 生成。同一摘要和全文内容相同只运行一次 matcher。人工关系不因缓存失效而删除。

接口固定职责：

| 接口 | 输入 | 输出/副作用 |
| --- | --- | --- |
| `build_document_features(extraction_ref)` | 当前文档版本与成功/部分抽取引用 | 运行时结构特征；无写入 |
| `recall_candidates(features, taxonomy_snapshot, rule_policy)` | 完整特征和冻结配置 | 排名候选；无正式关系写入 |
| `select_primary(candidates, effective_primary, purpose_snapshot, explicit_target, quality_policy)` | 经校验的业务/授权事实 | 唯一决策或 OTHER；无文件写入 |
| `submit_placement(command, authorization_context)` | 第 7 节冻结命令和服务端上下文 | 持久化 plan/operation/job，返回操作 ID |
| `execute_placement(operation_id)` | 只接受持久化操作 ID | 重验、文件操作、提交、审计 |
| `reconcile_placement(operation_id)` | 同一操作 ID | 恢复或返回结构化错误，不重做分类 |

## 6. 数据库增量契约

标识生成 UUID 字符串，与当前 `String(36)` 外键保持一致；本次不进行全库物理 UUID 类型转换。PostgreSQL 新结构 JSON 用 JSONB，时间用 timestamptz；测试 SQLite 按项目 JSON variant 方式兼容，不能用 SQLite 测试替代生产约束验证。

### 6.1 扩展现有表

| 表 | 新增字段/调整 | 迁移策略 |
| --- | --- | --- |
| working_copies | placement_status varchar(32) NOT NULL 默认 LEGACY_UNCHECKED；placement_policy_version varchar(80) 可空 | revision 已存在，不重复增加；旧记录不默认 IN_SYNC |
| document_classification_runs | input_fingerprint varchar(64)、input_manifest_json JSONB、decision_json JSONB | 指纹历史为空则缓存 miss；保留真实 agent_run_id 约束，不伪造运行 |
| document_category_suggestions | 使用现有 candidate_scores_json/evidence_json 保存规则分、证据属性、schema_version | 不增重复评分列；新结果状态 SUGGESTED |
| document_category_feedback | application_status varchar(32) 默认 LEGACY；placement_operation_id 可空 FK | 仍绑定 suggestion；无 suggestion 的 SET_PRIMARY 由 placement 操作记录授权和反馈语义 |
| document_organization_decisions | placement_operation_id 可空 FK | 复用已有授权/revision 字段，decision 新增 APPLIED_BUSINESS/APPLIED_OTHER，仅真实提交后写 |
| working_copy_path_records | placement_operation_id 可空 FK | 复用已有可空 plan/confirmation；直接请求 Confirmation 保持 null |
| operation_plans | authorization_mode varchar(32) 默认 CONFIRMATION_REQUIRED；authorization_context_json JSONB 默认 {}；conversation_id 改可空 | 新增 AUTHORIZED/EXECUTING 状态兼容序列化；只允许授权服务填上下文；无会话仅允许有真实集成请求来源的计划 |
| tool_invocations | agent_run_id 改可空；placement_operation_id 可空 FK | CHECK 要求 agent_run_id 或 placement_operation_id 至少一个非空；真实异步执行不伪造运行 |
| change_sets | agent_run_id/conversation_id 改可空；placement_operation_id 可空 FK | CHECK 要求 agent_run_id 或 placement_operation_id 至少一个非空；无会话权限从真实 actor/workspace/operation 解析 |

`authorization_context_json` 保存来源事件 ID、actor/client、作用对象清单摘要、请求摘要、授权策略版本及时间；原始用户消息引用现有消息表，不把全文指令复制到日志。HTTP/MCP 结构化请求保存规范化命令摘要；公开接口禁止写此 JSON。

当前 OperationPlan.conversation_id、ToolInvocation.agent_run_id 和 ChangeSet 的会话/运行外键非空，以上调整是无会话结构化入口的必做项。聊天仍保留真实关联；审计查询不得因改为 nullable 而绕过 actor 权限，不能只 inner join AgentRun 导致独立操作审计消失。DocumentClassificationRun 仍需真实 AgentRun：无会话 SET_PRIMARY/MOVE 不新建分类运行，直接使用已保存候选或明确目标。

新增 FK 形成相互引用时使用具名 FK 分阶段创建：先表和列，再补约束；drop 按反向依赖，不靠关闭外键。ChangeSet/ToolInvocation 在有真实 placement operation 后写入，提交审计与排队同事务，执行审计记录每次实际 handler 尝试及结果。

### 6.2 classification_placement_operations

每行代表一个工作副本的一次冻结操作，多文件共用 OperationPlan，一个文件一行并各自提交。

| 字段 | 类型与约束 |
| --- | --- |
| id | UUID 字符串主键 |
| workspace_id / actor_user_id / working_copy_id | 非空 FK；用户字段表示真实发起者，不代表共享副本所有者 |
| client_id / request_id | varchar(120) 非空，由已认证上下文产生 |
| idempotency_key / request_digest | varchar(160)/varchar(64) 非空 |
| operation_type | INITIAL_ORGANIZE / SET_PRIMARY / MOVE / RESTORE / RECONCILE |
| operation_plan_id / job_id / changeset_id | 可空 FK；公开直接更正/移动必须有 plan 和 job |
| authorization_source | INITIAL_ORGANIZE / EXPLICIT_REQUEST / CONFIRMED_PLAN / RECOVERY |
| expected_revision / expected_document_version_id | bigint 与版本 FK，非空 |
| source_sha256 / source_identity_json | 内容哈希及源文件身份事实，非空 |
| before_primary_relation_id | 可空 FK，保留原主类引用 |
| before_relative_path / target_relative_path | 非空相对路径，不保存任意宿主绝对路径 |
| target_category_id / taxonomy_key / taxonomy_version / taxonomy_digest | 非空，提交时验证冻结快照 |
| policy_version / target_filename / container_segments_json | 非空；容器必须经 resolver 验证 |
| decision_snapshot_json / authorization_snapshot_json | 非空 JSONB，结构化决策与已核验授权引用 |
| state | PREPARED / EXECUTING / FS_APPLIED / COMMITTED / RETRYABLE_FAILED / RECONCILING / FAILED / CANCELLED |
| attempt_count / execution_token / lease_expires_at | 计数默认 0；token/租约可空 |
| error_json / result_json | JSONB 默认 {} |
| created_at / updated_at / completed_at | 时区时间，completed_at 可空 |

唯一约束 `(workspace_id, actor_user_id, client_id, idempotency_key, working_copy_id)`；同键不同 digest 返回冲突。同一工作副本活动写占用采用本表部分唯一索引，覆盖 PREPARED/EXECUTING/FS_APPLIED/RETRYABLE_FAILED/RECONCILING；本次不再加第二个易失同步的 active operation 指针。

路径占用表 `working_copy_path_reservations`：root_id、normalized_path_hash、normalized_relative_path、placement_operation_id UNIQUE、created_at；`(root_id, normalized_path_hash)` 唯一，哈希相同还需核对规范路径。保留占用直到提交或核实未产生文件副作用后终止。路径保留并不能替代操作系统不覆盖保证。

现有已确认改名/删除/恢复/内容修改也必须取得同一副本写占用。统一锁获取顺序：按 working_copy_id 排序锁副本，再占目标路径；不要只让新增 MCP 入口遵守。

### 6.3 迁移要求

第一迁移扩展字段/表/约束，兼容旧读取；第二阶段数据脚本生成分类兼容与迁移预览。schema upgrade 不移动文件，不把旧 NEEDS_REVIEW 批量改为成功。

开发前检查真实 Alembic head 与现有 JSON 类型。迁移 upgrade/downgrade 在隔离库验证；已有未完成操作时禁止降级删除操作表，应先完成恢复或暂停发布并处理操作。历史审核记录、确认来源、原件哈希、DocumentVersion 不能清空。

## 7. API、Tool 与直接执行授权

### 7.1 命令 schema

新增 `PlacementCommand`，Pydantic `extra=forbid`：

```json
{
  "working_copy_id": "<uuid>",
  "action": "SET_PRIMARY",
  "expected_revision": 6,
  "expected_document_version_id": "<uuid>",
  "target_category_id": "college.finance",
  "taxonomy_version": "2026-09-v10",
  "container_segments": [],
  "idempotency_key": "<stable-submission-id>"
}
```

字段约束：working_copy_id、expected_document_version_id 使用 UUID 校验；revision 为大于零整数；target_category_id 最长 255；taxonomy_version 最长 80；idempotency_key 1–160；container_segments 最多 20 段，复用安全路径段规则，另校验整条最终路径及分类边界。

SET_PRIMARY 必须给 category ID。MOVE 接受 `target_category_id + container_segments` 或 `target_root_key + target_directory_segments` 两种之一；二者同时传入返回 422。目录形式由后端反向映射，输出规范 PlacementCommand，再生成请求摘要。根目录、未知目录、越界或不同工作副本根不自动注册为分类。

不接收 `skip_confirmation/authorized/actor_user_id/authorization_source`；客户端传这些字段返回 422。不接收任意 source_path，不接收任意 LLM 脚本。MOVE 目标文件名固定为当前名称；改名属于独立请求。

后台首次整理允许内部 action INITIAL_ORGANIZE，RESTORE/RECONCILE 只从有相应授权的后台/既有确认流程进入；普通 SET_PRIMARY/MOVE API 不能伪造这些动作。

### 7.2 HTTP 接口

| 接口 | 请求与响应 |
| --- | --- |
| `POST /api/classification/working-copies/{id}/primary-category` | JWT；SET_PRIMARY 请求，路径 ID 与正文一致；新执行统一 202 |
| `POST /api/classification/working-copies/{id}/placement` | JWT；MOVE 请求；新执行统一 202 |
| `GET /api/classification/placement-operations/{operation_id}` | 返回真实进度/结果；请求审计仅原发起者或 ops/admin 可读 |
| `POST /api/classification/placement-operations/{operation_id}/retry` | 无新目标；显式重试原操作；不得改变冻结对象和路径 |
| `/api/integrations/v1/working-copies/{id}/primary-category`、`.../{id}/placement` | 复用同一服务；按既有集成鉴权解析真实用户与 client；禁止仅信客户端 user_id |
| `/api/integrations/v1/placement-operations/{operation_id}`、`.../{operation_id}/retry` | 与普通接口相同的查询/重试行为 |

新请求使用异步持久化任务，返回形状：

```json
{
  "operation_id": "<uuid>",
  "status": "PREPARED",
  "working_copy_id": "<uuid>",
  "effective_primary": {"category_id": "school.finance"},
  "pending_primary": {"category_id": "college.finance"},
  "placement_status": "PENDING",
  "requires_confirmation": false,
  "file_position_changed": null
}
```

完成后的 GET 响应：

```json
{
  "operation_id": "<uuid>",
  "status": "COMMITTED",
  "working_copy_id": "<uuid>",
  "working_copy_revision": 7,
  "effective_primary": {
    "category_id": "college.finance",
    "category_path": ["学院", "财务管理"],
    "status": "CONFIRMED"
  },
  "pending_primary": null,
  "placement_status": "IN_SYNC",
  "relative_path": "学院/财务管理/示例文件.docx",
  "file_position_changed": true,
  "original_unchanged": true,
  "index_status": "READY",
  "requires_confirmation": false,
  "display_markdown": "主分类已更正为学院/财务管理，文件已移动到对应目录。"
}
```

示例路径必须由实际 taxonomy 解析；不是硬编码值。索引异步未就绪则返回 UPDATING，不伪造 READY。自动 OTHER 使用 AUTO_APPLIED，用户明确选择 OTHER 使用 CONFIRMED。

同幂等键同参数：未结束返回 202/原操作，已结束返回 200/原结果。客户端不得在网络重试时重新生成提交 ID。批量接口或 Tool 内部调用各项共享 request_id，各自幂等键稳定派生；只按原集合重试未完成项。

### 7.3 授权服务

新增运行时 `PlacementAuthorizationContext`，由后端构造，不可序列化后交给 LLM 再回传信任。包含 actor/client、scope、source_event_ref、request_digest、授权方式、策略版本。

授权服务按来源验证：

1. 聊天：服务端读取当前用户真实消息；对象范围由后端附件/共享文件解析器确定；意图包含执行更正/移动且不含只建议/先不要执行。LLM 只给结构化候选，不拥有授权决定权。歧义保存 clarification 与原消息关联。
2. UI/API/MCP：鉴权后的专用 SET_PRIMARY/MOVE 提交本身就是明确请求；schema、稳定 ID、当前版本、revision 和作用范围须全部通过。MCP 不能伪造用户权限或提供任意物理路径。
3. 首次整理：验证固定 ingest item/附件清单与冻结策略，不能对既有 ACTIVE 文件复用 INITIAL_ORGANIZE 身份。
4. 既有确认：仅对其他需要确认的操作读取真实 OperationConfirmation，保持原授权边界。
5. 恢复：只可恢复原已授权操作和冻结目标；恢复不是新分类授权。

直接请求创建内部 OperationPlan，`authorization_mode=EXPLICIT_REQUEST`，状态 AUTHORIZED，写真实授权快照，Confirmation 表不插入假记录，`confirmed_at` 不填。执行中 EXECUTING，真实完成 EXECUTED，失败 FAILED，批量部分成功 PARTIAL。单项进度和崩溃恢复以 placement operation 为准。

`authorize_execution(plan, operation, context)` 是所有执行器的硬入口；直接请求仅允许 SET_PRIMARY/MOVE，首次发布独立授权。不得通过放宽 `confirm_plan()` 或给所有 `requires_confirmation` 置 false 实现本需求。

执行前重新检查发起者账户、当前共享文件操作权限和目标根权限；授权被撤销则停止新文件副作用，保留实际已发生阶段用于恢复。retry 仅接受 RETRYABLE_FAILED 或可验证恢复状态；COMMITTED 返回原结果，EXECUTING 返回当前进度；FAILED/CANCELLED 且目标需变更时必须新提交，不在原幂等键下换目标。

### 7.4 Tool 白名单

新增窄范围 Tool `working-copy-placement-submit` 和 `working-copy-placement-status`：

| Tool | 输入 | 副作用/授权 | 失败 |
| --- | --- | --- | --- |
| working-copy-placement-submit | 经后端解析的文件 ID、目标 ID/目录、版本、revision、稳定提交 ID | 写 plan/operation/job/ToolInvocation；明确请求或首次任务由后端核验；新执行不二次确认 | 参数/权限/目标/冲突错误结构化返回，不能继续执行 |
| working-copy-placement-status | operation_id | 只读授权范围内进度；恢复后的状态亦来自数据库 | 找不到或无权限按现有隐藏存在性约定返回 |

只有 handler、schema、output schema 和审计实现完成后才能注册。Planner 对已明确更正/移动使用 submit；evidence/change 节点聚合结果到 result_summary；response 不扫描 tool_results 拼装虚假成功。异步任务 SUBMITTED 表示该 Tool 已成功受理，不表示实际移动 COMPLETED。

原 `classification-decision` 在 PRIMARY ACCEPT/CORRECT 时转交统一提交服务；SECONDARY/RELATED/DOCUMENT_TYPE 继续逻辑反馈。原 `working-copy-action-plan-create` 对明确 MOVE 路由同一服务，旧入口不得再创建第二个移动计划。`confirmed-file-action` 保持其他需确认操作的边界，复用同一低层执行器，不开放给模型自由执行。

新 Tool 的声明式 `requires_confirmation=false` 不等于授权已经通过；授权服务仍须验证实际用户请求。状态查询不触发分类、写关系或移动。

后台执行使用内部白名单 `working-copy-placement-execute`，输入仅 operation_id，配置为不可进入 Planner/MCP Catalog；worker 经过同一 schema/审计 dispatcher 调用，handler 读取冻结授权后进入执行器。HTTP/MCP 受理也复用 submit 的校验服务；独立请求的 ToolInvocation 用真实 placement_operation_id 关联，业务失败写 FAILED，受理成功与后续执行各记各的结果，不把 job 成功当整个文件操作完成。

### 7.5 错误与选择

统一 `{ "error": { "code": "...", "message": "..." } }`；重要错误：

| 错误/状态 | HTTP | 行为 |
| --- | ---: | --- |
| AUTH_REQUIRED / FORBIDDEN | 401 / 403 | 无写入 |
| TARGET_SELECTION_REQUIRED | 409（专用接口）；聊天返回选择卡 | 无执行副作用，展示后端固定选项；选择后继续 |
| CATEGORY_NOT_PLACEABLE / TARGET_OUTSIDE_CLASSIFICATION_TREE / CONTAINER_CROSSES_CATEGORY_BOUNDARY | 422 | 不造目录、不自动换类 |
| WORKING_COPY_REVISION_CONFLICT / TAXONOMY_VERSION_STALE | 409 | 原操作不执行，客户端刷新快照并明确重新提交，不后台覆盖预期值 |
| IDEMPOTENCY_CONFLICT | 409 | 同键不同参数拒绝 |
| TARGET_NAME_CONFLICT | 409 | 返回有权限冲突候选，保留当前主类与路径 |
| PLACEMENT_IN_PROGRESS | 409 | 返回可访问的已有操作引用，无第二个写任务 |
| PLACEMENT_RECONCILIATION_REQUIRED | 409 | 显示恢复中或错误；禁止新并发写 |
| FILE_PUBLISH_CAPABILITY_UNAVAILABLE / CROSS_DEVICE_MOVE_UNSUPPORTED | 409 | 不使用潜在覆盖实现替代 |

自动分类的 AMBIGUOUS_PRIMARY 不返回 TARGET_SELECTION_REQUIRED，应归 OTHER 并完成。只有用户明确操作指向不明对象/目标时才需要选择，这两种歧义不得混淆。

## 8. 路径、冲突与一致性执行

### 8.1 正反路径解析

扩展 `CategoryOrganizationPathResolver`：正向接受 category_id + 冻结 taxonomy + 容器段；反向在授权工作根内匹配最深已注册分类路径，按路径段比较，不能字符串 startswith。

允许 CATEGORY_WITH_OPTIONAL_CONTAINER。材料包容器来自验证的包名称/批次和稳定 ID；同类收纳移动保留主类；跨类移动清除旧容器，仅按目标允许政策生成。容器不得跨入另一个注册分类目录。

`system.other` 的源目录位置不成为业务范围；年份、人员名只作为有授权的容器，不成为新 taxonomy 节点。路径生成结果全程传同一 category_id/suggestion_id，不再读取 latest 分类首项替换。

### 8.2 同名规则

已有文件的明确移动保留名称，检测到现有工作区名称冲突或真实目标占用时返回冲突，不自动覆盖或悄悄改名。用户选定另一可用位置后执行原明确移动，不再追加确认。用户选择覆盖时转现有覆盖授权流程，不由 MOVE 自动推导授权。

首次导入维持支持同名文件并行归档的业务边界：目标目录同名时使用受控 `_items/<固定 ingest item ID>/原文件名` 收纳段，或复用已有等价不改名收纳机制；该规则作为版本化允许容器，不进入其他分类边界。显示名保持原名，不靠拒绝整个导入批次解决。已有副本纯移动的重名校验保持现有范围，若需改变共享范围名称唯一策略须另行设计。

无位置变化时分两种：同类仅用户确认来源新增，写来源审计、关系状态变化时 revision 增一次；分类、路径、来源均已一致的重复语义请求返回 NO_CHANGE，不再次增 revision。幂等重放始终返回原结果。

### 8.3 执行协议

事务 A：验证授权、内容版本和 revision；锁 working_copy；检查是否活动操作；写 OperationPlan 快照、operation=PREPARED、目标路径占用、FilesystemJob，再提交。反馈可记录“已收到”，effective PRIMARY 此时保持原值。不得在等待 worker、OCR 或用户时持有长事务。

worker 领取租约并取得进程级/系统级互斥；重验源文件身份和 SHA256，校验实际目标。将操作推进 EXECUTING，记录持久化发布意图，再执行不覆盖移动。成功后记录目标文件身份和 FS_APPLIED。

事务 B：重验操作 token、锁和版本；在一次事务中结束旧 PRIMARY、建立新 PRIMARY、保留辅助关系、更新 WorkingCopy、当前工作版本/对应 FileObject、PathRecord、ChangeSet/ChangeItem、瘦检索投影、Outbox；state=COMMITTED、placement_status=IN_SYNC，释放占用。

只通过当前版本/工作副本的确定 FileObject 关联更新，禁止 `filter(document_id).first()` 改错归档原件。旧关系结束状态使用现有允许枚举并填 ended_at，不删除；人工来源记录留存。当前版本最多一个 PRIMARY 的数据库约束与至少一个的提交断言都必须保留。

单文件提交失败不得回滚其他已提交文件；批量 plan 按实际结果派生 PARTIAL。新的内容版本、分类/目录变更和 no-op 需单独处理，不以重新 OCR 修复路径变更。

### 8.4 文件不覆盖实现和恢复

首版只支持同文件系统移动。Windows 使用不带 REPLACE_EXISTING 的系统移动能力或已证明失败不覆盖的封装；Linux 使用 `renameat2(RENAME_NOREPLACE)` 等经过真实平台验证的能力。探测能力不可用时返回明确错误，不退回 `os.replace`。Windows 大小写改名等特殊情形若未实现，报告能力限制，不做两次无审计改名。

持久化操作身份日志位于共享工作根内部受控操作目录，以 operation_id 命名，保存相对路径、源身份、目标身份、哈希和阶段。先持久化意图再执行文件动作。文件系统锁与身份日志独立于数据库 token；旧 worker 未确认停止时新 worker 不得仅因租约过期同时执行。

| 恢复观测 | 必须执行 |
| --- | --- |
| 源在、目标不在 | 重验身份与快照，重试同一目标 |
| 源不在、目标在，持久化证据证明属于本操作 | 重验内容和身份，补事务 B |
| 源和目标都在 | RECONCILING；核对操作日志，禁止因哈希相等删除一份 |
| 两处都不在、目标身份无法证明、内容已改变 | 标记 ERROR，保留操作与占用直到人工运维处置，不伪完成 |
| 数据库 COMMITTED | 返回原结果，不重复移动或写 ChangeSet |

发布后尚未记录目标身份即崩溃时，必须依据预写日志与稳定文件身份验证归属；不能验证则停止恢复写，不凭文件名/相同哈希认领。APPLYING/RECONCILING 期间下载采用受控解析或返回可重试状态，不能按旧数据库路径声称文件仍在原处。

## 9. 反馈与各入口的统一行为

`ClassificationDecisionService` 拆为记录反馈和应用关系两部分；主类应用仅事务 B 执行。自然语言更正去掉“先写分类、再 prepare MOVE、外层 COMPLETED”的旧过程。

无 suggestion 的结构化 SET_PRIMARY 直接创建 placement/plan 和 DocumentOrganizationDecision，source_suggestion_id=null；不写虚构 DocumentCategoryFeedback。来自建议的请求保留真实 suggestion/feedback 关联。无会话集成请求不伪造 AgentRun，相关对象支持可空真实关联；若经现有 Agent Runtime 路由，使用实际运行 ID。

PRIMARY ACCEPT 表示“把这条建议设为主类”，直接执行；REJECT 未生效建议只记录负反馈；“撤销当前主类并归其他”是明确 SET_PRIMARY(system.other)。仅“撤回我此前的确认”只撤回本人来源，不能删除其他用户来源或等同于全局撤销共享主类。

PRIMARY 更正后图谱正负样本来源于真实用户动作；自动 OTHER、自动业务落位和目录位置不能升级为人工正样本。移动失败的更正记录为收到意图、应用失败，不能将未生效目标作为有效 PRIMARY 投影。

MCP 新增 `file_set_primary_category`、`file_move`、`file_placement_status`，参数必须包含稳定 document_id/working_copy_id 与当前版本/revision（客户端可先查询获得），目标 ID/受控目录和稳定提交 ID。Document ID 到唯一 ACTIVE WorkingCopy 的解析由后端完成，多值则选择。MCP 不写副本，不计算分类，不拼宿主路径，不调用 `/confirm`。

分类页面提交主类选择后立即显示处理中并轮询 operation；同一个提交不再弹“是否移动”。刷新/重连读取后端 operation/clarification；超时不创建新操作。辅助标签按钮不触发移动；选择卡文案明确动作与共享目录作用范围，但不增加额外确认步骤。

## 10. 用户投影和历史兼容

### 10.1 新公开分类契约

树响应新增 `other_file_count`、`business_classified_file_count`、`schema_version=2`；分类文件项新增 classification_outcome、placement_status、effective_primary、pending_primary、legacy_location。新前端不使用 needs_review_file_count、review_only、organization_decision=NEEDS_REVIEW。

`classified_file_count` 定义为存在有效 PRIMARY 的文件数，包含 OTHER；`business_classified_file_count` 排除所有 fallback/兼容 OTHER；按 working_copy_id 去重。total_active_files 不包含 TRASHED，不能累加每个祖先节点计数当总数。

公开分类树不返回 `__needs_review__`、system.unclassified 或分类复核按钮。聊天结果、批次回执、分类列表、MCP display_markdown 不以分类依据不足返回“待复核/待分类/请确认分类”。允许文案：“已归入其他，暂未识别到明确业务类别。” 用户之后仍可主动更正。

删除/冲突/外部发送等非分类确认卡仍保留，不全局替换所有 NEEDS_REVIEW 常量。低层旧状态可用于只读诊断，新分类流程不得以该状态等待用户。

### 10.2 兼容与物理迁移

旧 `category_id=__needs_review__` 或 `review_only=true` 请求由适配器规范为 OTHER 聚合视图，附 deprecated 标识；旧计数字段在一个兼容版本内可保留为该聚合数，仅供老客户端，不能向新版 UI 暴露复核语义。query 规范化后再分页/计数，不能前端仅改标签而查询还走旧队列。

OTHER 聚合包含新 system.other、历史 fallback 主类、没有可靠主类的历史活动文件。历史 fallback 的真实 ID 和真实位置在文件项保留：`effective_primary` 不伪造；可通过 `classification_outcome=OTHER` 和 `legacy_location=true` 说明它属于展示分组。新数据 INV-01/02 验收范围必须剔除 LEGACY_UNCHECKED，单独报告尚未迁移数量。

对隐藏的历史 `.issued/.other`，查询兼容解析原 ID，但新选择器只提供 system.other；保留旧人工来源，不默默结束。历史 NEEDS_REVIEW 的建议记录不直接当 PRIMARY，也不复制写成 CONFIRMED。

历史批次步骤：只读冻结文档/版本/哈希/主类/真实位置清单 → 生成逐项 before/after → 取得用户对该批次“按新规则更正并移动”的明确指令 → 逐项使用 placement 服务。该明确指令已是授权，不再弹第二次确认。仅授权评估/预览时不移动。迁移不在 schema upgrade 中执行，原件始终不变。

业务主类已人工确定的文件优先按人工目标修复路径；历史人工选的分支其他保留，除非本次迁移授权明确包括统一这些其他节点。UI 可先用兼容 OTHER 视图，无需先搬完所有历史文件才能去除复核流程。

## 11. 开发工作包、依赖与提交要求

每个工作包单独提交，提交前检查 diff，仅包含本包修改；现有 dirty 文件有重叠时先核对变化再补丁，不覆盖其他任务。建议提交前缀见下表。

| 包 | 步骤与文件 | 验收出口 | 依赖 |
| --- | --- | --- | --- |
| D0 `docs:` 固定基线 | 更新项目规范中两个例外，保存当前 taxonomy/配置快照；将 19 个样本编号，原件只读 | 最新明确要求在规范和两份文档一致；现状与目标分开 | 无 |
| D1 `fix:` 候选规则 | schemas/loader/matcher/rule_policy；去 fallback 竞争、简历早退和组织硬裁剪 | T01–T07 通过，现有正确财务样本不退化 | D0 |
| D2 `feat:` 分类决策 | purpose_policy/primary_selection/fingerprint/classifier_service；构建 v10 候选及 OTHER | T08–T13；JSON 构建可重现，无分类复核挂起 | D1 |
| D3 `feat:` 数据及授权 | models/Alembic、authorization、placement_schemas/repository | 空库和历史库迁移；T14–T18；无假 Confirmation | D0 |
| D4 `feat:` 执行恢复 | placement_service/reconciler/executor、首次发布与旧操作复用 | T19–T25；真实 Windows/Linux 和 PG 测试证据 | D2、D3 |
| D5 `feat:` 全入口 | API/Tool/Graph/反馈/MCP/UI、OTHER 兼容投影 | T26–T32；各入口明确操作均无二次确认 | D4 |
| D6 `test:` 校准和试点 | 19 样本回归、200 样本问题集、600–1,000 文件族评测、隔离灰度脚本 | 分层精度/覆盖/OTHER 比例、故障恢复报告 | D2；上线依赖 D5 |
| D7 `docs:` 发布交付 | API/schema/runbook/树文档、兼容与迁移操作说明 | 第 14 节全部满足 | D6 |

上述是依赖关系，不要求启用多 agent。不得仅完成 D1/D2 就宣布主类/目录联动或免二次确认已经上线。新接口出现在公开 catalog 前，D4 和对应鉴权测试必须完成。

## 12. 自动化测试与验收矩阵

新增测试路径位于 `apps/api/app/tests`，前端测试在 `apps/web/tests`，MCP 测试在 `apps/mcp/tests`。真实敏感样本与完整路径清单不提交 Git；deterministic fixture 使用脱敏结构化文本，真实文件回放用独立 manifest。

| 编号 | 输入/场景 | 必须断言 | 测试文件（新增或修改） |
| --- | --- | --- | --- |
| T01 | 正文仅有“其他”和学院名称 | 普通候选不出现任意 fallback；最终仅 system.other | test_taxonomy_matcher.py |
| T02 | 加载 v9 兼容与新配置 | 历史 ID 可解析；system.other 可顶层落位；无复核节点 | test_taxonomy_loader.py |
| T03 | 132 页教学报告的脱敏结构，履历词散在各章节 | 招聘不独占候选；教学评估进入候选 | test_taxonomy_matcher.py |
| T04 | 真正单人应聘简历、Research Statement 有/无包 | 真简历保留招聘；无用途不能只凭陈述题名定招聘 | test_primary_selection.py |
| T05 | 学校通知提及学院、学院填报引用学校文件 | 不提前排除正确组织业务分支 | test_taxonomy_matcher.py |
| T06 | 工会女职工体检、会议写作教程 | 工会及参考资料判据成立；不能误判教学/真实纪要 | test_classification_rule_policy.py |
| T07 | 财务制度位于信息化处；收费自查含“其他” | 财务强业务正确，无党建/其他竞争污染 | test_classification_rule_policy.py |
| T08 | 合法材料包中论文/经费佐证 | 主用途+正文辅助均保留，语义概率不伪造 1.0 | test_classification_purpose_policy.py |
| T09 | 应聘根下学校名单、包外文件、伪造成员哈希 | 不继承整目录用途，拒绝不匹配快照 | test_classification_purpose_policy.py |
| T10 | 无正文、低质量、组织歧义 | outcome OTHER；不创建分类 NEEDS_REVIEW/确认卡 | test_primary_selection.py |
| T11 | 明确目标/已有人工主类/后台新建议 | 本轮目标优先；后台不得覆盖人工 | test_primary_selection.py |
| T12 | OCR 补页、政策/包/模型变化 | 自动缓存失效；相同事实复用；人工关系不消失 | test_classification_freshness.py |
| T13 | 用户更名与系统自动生成名 | 不出现自我强化循环；纯移动不重新解析 | test_classification_input_fingerprint.py |
| T14 | API 注入 skip_confirmation/actor_user_id | 422，无 plan/job/file 写入 | test_placement_authorization.py |
| T15 | 明确消息、结构化提交、只建议、不明确消息 | 仅明确提交授权；不以 LLM 自填字符串授权 | test_placement_authorization.py |
| T16 | 明确分类更正执行，包括无会话 API | 无 `/confirm` 调用，无 OperationConfirmation 新行；可空运行关联下审计完整且按 actor 鉴权 | test_placement_entrypoints.py |
| T17 | 同键重试、同键不同参数、同副本并发写 | 返回原任务、409、唯一活动写占用 | test_classification_placement.py |
| T18 | 删除/覆盖/独立重命名借 MOVE 授权 | 拒绝扩权，保留原确认规则 | test_operations.py |
| T19 | 首次 OTHER 发布 | 原名/原件不变，有效 PRIMARY 与其他目录一致 | test_file_lifecycle.py |
| T20 | SET_PRIMARY、跨类 MOVE、同类容器 MOVE | 分类与目录同一提交；辅助关系保留；revision 正确 | test_classification_placement.py |
| T21 | no-op 及幂等重放 | 不重复加 revision/ChangeItem/关系 | test_classification_placement.py |
| T22 | 目标同名、进程外抢占 | 目标不覆盖，旧主类/路径不提前改成功 | test_working_copy_executor.py |
| T23 | FS 后 DB 提交失败、租约过期旧 worker 未停 | 恢复协议成立，无双 worker 写，无假完成 | test_placement_recovery.py |
| T24 | 目标同 hash 不同身份、两边都有文件 | RECONCILING/ERROR，无自动删除或认领 | test_placement_recovery.py |
| T25 | 更新 FileObject、投影与 Outbox | 原件记录不变，当前版本引用正确，投影幂等 | test_stage6_classification_outbox.py |
| T26 | UI/MCP/聊天/旧反馈 PRIMARY 路径 | 都调用同一协调服务，不二次移动，不二次确认 | test_placement_entrypoints.py |
| T27 | RELATED 默认反馈、撤回本人确认 | 位置不变，不删除其他用户有效来源 | test_classification_feedback.py |
| T28 | 分类树/列表/批次/聊天/MCP | 仅 OTHER 兜底；无分类复核卡；实际解析失败仍显示 | test_classification_organization_query.py 等 |
| T29 | 旧 review_only/虚拟节点链接、历史无主类 | OTHER 聚合正确，分页一致，不伪造物理路径/PRIMARY | test_classification_organization_query.py |
| T30 | 歧义选择后刷新、重连、重复提交 | 继续同一操作；无二次确认；跨用户不可读选择历史 | test_placement_entrypoints.py |
| T31 | 批量三项：成功、冲突、技术失败 | 逐文件真实状态，整体 PARTIAL，失败不回滚已完成 | test_classification_placement.py |
| T32 | 搜索/阅读工具收到移动文字 | 不进入写路由，读接口保持只读 | apps/mcp/tests/test_conversation_tools.py |

前端额外覆盖：OTHER 分类计数和空态；提交按钮只提交一次；pending/effective 分开；处理中超时刷新可恢复；目录/文件选择完成直接继续；无分类复核入口但上传重复卡仍存在。不得对整个页面做字符串替换破坏非分类错误提示。

LLM/embedding/OCR 单元测试使用 deterministic fake。Windows/Linux 不覆盖保证、真实 PostgreSQL 唯一约束与锁、故障恢复必须分别执行集成验证，mock 通过不能替代。

### 12.1 开发验证命令

在仓库根，按当前配置使用既有环境，命令分别运行：

```powershell
git status --short
& 'D:\anaconda\envs\myenv\python.exe' -m alembic -c apps/api/alembic.ini heads
$env:PYTHONPATH = 'apps/api'
& 'D:\anaconda\envs\myenv\python.exe' -m pytest apps/api/app/tests/test_taxonomy_matcher.py apps/api/app/tests/test_taxonomy_loader.py apps/api/app/tests/test_auto_placement_policy.py apps/api/app/tests/test_classification_feedback.py apps/api/app/tests/test_classification_organization_query.py
```

各工作包追加上表新测试文件。集成数据库、共享工作根和外部 Provider 必须使用隔离配置；禁止默认指向生产/本机真实受管原件进行破坏性测试。

最后执行后端测试、MCP 测试和前端现有脚本；若当前环境不能执行某项，提供具体环境缺口和实际执行记录，不能标记通过：

```powershell
& 'D:\anaconda\envs\myenv\python.exe' -m pytest apps/api/app/tests
$env:PYTHONPATH = 'apps/mcp;apps/api'
& 'D:\anaconda\envs\myenv\python.exe' -m pytest apps/mcp/tests
```

在 `apps/web` 工作目录核对 package.json 中现有测试命令后执行 `npm test`、`npm run build`；新增文件测试应加入现有测试运行入口。Alembic upgrade/downgrade 仅在明确隔离连接上运行，不提供可能误操作生产的通用一键重置命令。

## 13. 校准、发布及回滚

### 13.1 评测产物

实现只读评测工具 `app.scripts.evaluate_classification_policy`，参数固定为 manifest、taxonomy 快照、规则快照、配置模式和 output-dir；不得调用 placement、不得写正式关系或复制/移动原件。产物为 summary.json、逐样本结果 JSONL、混淆矩阵和版本清单，原始文本不写日志。

先冻结 19 个问题样本，补至 200 个开发问题样本，再用 600–1,000 个文件族样本校准/保留验证。按文件族和包分组隔离，不按复制件数量计分。未经人工仲裁的样本只作诊断，不能当正确标签。

发布指标必须包括：Recall@8、完整主类 ID 准确率、非 OTHER 自动业务精度、非 OTHER 覆盖率、OTHER 正确率及比例、多标签精度、格式/类别分层、样本量和区间。本版本业务精度目标 98%，不是单样本阈值；未覆盖类别使用 conservative_rules 或 OTHER，不阻塞归档。保留集零样本的节点不能宣称完成校准。

### 13.2 配置开关

| 配置 | 本次目标 |
| --- | --- |
| CLASSIFICATION_POLICY_BUNDLE_VERSION | workdata-v1，冻结 taxonomy/规则/映射哈希 |
| CLASSIFICATION_FALLBACK_CATEGORY_ID | system.other，启动校验唯一、可落位 |
| CLASSIFICATION_QUALITY_MODE | shadow / conservative_rules / calibrated；缺失校准文件不能伪用 calibrated |
| CLASSIFICATION_DIRECTORY_POLICY | CATEGORY_WITH_OPTIONAL_CONTAINER |
| CLASSIFICATION_PLACEMENT_ENABLED | 隔离试点验证全部入口后开启 |
| CLASSIFICATION_DIRECT_REQUEST_ENABLED | 新策略范围必须 true；不是给用户的每次确认开关 |
| CLASSIFICATION_RECONCILE_ENABLED | 试点开启，恢复只处理已有授权操作 |

复用已有 Settings 字段时保留兼容映射，禁止两个同义开关同时控制同一行为且优先级不明。已有 auto_primary_classification_enabled、shadow mode 等与新 bundle 的优先级：入口授权/安全校验 > bundle 模式 > 旧兼容开关；任何矛盾配置启动报错，不静默选择。

### 13.3 发布步骤

1. 备份数据库、配置快照和操作日志；核验恢复工具可用，冻结基线。
2. 在隔离根迁移并运行 D1–D6 验收，包括两平台/真实 PG 的必要检查。
3. 同时部署后端兼容字段、新 UI/MCP 与窄范围 Tool；发布 taxonomy/规则不可变 bundle，进行一个固定试点批次。
4. 验证 OTHER、明确更正、明确移动、歧义选择、同名冲突、断点恢复的真实回执；确认没有分类复核分支和二次确认。
5. 推广新入库；所有操作冻结自己开始时的 bundle。另行取得明确历史批次指令后迁移历史文件。

规则回滚：切回上一规则 bundle，只影响新建议，不删除人工主类或既有操作快照。执行功能暂停：停止接收新操作，已发生文件副作用的操作必须由兼容恢复器处理，不关闭恢复后留下文件/数据库不一致。

UI 回滚到带分类复核卡的版本不满足本轮需求；需维持 OTHER 兼容投影和直接请求响应，不通过恢复旧卡片代替后端恢复。schema 回滚前核查所有操作终态，禁止清空工作目录、重置数据库或覆盖原件。

## 14. 最终开发交付清单

- [ ] 两项用户要求在 `AGENTS.md/agent.md`、优化方案、开发文档及相关旧流程注释中一致；旧 OperationPlan 规则写明 SET_PRIMARY/MOVE 明确请求例外。
- [ ] taxonomy/规则构建可重现；新 OTHER 单一兜底；所有 fallback 退出普通召回；历史 ID 可查询。
- [ ] 当前四个关键误判及正确财务对照完成真实样本回放，保存版本与结果。
- [ ] 材料包保留正文辅助分类；版本指纹与人工主类保护生效。
- [ ] UI、聊天、反馈、MCP、首次入库、旧移动入口共用协调器；明确更正/移动无二次确认。
- [ ] OperationPlan/真实授权/ToolInvocation/ChangeSet/PathRecord 关联完整，没有虚构 Confirmation、建议或 AgentRun。
- [ ] 不覆盖、并发互斥、数据库失败与崩溃恢复在目标平台和 PostgreSQL 有验证记录。
- [ ] 分类树、分页、计数、聊天与批次回执仅 OTHER 兜底，无分类复核队列；非分类冲突/失败仍真实可见。
- [ ] 原件哈希与路径不变；纯移动不新增内容版本或 OCR；迁移没有假报历史位置已同步。
- [ ] 后端/MCP/前端检查通过，或明确列出阻止上线的未完成检查；不得把未测项勾选完成。
- [ ] API 契约、数据库 schema、runbook、当前分类树、发布/回滚文档与最终实现一致。

所有条目完成后才可把本版本标为“已交付”。代码实现发生重要选择变化，必须先同步两份方案的契约与验收项，不允许实现一套、文档保留另一套。
