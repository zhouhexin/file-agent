# WorkBuddy 图片结构化提取接入方案审查

> 用户修订：本次先新增小 Demo，尽量复用已有功能，所有登录用户共用工作副本及数据集，不新增用户授权。
> 下文是完整方案的审查，不再将全部事项作为 Demo 阻塞条件；本轮以[Demo 范围](2026-09-18-workbuddy-structured-dataset-demo.md)为准。

审查日期：2026-09-18。

审查对象：[原方案](2026-09-18-workbuddy-image-structured-extraction-integration-plan.md)，共 523 行，已逐节阅读第 1–13 节。

## 1. 结论与审查边界

**总体方向合理，能沿用当前架构，但尚不适合作为冻结后直接实施的方案。**

保留原图、由 WorkBuddy 回传结构化候选、后端校验证据、复用单图字段模型、用成员关系组成累计数据集、生成不可变 Excel 快照，这些选择成立。独立 v2 公共契约和复用内部服务的区分也合理，不需要推倒重写。

主要问题集中在实际入口、编排时序、上传与工作副本身份、事务恢复、历史 revision 和查询质量语义。第 12、13 节已经写出兼容目标，但前面的具体链路还不足以落实这些目标。“现有能力覆盖约三分之二”没有可核验的工作量依据，建议改为定性描述，不能据此估工期。

本次为静态设计与代码对照审查：读取了项目规范、相关 API/数据库文档、WorkBuddy 插件规则、MCP、外部 OCR、结构化抽取、导出、导入工作流、文件生命周期与索引实现。未修改原方案或业务代码，未运行全量测试，未调用真实 WorkBuddy/OCR，也不据此认定宿主线上能力已验证。以下区分已确认的实现冲突与尚待补全的设计。

## 2. 实施前必须解决的问题

### R1 / 兼容提醒：新增功能需要保留现有插件入口行为

- 方案位置：第 2、3、12.1、13 节，尤其原方案第 22、40、371、473 行。
- 当前依据：[插件 Skill](../integrations/workbuddy/file-agent-plugin/skills/file-agent/SKILL.md) 第 8–14 行要求 `workbuddy_submission_ingest(submission_ref)`，明确不调用旧 `workbuddy_attachment_ingest`；[MCP server](../apps/mcp/file_agent_mcp/server.py) 第 425 行的实现从可信提交单进入 `AttachmentTransferService`。
- 影响：照方案更新插件可能退回模型提供附件参数的旧入口；标准 connector 的 README 还明确说明其本身不支持自动接入会话附件，不能把 connector 和附件桥接插件当成同一种部署形态。
- 调整：目标链路改为可信宿主提交单 → `workbuddy_submission_ingest` → 内部复用 `AttachmentTransferService`。保留旧接口兼容不等于要求新插件调用旧接口。明确本功能交付到哪个插件包，并同步其 Hook、Skill、工具可见性和包测试。

修正：上述代码依据描述的是既有入口行为，不能据此认定独立新增功能本身存在冲突。用户明确要求增量扩展；只要保留现有入口并复用其传输能力，就不构成 Demo 阻塞条件。

### R2 / P1：缺少创建 v2 任务的契约，且可能再次触发现有结构化 Provider

- 方案位置：第 4.1、9、12.2、13 节；第 71 行要求 Planner 冻结配置，第 473 行称由“后续 v2 编排契约”携带，但没有定义该契约。
- 当前依据：[外部提取服务](../apps/api/app/modules/external_extraction/service.py) 第 54 行只按 ingest item 创建 `workbuddy-ocr-v1`；[导入工作流](../apps/api/app/modules/ingestion/workflow.py) 第 79 行在整理完成后调度 `batch.user_request`；[附带请求执行器](../apps/api/app/modules/ingestion/request_runner.py) 会把同一请求送入现有聊天 Agent；[Planner](../apps/api/app/modules/agent/planner.py) 第 298 行已有图片结构化抽取计划。
- 影响：尚无接口把用户授权、目标 dataset、schema 和来源清单原子绑定到 v2 task。如果用批次原始 `user_request` 触发 v2，整理结束后旧附带请求又可能执行一次本地/云端结构化抽取。仅让外部适配器避开 `execute_run` 不足以消除这条第二入口。
- 调整：定义独立的 v2 创建/编排请求及响应，固定可信提交事件、条目、用户、dataset、schema 指纹、输出和请求幂等键；说明它在默认 OCR 创建之前还是之后生效，如何处理已经被 v1 claim 的任务。为已消费的结构化用户意图保存执行映射，避免旧附带请求重复执行；复合请求剩余的总结等部分仍按原授权执行。

需要补上 AgentRun、ToolInvocation 与批次附带请求的归属，而不只是笼统要求“增加审计”。

### R3 / P1：暂存文档、最终工作副本和字段证据之间的映射缺失

- 方案位置：第 3、5、6、7 节；先保存字段和追加数据集，随后才归档和发布工作副本。
- 当前依据：[生命周期服务](../apps/api/app/modules/file_lifecycle/service.py) 第 1943 行 `_clone_upload_extraction_for_working_copy` 只查找 `extractor=workbuddy-external-ocr`，为新 DocumentVersion 克隆页面；该方法没有克隆 `DocumentElement` 或结构化字段。其他源侧物化分支虽会克隆元素，也会生成新的元素 ID。[共享身份解析](../apps/api/app/modules/file_lifecycle/shared_access.py) 明确区分上传暂存、共享工作副本与不可变原件身份。
- 影响：dataset member 绑定的上传 Document ID 不等于 `file_search` 返回的最终 Document ID；更换 extractor 标识还可能使既有 OCR 复用失效。直接挂接新的元素 ID 会破坏字段证据引用。上传取消、选择已有文件或工作副本版本变化后的证据可读性也未定义。
- 调整：保留不可变源版本和源证据 ID，另存或可追踪解析到最终工作副本身份；不得把字段事实强行改挂另一内容版本。列出新建工作副本、同哈希复用、近似重复选择已有文件、取消上传的映射与可见性规则；明确源证据保留、授权下载及删除/清理保护。补充需要修改的生命周期和访问控制模块。

### R4 / P1：直接复用两个持久化入口会产生不同的正文运行

- 方案位置：第 5 节第 135 行和第 13 节复用表。
- 当前依据：[外部 OCR submit](../apps/api/app/modules/external_extraction/service.py) 第 231–288 行创建并完成一个 `DocumentExtractionRun`，使用 `pages.text`，合并 PDF 原生页，并保留成功重试页；[create_layout_extraction](../apps/api/app/modules/structured_extraction/repository.py) 第 140 行每次另建一个运行，并用元素文本拼接 `DocumentPage.text_content`。
- 影响：外部 submit 与 layout repository 先后调用会形成两份不同的解析正文。blocks 不完整时，后者丢失正文；混合 PDF 和部分页重试也不能只通过 `LayoutParseResult` 的简单转换保留当前行为。相反，若完全替换 submit，则其近似查重、归档续跑和租约终态行为需要明确迁移。
- 调整：冻结一个 v2 页面持久化协调流程，明确哪个运行是完整原文事实源，以及 `task.extraction_run_id`、`item.extraction_run_id`、`structured_run.layout_extraction_run_id` 如何关联。允许向已确定的运行追加受控版面元素，不应为表面复用而重复生成正文。v1 保留原语义。

### R5 / P1：submit、dataset append、导出及查重之间的事务边界不明确

- 方案位置：第 3、5、8、13 节。
- 当前依据：[外部 OCR submit](../apps/api/app/modules/external_extraction/service.py) 第 289–351 行清除租约、记录终态、执行近似查重、创建归档任务并最终 `commit`；[结构化服务](../apps/api/app/modules/structured_extraction/service.py) 第 443–466 行在完成运行之后同步生成导出，再记 ChangeSet。
- 影响：在现有 submit 提交之后再追加 dataset，进程中断可能留下“任务已完成但字段/成员未完成”；重试命中旧结果后可能不再执行追加。把 dataset 冲突直接当作 OCR 整体失败则会违背“保留成功 OCR”的要求。导出顺序也不能继续沿用同步 finalizer 并声称所有失败已独立。
- 调整：明确数据库原子提交范围，例如单图事实、可恢复的追加状态、审计和队列任务在同一事务持久化；XLSX 文件生成及发布独立执行。允许 OCR 完成而 dataset 待解决，恢复入口不得依赖已经过期的 OCR 租约。说明追加是在重复文件决定之前还是之后；取消导入是否取消尚未追加的数据任务，需要固定规则。

幂等还应补充：

1. 当前成员唯一键只能防止**同一个 run** 的重复记录，不能防止重试新建 run 后重复累计；固定逻辑提交到结构化 run 的稳定映射。
2. `IntegrationRequest` 当前唯一键是 `user_id + client_id + idempotency_key`，不包含 operation；跨 operation 的键应命名空间化，不能假设增加 operation 字段就隔离了冲突。
3. revision 更新使用行锁或 CAS，并与成员写入及导出任务创建同事务提交。
4. 固定锁顺序、失败恢复和“提交已成功但响应丢失”的重放语义。

### R6 / P1：现有数据模型不足以兑现历史 revision 查询和导出重建

- 方案位置：第 6.1–6.3、7 节。
- 问题：dataset 只有当前 `schema_json/schema_version`；member 只有 `appended_revision` 和可变 `status`。没有保存旧 revision 对应的 schema、字段别名映射和成员质量状态。允许后续 schema 升级，却承诺按任意旧 revision 查询/生成快照，二者尚未闭合。
- 影响：revision 1 的 Excel 重试可能使用 revision 2 的列定义；同义字段 key 的映射变化会改变历史查询；待复核状态变化可能让同一 revision 的统计前后不同。
- 调整：增加不可变 revision 快照或等价的版本事件，固定 schema、别名映射和成员纳入规则。每次 schema/正式成员状态变化都推进 revision。导出必须固定请求的 revision，按 `appended_revision <= requested_revision` 及该 revision 的有效状态投影。补充 `(dataset_id, dataset_revision, export_type)` 等导出唯一约束；latest 按已完成 revision 排序，不能按异步任务完成先后选择。

缓存隔离还需把 v2 契约、schema 指纹、Provider/模型身份、提取配置和任务代次纳入运行复用规则，不能只换公共 Tool 名称。

### R7 / P1：待复核数据的查询、汇总和导出标准不一致

- 方案位置：第 5、6.2、7 节；第 215 行禁止待复核记录污染正式导出，但未明确查询和聚合是否排除它们。
- 当前依据：[结构化导出](../apps/api/app/modules/structured_extraction/export.py) 第 120 行 `_tabular_rows` 对传入的每条记录直接输出值，不筛选状态；[归一化](../apps/api/app/modules/structured_extraction/normalization.py) 将 decimal 保存为字符串、money 保存为 `{amount, currency}`；现有结果投影会对电话和证件号脱敏。
- 影响：直接复用表格投影会导出待复核数值；按 JSON 文本排序可能把 `100` 排在 `20` 前；查询合计和 Excel 行数可能不同。复用“安全 writer”并不代表它已经提供数据质量筛选。
- 调整：统一查询、汇总和正式导出的质量准入规则，明确按记录还是字段排除，分别返回总记录数、纳入数、排除数及原因。数值必须按类型转换，money 明确币种和缺失币种的策略，禁止跨币种直接求和。定义多值字段、空值、大小写与日期比较语义。
- 补充查询 schema：aggregation/目标字段、稳定排序次序、游标或 offset、实际 `resolved_dataset_revision`；聚合应作用于筛选后的完整合格集合，而非 `limit=200` 的返回页。脱敏与完整值导出也要明确沿用还是调整现有规则。

### R8 / P1：把外部识图结果转换为“可信字段”的证据契约仍不充分

- 方案位置：第 4.3、5 节。
- 当前依据：[现有插件规则](../integrations/workbuddy/file-agent-plugin/skills/file-agent/SKILL.md) 第 71–87 行允许 OCR 没有 blocks、confidence、Provider 版本，并禁止模型补写 OCR；[证据校验](../apps/api/app/modules/structured_extraction/evidence.py) 只验证归属和文本支持，不校验页面尺寸；当前文本匹配还接受双向包含。[字段归一化](../apps/api/app/modules/structured_extraction/normalization.py) 在 raw_text 缺失时可使用 candidate value，而当前证据检查主要由 raw_text 触发。
- 影响：不能假定“复用校验”已经实现方案承诺的完整外部输入校验；无 raw_text 的非空 value、包含额外内容的 raw_text、缺失真实坐标或模型自报高置信度，都需要更严格的适配规则。
- 调整：定义 v2 严格 block schema、页内 block ID 到全局唯一 element index/UUID 的映射、真实页面尺寸、像素/归一化坐标单位、旋转和渲染比例。`DocumentElement` 当前要求 `(extraction_run_id, element_index)` 唯一，多页不能直接复用各自从 0 开始的 block index。
- 所有非空正式字段均须有可验证原文或明确的可追踪派生规则；原文应为证据的受支持子串，不能反向扩大。OCR 置信度和模型字段映射置信度分开保存，缺失就留空或降级，不补造。缺少坐标时允许文本定位或进入待复核，具体策略需冻结。

这不要求重新写 OCR 算法，只要求在复用内部 finalizer 前补足不可信客户端输入校验。真实宿主是否稳定返回表格行列和坐标仍需试点验证。

### R9 / P2：PDF 范围扩大与现有固定页策略不兼容

- 方案位置：第 12.2 节第 383 行允许图片/PDF，但其余设计主要按“本张图片”描述。
- 当前依据：[外部页面清单](../apps/api/app/modules/external_extraction/service.py) 第 461 行只渲染没有原生文字的 PDF 页；PDF 所有页都有文字时不创建 OCR 任务。PARTIAL 重试也只 claim 失败页。
- 影响：文字型 PDF 无法进入方案的结构化任务；混合 PDF 只能领取部分页，跨页表格会不完整；“完整页集合”不能同时指原 PDF 全部页和本轮 claim 页。
- 调整：首期明确只支持单页图片，或为 v2 单独冻结结构化页面范围及跨页记录策略，同时保留 v1 不外发原生文字页的行为。增加纯文字 PDF、混合 PDF、跨页表格和失败页重试测试。多页 TIFF 也应明确是否在首期范围内。

### R10 / P2：数据集发现、权限和下载接口还没有闭环

- 方案位置：第 7、9、12.5、13 节。
- 问题：列出了 create/select MCP，但 API 只有 get/query/latest；没有 schema 升级确认、选择持久化、重启后按条件列出数据集或从文件解析 dataset membership 的接口。`file_search` 当前发现的是文件，不会自然返回新增的 dataset ID。
- 当前依据：[共享读取策略](../apps/api/app/modules/file_lifecycle/shared_access.py) 允许登录用户读取共享 ACTIVE 文件，但上传来源与个人上下文隔离；[integration router](../apps/api/app/modules/integrations/router.py) 对全部已有路由使用 `INTEGRATION_INGEST_ENABLED` 门禁。
- 调整：明确数据集首期是个人私有还是共享；个人私有是可行扩展，但不能把文件共享权限直接等同于 dataset 或上传原图权限。补充受控 list/create/select/schema-upgrade/export-download 契约及归属校验，说明历史源证据读取规则。
- 第 12.5 节“关闭新开关仍可读历史数据”可以成立，但不能自动推导出关闭现有 integration 总开关后仍可读。明确门禁矩阵，避免为了新需求改变旧 router 的关闭语义。latest 接口还应说明返回元数据还是二进制，以及固定 export ID 的下载路径。

### R11 / P2：结构化字段进入原文索引的方式未定义，可能改变证据语义

- 方案位置：第 9、13 节第 311、483 行。
- 当前依据：[DocumentIndexService](../apps/api/app/modules/chunks/service.py) 第 79 行按文档版本、解析运行与配置指纹建立索引；成功运行直接复用；第 325 行从页面/元素构建 Chunk。当前没有读取 `StructuredExtractionField` 的索引来源。
- 影响：字段后来追加不会自动进入已完成索引。把归一化值或新字段标签塞回原文页面，又会将派生解释伪装为原文，甚至因元素覆盖率改变 Chunk 来源。
- 调整：首期可只索引完整 OCR 原文，字段精准查询走 dataset query。若必须索引字段投影，明确派生来源类型、结构化 run/config 指纹和证据反向关联，并生成新的派生索引版本；不得覆盖历史 Chunk/Evidence 或改写原文。

## 3. 文档内部需要统一的地方

| 位置 | 当前不一致或遗漏 | 建议 |
| --- | --- | --- |
| 第 1 节 vs 第 12.2 节 | “提取文字或表格”与“明确要求累计表格”范围不同 | 统一首期为显式累计数据集；单纯 OCR/单图表格沿用旧路径 |
| 第 4.3 节 vs 第 12.5 节 | submit 必须有 dataset，但灰度第二步要求仅持久化单图、不追加正式 dataset | 定义服务端冻结的 `VALIDATE_ONLY` 等非正式模式，或者调整灰度步骤；不能靠漏传字段实现 |
| 第 4.2–4.3 节 | submit 要使用 claim 时看到的 dataset revision，但 claim 示例没有 dataset/schema 版本 | 补完整示例及严格 schema；版本检查要基于服务器冻结配置 |
| 第 4.1、6.3 节 | presentation 支持 TABLE/CSV/XLSX，但流程默认总生成 XLSX | 明确输出选择和自动导出开关优先级 |
| 第 8 节 | 混用 IngestItem、外部 task、结构化 run 和 export 的状态 | 保留各领域现有状态；新阶段放到明确的编排进度容器，避免 v1 消费者误读 |
| 第 12.6 节 | `cd apps/api` 后再 `cd apps/mcp`、`cd apps/web` 不能顺序执行 | 使用从仓库根开始的独立命令，或 `(cd apps/api && pytest -v)` 等子 shell |
| 第 12.7 节 | “关闭后与改造前一致”与保留历史 dataset 读取存在字面矛盾 | 改为“旧功能行为不变，新写入停止，历史读取按约定保留” |
| 第 9、10 节 | 缺少运行文档、配置示例及完整故障恢复验收 | 同步 README、runbook、API/schema 文档、插件包测试；增加进程崩溃、乱序导出、重复确认、历史 revision 回放测试 |

自动累计 XLSX 是系统生成的派生数据快照，可以设计为用户显式任务或配置授权覆盖的操作，不必机械增加第二次确认；但方案应将该授权范围写清楚。它不应顺带扩大为已有文件批量外发、覆盖用户 Excel 或未授权云端处理。

## 4. 合理且建议保留的部分

1. PostgreSQL 持久化字段事实，Excel 只作为快照，不让 WorkBuddy 维护累计 Excel。
2. 数据集成员引用单图结构化事实，避免复制另一张字段宽表。
3. dataset 目标由用户明确创建或选择，不按名称/相似字段自动合并。
4. 独立 v2 schema 和 Tool，旧 v1 保留；内部复用租约、归一化、安全写入和队列。
5. 聚合导出使用独立 `StructuredDatasetExport`，不伪造单 Document 外键。
6. 数字与日期的确定性处理、字段白名单查询、公式注入防护、原件保护和逐文件回执。
7. 导出失败不撤销已接受的数据，功能开关默认关闭，使用 deterministic fake 验证。

## 5. 修订优先顺序

先修 R1–R5，固定真实入口、任务创建、身份映射、唯一正文来源及事务恢复；再修 R6–R8，冻结 revision、查询质量和外部证据契约。随后明确首期 PDF 范围、数据集权限/发现与索引策略，最后补完整接口和验收矩阵。

建议先完成“单页图片 + 明确 dataset/schema + 完整 OCR + 追加 + 版本化导出 + 查询”的闭环。AUTO_DISCOVER、schema 升级、跨页 PDF 可以保持在总体设计中，但应在上述契约确定之后按独立可验证增量实现。
