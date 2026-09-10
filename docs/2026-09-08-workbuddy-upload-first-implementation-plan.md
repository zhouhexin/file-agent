# WorkBuddy → MCP Server → File Agent 改造实施方案：本地目录与会话附件上传

## 0. 版本依据与使用方式

- 编写日期：2026-09-08。
- 仓库：`https://github.com/zhouhexin/file-agent.git`。
- 本次已执行远端同步，方案基于`origin/main`提交`f8d61eee507eaec63905d0935444b3c084265d9e`，提交说明为`feat: improve classification naming and deployment workflow`。
- 对比此前查看的`45d814b`，方案阶段更新涉及75个文件；实施阶段已继续核对上传、查重、解析、分类、命名、操作执行、模型、队列和相关测试。
- 本文同时作为实施依据和进度记录。第10节 P0–P8 已落地并完成首轮真实试点；本次试点使用项目 `docs/` 下 26 份材料及隔离归档/工作副本目录，验证结果见第10.1节。由于项目样本以 Markdown 和图片为主，文本/扫描/混合 PDF、Word、Excel 的格式覆盖继续由确定性自动化测试保护；扩大到真实业务材料目录仍属于上线前业务验收，不得把当前样本覆盖描述成全部格式实物验收。

### 当前实施进度

- 2026-09-08：P0契约和现有代码基线已核对；Alembic新迁移顺接唯一head `20260901_0001`。
- 2026-09-08：P1已实现`ingest_batches`、`ingest_items`、`integration_requests`模型与迁移，以及批次创建、清单分页追加、seal、查询、游标分页、用户隔离和幂等冲突API。
- 2026-09-08：P2已抽取不提交事务的`stage_upload`核心，新增逐条内容上传、来源快照校验、原子任务绑定、幂等重传和`file_ingest`/`job_get` stdio MCP骨架；旧聊天上传仍保持“只暂存、发送后启动”。
- 2026-09-09：增加`workbuddy_attachment_ingest`会话附件适配入口；由WorkBuddy宿主提供稳定提交ID、附件ID、原文件名和缓存文件引用，MCP在预配置缓存根内校验并冻结清单后复用既有隔离、查重和自动整理链路。
- 2026-09-09：第二、三阶段首组外部能力已接入：`file_search`固定走只读搜索API，`file_read`只允许固定读取模式，`evidence_answer`只提交问题和文件范围；`file_rename`要求稳定Document ID及完整before/after文件名并复用现有受控重命名链路。搜索澄清和OperationPlan均提供后端事实恢复/确认工具。
- 2026-09-08：P3已实现授权逻辑根目录枚举、清单哈希、分页登记、seal、并发传输、本地断点状态、后端`resume`检查以及批次文件数/字节/用户容量限制；失败项不由自动恢复重置。
- 2026-09-08：P4已增加版本化review、候选快照、工作副本revision、批内完整哈希组、`WAIT_AND_REUSE`、结构化重复读取/决定、幂等冲突、过期及混合批次聚合；`batch_get`可在刷新后恢复全部待确认项。
- 2026-09-08：P5已实现外部OCR任务、真实页资源、领取/续租、固定页集合回写、混合PDF原生页保护、幂等提交、近似查重续跑和页级部分失败覆盖信息；外部模式不会回退内部OCR。
- 2026-09-08：P6已把批次冻结的`AUTO_ORGANIZE/BY_CATEGORY`策略贯穿归档、工作副本和分析任务，完成首次分类、标准化命名、分类落位与索引，并记录授权来源、请求ID和工作副本前后revision；命名依据不足保持原名和`NO_CHANGE`。外部OCR正文会复制到正式版本后继续执行整理，不再因“已有解析”跳过分类/命名；显式`legacy_school_materials`规则通过只读`SourceProvenance`消费固定清单来源，默认`content_based`不使用目录信号。
- 2026-09-08：P7已实现固定已完成文件集合的增量附带请求、AgentRun映射和可恢复批次回执；分类/整理文字复用默认流程，读取/总结在整理后自动执行，重复待确认不阻塞其他已完成文件。
- 2026-09-08：P8已补充逐项显式重试/取消、已发布文件保护、附带请求独立恢复、OCR失败页重试、中间资源期限清理、默认关闭门禁、worker运行说明和文件域重置清单；“逐份总结”按单文件固定执行，“汇总”按本轮完成集合执行。
- 2026-09-09：首批真实试点批次 `931a898c-2e8a-4bb2-be5b-63cc1369fbfc` 已完成。固定清单 26 项，23 个新文件和 3 个明确选择“使用已有文件”的重复项全部收敛为成功；最终状态 `SUCCEEDED`、展示状态 `FILE_PROCESSING_COMPLETED`，源文件哈希、固定成员、最终 Document/Version/WorkingCopy 映射、整理、索引和命名回执由只读试点报告验证，报告 `ok=true` 且各错误集合为空。传输恢复、刷新后重复选择恢复和 worker 中断恢复均已实际执行。
- 2026-09-09：外部 OCR 另用单图片批次验证真实 HTTP `claim -> page -> results`。本机 Tesseract 仅有英文语言包，对中文图未识别出正文，因此按真实结果提交页级 `LANGUAGE_PACK_UNAVAILABLE`，任务和批次分别正确收敛为 `PARTIAL`，失败页 `[1]`，工作副本仍为 `ACTIVE` 且保留原名，没有伪造文本或把部分结果显示成成功。另复用一个未发布图片条目验证 OCR worker 中断：租约过期后第二个 worker 可重新领取，旧 token 访问页资源返回 `409 LEASE_EXPIRED`，验证完成后该条目显式取消且未继续归档。
- 当前自动化验证：后端全量`1208 passed, 19 skipped`；MCP目录传输、会话附件适配、对话搜索、固定模式读取、证据回答、明确重命名、OperationPlan恢复/确认、整次工具重放、结构化确认、规则策略、OCR、动作、只读试点报告与真实服务注册共`30 passed`；前端`26 passed`且生产构建成功。新增迁移从既有 head 到新 head 的 PostgreSQL offline 升级与降级 SQL 编译通过，现有 PostgreSQL 已从`20260901_0001`真实升级到`20260908_0005`；隔离验证使用4个并发幂等批次请求和2个并发重复组写入者，分别只产生一个批次和一个活动组，临时身份随后精确清理。混合PDF自动化测试确认只外发缺字页并保留原生文字层。

**第一阶段交付目标：** 用户在WorkBuddy中指定本地目录，或在消息中明确提交宿主已经落入受控缓存的附件，经MCP批量复制导入；File Agent完成查重、必要确认、解析与外部OCR协作、分类、重命名、索引，并返回逐文件结果。中断后可继续，已有源文件和WorkBuddy缓存文件不被移动或覆盖。

最新用户要求优先于旧架构文档：检索、总结、分类判断、名称生成及实际操作均留在File Agent；WorkBuddy传递请求、展示结果、转交必要用户选择，并按外部任务提供OCR。不再将内部分类器、问答生成器或统计计划生成器迁往WorkBuddy。

## 1. 范围和固定业务规则

### 1.1 第一阶段必须完成

1. 本地文件与目录的明确指令导入；首批材料必须先指定处理目录，并支持用户指定是否递归。用户确认目录并调用新导入接口后，不再要求额外输入聊天文字。
2. 单文件上传和目录批量导入共用一个后端处理流水线。
3. 每个已提交文件默认分类、命名和建索引，`user_request=null`也生效；这里的`null`只表示没有额外总结、读取或明确修改要求，不表示可以在用户没有指定处理目录和发起导入时自行扫描文件。
4. 原文件字节SHA-256查重、同名检查、解析/OCR后的正文相似检查。
5. 发现活动范围内的精确重复、同名异内容、近似内容或处理中重复时，展示对比并等待选择。
6. 重复确认之后自动整理；普通整理不再二次确认。
7. 用户指定名称或分类时合并为本次要求；已有人工更正默认保留。
8. 外部OCR任务领取、页图传输、结果回写、失败与租约恢复。
9. 批次状态、分页明细、断点恢复、逐项重试及真实结果回执；非重复文件立即继续整理，重复文件的等待不阻塞同批其他文件。
10. 上传附带的内容请求保留并由File Agent继续处理；总结请求在目标文件完成分类和实际命名后自动执行，普通“分类”请求复用系统默认分类流程而不再启动第二次分类。不能完成的请求明确返回状态，不能在导入后悄悄丢弃。
11. 当本批次已无可自动继续的运行项时，即使仍有重复文件等待确认，也显示“文件处理完成”并生成覆盖全部清单项的最终回执；待确认文件作为后续可继续事项展示，不得把回执写成全部成功。

### 1.2 分期边界

| 阶段   | 交付内容                                           |
| ---- | ---------------------------------------------- |
| 第一阶段 | 上述本地目录与WorkBuddy会话附件批量导入闭环；包含默认自动命名所需执行器和上传附带请求的续跑能力 |
| 第二阶段 | 独立只读`file_search`、`file_read`、`evidence_answer`与搜索澄清入口；统计继续按受控确定性能力逐项开放 |
| 第三阶段 | 已接入明确`file_rename`和OperationPlan恢复/确认；移动、回收、恢复等其他外部动作后续按独立白名单工具开放 |

首批本地材料由用户在WorkBuddy中指定处理目录并明确发起导入。`file_batch_ingest`调用本身就是提交授权，调用后不再要求用户补一条聊天文字。WorkBuddy会话附件由宿主在用户提交消息后调用`workbuddy_attachment_ingest`；宿主必须提供本轮稳定`submission_id`、每项稳定`attachment_id`、原文件名和真实缓存文件路径，该调用本身构成固定附件清单的提交授权，`user_request=null`仍自动整理。MCP仅允许读取`FILE_AGENT_WORKBUDDY_ATTACHMENT_ROOTS`配置的缓存根，拒绝根外路径、软链接、目录和特殊文件；后端只接收逻辑来源及文件字节，不接收宿主绝对路径。仅选择但尚未提交的附件不得触发工具，普通File Agent聊天页仍保持“附件必须带任务文字”的边界。

### 1.3 对新增MCP导入通道采用的默认值

- 导入方式：`COPY`，本地源文件保留，源目录不配置为后端归档可写目录。
- 默认整理：`AUTO_ORGANIZE`。
- 物理位置：默认`BY_CATEGORY`，按最终主分类自动落位；逻辑分类仍与文件动作分别记录，主分类不明确时不得猜测路径，应使用中性落位或进入待复核。用户明确要求保留材料包路径时使用对应的版本化落位策略。
- 自动名称冲突：标准名占用时为新文件追加由该导入项稳定ID生成的短后缀；短后缀冲突时确定性加长。不同逻辑副本不能只用内容哈希产生相同后缀。
- 明确指定的精确名称冲突：返回`TARGET_NAME_CONFLICT`，不覆盖、不偷偷改成其他名称。
- 命名依据不足：直接保留原名并记录`NO_CHANGE`，只要其他必需处理成功，就仍算成功，不记为失败或部分失败。分类证据不足进入中性分类或`NEEDS_REVIEW`；OCR/正文提取确实不完整时才按覆盖范围决定是否记为`PARTIAL`。
- 仅回收站中有重复：新建活动文件，不自动恢复；仍检查其他活动候选。
- 普通查找不含回收站。查重范围按后端授权，不泄露用户不可访问的文件。
- 选择“使用已有文件”时，只替换本次上传的最终文件引用，不对已有文件重新执行自动命名或自动分类，不覆盖人工名称和人工分类。总结、读取等只读附带请求可以继续；只有用户明确要求修改该已有文件时，才进入相应文件操作链路。

## 2. 最新代码中可复用的能力与实际缺口

以下路径均相对仓库根目录。

| 现有代码                                                                                         | 最新实现事实                                  | 本次具体处理                                                  |
| -------------------------------------------------------------------------------------------- | --------------------------------------- | ------------------------------------------------------- |
| `apps/api/app/modules/files/service.py`：`FileUploadService.upload`                           | 流式暂存、计算哈希、创建Document和上传版本；当前不自动启动处理     | 提取可组合暂存方法，供新提交入口调用；保留旧UI“选择附件未发送”的暂存语义                  |
| `file_lifecycle/service.py`：`UploadLifecycleService.start_processing`                        | 已有按上传版本启动处理的幂等入口                        | 新MCP提交事务自动安排续跑，不再依赖发送聊天消息                               |
| 同文件：`_check_upload_duplicates`、`decide`                                                      | 已有精确/同名候选、确认、复用/另存/取消                   | 增加阶段查重、候选快照修订、同批等待组、选择一致性及最终ID映射                        |
| 同文件：`_append_near_duplicate_candidates`                                                      | 当前针对可读小文本做token Jaccard，候选查询有数量限制       | 将入口改为已提取正文；候选召回与差异定位单列，明确覆盖度，不能将现状描述为扫描件全文相似查重          |
| `files/extractors.py`                                                                        | 原生解析与`build_default_ocr_service()`调用耦合  | 增加原生解析模式，缺页生成外部任务，不在外部模式隐式调用内部OCR                       |
| `structured_extraction/worker.py`                                                            | 处理结果与恢复原AgentRun相关联                     | 新MCP任务恢复导入流水线；旧会话任务继续走旧恢复路径，避免双重续跑                      |
| `file_lifecycle/organizer.py`：`InitialWorkingCopyOrganizer`                                  | 已能调用命名建议与分类；部分路径仍固定原名，输出与执行语义需梳理        | 复用决策能力，拆开“建议READY”和“实际改名成功”；支持既有解析结果与用户约束               |
| `file_lifecycle/service.py`：`_finalize_initial_organization`                                 | 已有ORGANIZING工作副本首次发布、Shadow及多项开关控制的自动落位 | 复用隐藏副本和发布机制；将逻辑分类、命名、物理目录策略拆开，不能简单开启旧开关就视为完成目标          |
| `classification/classifier_service.py`                                                       | 最新版本v14，增加全文优先和职称材料包规则                  | 保留新规则，传入可核验的来源上下文，不回退到旧分类器                              |
| `file_rename/uploaded_suggestion_service.py`、`resume_naming.py`、`trial_evaluation_naming.py` | 最新有职称表单保留原名、应聘试讲命名等规则                   | 保留业务规则；自动整理允许`NO_CHANGE`，明确用户指令仍按既定优先级处理                |
| `file_rename/collision_naming.py`                                                            | 新增可读消歧候选，使用原名、来源目录等信息                   | 保留旧通道行为；新增通道默认按本方案短ID冲突规则，若将可读消歧设为可选策略须版本化，不静默替换用户已确定策略 |
| `file_rename/uploaded_review_service.py`                                                     | 最新明确改名已有直接执行路径，内部仍记录OperationPlan及确认来源  | 复用校验和底层执行能力；不重新实现字符串匹配入口，也不把自动整理伪装成用户确认                 |
| `managed_files/source_path_policy.py`                                                        | 新增针对特定材料目录的过滤/包内路径规则，上传归档默认不保留来源包路径     | 新导入必须单独保存来源相对路径和规则集，不能用`uploads/...`替代来源上下文             |
| `db/models.py`：`FilesystemJob`                                                               | 已有幂等键、租约、execution_token、进度和事件          | 复用队列，不另建消息中间件；补导入聚合记录和外部任务元数据                           |
| `db/models.py`：`WorkingCopy`                                                                 | 有当前内容版本和哈希，目前没有本方案的操作修订号字段              | 增加`revision`，覆盖所有会修改工作副本状态、名称、路径的入口                     |
| `agent/mcp_filesystem_bridge.py`                                                             | 是向外调用MCP的客户端，非对外MCP服务                   | 单独新增MCP适配器，不改其角色                                        |
| `evidence_answer/service.py`                                                                 | 已有证据召回、生成和校验，但带会话依赖                     | 保留后端能力，建立外部请求到后端会话/任务的明确映射                              |

表中省略`apps/api/app/modules/`前缀的模块路径均位于该目录。

**最新代码带来的两个实施约束：**

1. 图片已有“学院/上传年份”的专用策略；它不能在新通道中悄悄覆盖默认正文分类。建议将它保留为显式规则集`legacy_school_materials`的一部分；新通道默认`content_based`，照片/材料包业务选择对应规则集后生效。元数据年份不是识别出的正文年份。
2. 旧显式命名冲突会给出“覆盖已有文件”的选项；新增通道只返回本方案允许的行为，不照搬这些覆盖选项。后端共享执行器可以复用，外部授权策略必须单独控制。

## 3. 目标模块结构

### 3.1 建议新增文件

| 新增路径                                                                    | 职责                               |
| ----------------------------------------------------------------------- | -------------------------------- |
| `apps/mcp/file_agent_mcp/server.py`                                     | 注册stdio MCP工具，不导入ORM或直接修改受管文件    |
| `apps/mcp/file_agent_mcp/client.py`                                     | 后端HTTPS客户端、认证、超时、请求关联及错误映射       |
| `apps/mcp/file_agent_mcp/local_import.py`                               | 校验授权目录、枚举固定清单、受控文件打开与流式传输        |
| `apps/mcp/file_agent_mcp/transfer_state.py`                             | 仅保存本地传输清单、待传项和续传位置；不拥有分类/查重等业务事实 |
| `apps/mcp/file_agent_mcp/schemas.py`                                    | MCP参数与返回模型；依赖文件另行固定经验证的SDK版本     |
| `apps/api/app/modules/integrations/router.py`、`schemas.py`、`context.py` | 外部业务API、Schema、当前用户与请求/会话映射      |
| `apps/api/app/modules/ingestion/service.py`、`repository.py`             | 批次、条目和最终目标绑定                     |
| `apps/api/app/modules/ingestion/workflow.py`                            | 导入状态推进和续跑，唯一决定下一处理阶段             |
| `apps/api/app/modules/ingestion/duplicate_service.py`                   | 两阶段查重、重复组、版本化比较及决定校验             |
| `apps/api/app/modules/ingestion/organization_service.py`                | 调用现有分类/命名服务，执行用户约束和自动整理政策        |
| `apps/api/app/modules/ingestion/receipt.py`                             | 后端生成批次、逐文件和最终展示结果                |
| `apps/api/app/modules/ingestion/request_runner.py`                      | 上传附带请求绑定最终ID后在File Agent中续跑      |
| `apps/api/app/modules/external_extraction/service.py`、`schemas.py`      | 外部OCR准备、领取、续租、提交、验收              |
| `apps/api/app/modules/file_lifecycle/working_copy_executor.py`          | 从现有执行器提取共享的受控校验和实际动作，支持自动整理授权来源  |

这是职责拆分清单，允许把紧密关联的小型Schema合并；不要为每个表再建一层重复框架。

### 3.2 调用边界

- WorkBuddy只调用MCP；MCP只访问集成API和受控本地传输目录。
- 集成API使用`get_current_user`等现有认证依赖，生成可信`RequestContext`。
- `workflow.py`调度现有生命周期、解析、分类、命名和索引服务，不调外部WorkBuddy去作业务决策。
- File Agent以数据库事务记录状态和下一阶段job；不得只在内存回调里保存续跑位置。
- 首期采用现有授权工作区语义，`project_key`仅作请求来源标签。需要新项目数据隔离时必须增加后端范围授权，不能把WorkBuddy项目配置等同于数据库隔离。

## 4. 数据库改动

采用增量Alembic迁移，保留现有Document、DocumentVersion、ManagedFile、WorkingCopy、ChangeSet和FilesystemJob。不要删除原表或重导历史数据。

### 4.1 新增核心表

| 表名（建议） | 关键字段和约束 |
| --- | --- |
| `ingest_batches` | `id,user_id,workspace_id,client_id,request_id,idempotency_key,request_fingerprint,manifest_status,manifest_revision,result_revision bigint not null default 1,policy_json,policy_version,user_request,conversation_id,status,created_at,updated_at`；唯一`user_id+client_id+idempotency_key`；等待确认项后续恢复时递增`result_revision`，不覆盖此前已经展示的回执版本 |
| `ingest_items` | `id,batch_id,client_item_id,source_root_ref,source_relative_path,original_filename,expected_size,expected_sha256,actual_sha256,upload_document_version_id,archive_record_id,workflow_revision,stage,status,decision,final_document_id,final_version_id,final_working_copy_id,extraction_run_id,current_job_id,error_json,result_json`；唯一`batch_id+client_item_id`；`id`作为外部`upload_id` |
| `external_extraction_tasks` | `id,ingest_item_id,source_version_id,source_sha256,phase,provider_contract_version,page_manifest_json,status,lease_owner,lease_token_hash,lease_expires_at,attempt_count,last_submission_key,last_submission_digest,extraction_run_id,error_json`；同一源版本/缺页集合/配置的活动任务去重 |
| `external_extraction_pages` | `task_id,page_number,status,result_digest,result_json,error_json`；唯一`task_id+page_number`，仅接受有效租约下的提交 |
| `ingest_duplicate_groups` | `id,batch_id,user_id,workspace_id,content_sha256,revision,primary_item_id,status`；记录本批主任务；活动组以`batch_id + content_sha256`并发唯一，不跨批次共享等待关系 |
| `ingest_duplicate_group_members` | `group_id,ingest_item_id,joined_revision,decision,waits_for_item_id`；成员唯一；决定绑定固定成员集及组修订 |
| `integration_requests` | `id,user_id,client_id,request_id,operation,idempotency_key,payload_digest,target_refs_json,status,result_json,created_at`；统一保存确认、重试和写操作幂等结果，同键不同payload拒绝 |

`ingest_items`保存业务进度，`FilesystemJob`保存一次阶段执行；不能用“某个子job完成”代表整个文件导入完成。实际失败、部分完成、取消、等待确认和确认过期等回执都从业务记录聚合。批次生成最终回执后仍允许待确认条目通过新修订继续，但不得重跑或改写其他已经完成的条目。

### 4.2 扩展现有表

| 表/模型                                    | 扩展内容                                                                                                                    |
| --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `WorkingCopy`                           | `revision bigint not null default 1`；所有名称、路径、内容和状态修改统一递增，单纯查询不递增                                                        |
| `UploadDuplicateReview`                 | `revision`、`comparison_phase`、`selected_candidate_id`、`ingest_item_id`、`decision_scope_json`；沿用现有期限和用户归属                |
| `UploadDuplicateCandidate`              | `candidate_ingest_item_id`、`compared_version_id`、`compared_sha256`、`compared_working_copy_revision`；在现有证据JSON中记录对比范围/页码 |
| `DocumentExtractionRun`及页/元素记录          | 沿用现有正文存储，补能区分STAGING与PUBLISHED的标记或严格关联；暂存正文不得进入正式检索投影                                                                   |
| `DocumentOrganizationDecision`或现有组织审计结构 | 增加/映射`authorization_source`、`policy_version`、`source_request_id`、`before_revision`、`after_revision`和真实执行状态              |

### 4.3 版本和身份约定

现有上传会先创建DocumentVersion，因此无需推翻模型：暂存OCR可以使用内部`upload_document_version_id`定位字节，但它不表示文件已正式入库。对WorkBuddy返回`upload_id/job_id`；最终目标准备好后再返回`final_*`映射。

“使用已有文件”时最终ID必须指向所选已有文件；“保留两份”必须有独立逻辑文件和工作副本。原件字节或解析计算可以共享，但不能让内容哈希唯一约束禁止用户保留两份。

迁移实施要求：先检查实际Alembic head，以当前head为父节点；对新增字段做可空或有默认值的兼容迁移；旧记录不强行补造成已确认/已完成。生产迁移前备份并在数据库副本上验证，本文不执行数据库变更。

## 5. 对外工具与后端API契约

以下`/api/integrations/v1`为建议新增前缀；MCP业务JSON与底层HTTP协议分别维护Schema。

| MCP工具                  | 集成API/实现                                                               | 核心行为                                                     |
| ---------------------- | ---------------------------------------------------------------------- | -------------------------------------------------------- |
| `file_batch_ingest`    | 本地枚举后调用`POST /ingest-batches`、分页追加`/items`、`/seal`                     | 固定本次清单与用户规则，创建传输任务；尽快返回批次ID                              |
| `workbuddy_attachment_ingest` | 校验宿主附件缓存引用后复用`POST /ingest-batches`、`/items`、`/seal`和内容端点 | 固定本轮已提交附件清单；不把宿主绝对路径发送给后端，空附带请求仍自动整理 |
| `file_ingest`          | `PUT /ingest-batches/{batch_id}/items/{item_id}/content`               | 流式上传一个条目；无批次时先创建单条目批次                                    |
| `batch_get`            | `GET /ingest-batches/{id}`、`GET /ingest-batches/{id}/items?cursor=...` | 返回状态、`result_revision`、统计、分页明细、最终回执及待交互项；条目同时区分导入名称快照和当前工作副本名称/状态；刷新或重连后用后端事实恢复选择 |
| `batch_resume`         | `POST /ingest-batches/{id}/resume`及本地传输恢复                              | 只恢复原清单未完成的传输；不重置业务失败项和已完成结果                              |
| `ingest_retry`         | `POST /ingest-items/{id}/retry`                                        | 显式重试可重试失败，记录原因与新阶段执行代次                                   |
| `ingest_cancel`        | `POST /ingest-items/{id}/cancel`                                       | 取消尚未完成的本次条目及依赖任务，不回收已有复用目标                               |
| `job_get`              | `GET /jobs/{id}`                                                       | 只返回当前用户可见的任务状态和关联业务阶段                                    |
| `duplicate_review_get` | `GET /ingest-items/{id}/duplicate-review`                              | 返回对比、候选版本、修订和允许决定                                        |
| `duplicate_decide`     | `POST /ingest-items/{id}/duplicate-decision`                           | 核验review、候选、决定、组成员和幂等性后推进                                |
| `extraction_claim`     | `POST /extraction-tasks/{id}/claim`                                    | 返回租约和授权页资源；页下载用`GET /extraction-tasks/{id}/pages/{page}` |
| `extraction_renew`     | `POST /extraction-tasks/{id}/renew`                                    | 只续当前有效租约                                                 |
| `extraction_submit`    | `POST /extraction-tasks/{id}/results`                                  | 逐页或分组提交，验收完成后唤醒后端任务                                      |
| `file_search` | `POST /api/search` | 只读检索已入库文件；返回稳定Document ID、相关性说明和安全结果投影 |
| `file_read` | `POST /api/conversations/{id}/messages` | 仅允许`READ/SUMMARY/EXPLAIN`固定模式和稳定Document ID范围 |
| `evidence_answer` | `POST /api/conversations/{id}/evidence-answer` | 后端从完整原文生成并校验答案与引用，不接受客户端自造Evidence |
| `file_search_clarification_resolve` | `POST /api/file-search/clarifications/{id}/resolve` | 原样提交后端签发选项，继续原固定任务 |
| `file_rename` | `POST /api/conversations/{id}/messages` | 要求Document ID、当前文件名和目标文件名，复用受控明确重命名及审计链路 |
| `operation_plan_get` | `GET /api/operations/plans/{id}` | 刷新或重连后恢复计划真实状态、before/after和影响范围 |
| `operation_plan_confirm` | `POST /api/operations/plans/{id}/confirm` | 只转交用户明确确认，后端重新校验归属、状态、修订和执行器白名单 |

旧接口`/api/files/upload`、`/api/uploads/{version}/process`与duplicate-review接口可以保留兼容，但新MCP不要只依次调用两个旧接口就认为实现了幂等批量导入。

### 5.1 本地批次工具输入示例

```json
{
  "tool": "file_batch_ingest",
  "arguments": {
    "source_root_ref": "local-materials",
    "relative_directory": "待导入",
    "recursive": true,
    "ingest_policy": "AUTO_ORGANIZE",
    "placement_mode": "BY_CATEGORY",
    "rule_profile": "content_based",
    "user_request": null,
    "request_id": "request-20260908-001",
    "idempotency_key": "batch-submit-event-001"
  }
}
```

`local-materials`由本地受控配置映射到真实目录，不允许模型通过工具临时扩大授权根。配置中的规则集必须由后端验证，不能靠客户端填写任意策略名称绕过处理规则。

每条清单至少传递：`client_item_id`、`source_relative_path`、`original_filename`、`size_bytes`、`mtime_ns`；哈希在传输时计算。清单分页注册完成后seal，固定本批次成员范围；seal不代表尚未传输文件已完成查重。重复组依据已接收的哈希逐步建立，每次确认只覆盖明确列出的成员；随后发现的新成员按新修订处理。清单之外的文件须新批次或显式清单修订。

### 5.1.1 WorkBuddy会话附件工具输入示例

```json
{
  "tool": "workbuddy_attachment_ingest",
  "arguments": {
    "submission_id": "message-20260909-001",
    "attachments": [
      {
        "attachment_id": "attachment-001",
        "filename": "奖学金申请表.xlsx",
        "local_path": "/workbuddy/controlled-cache/message-20260909-001/奖学金申请表.xlsx"
      }
    ],
    "user_request": null,
    "placement_mode": "BY_CATEGORY",
    "rule_profile": "content_based"
  }
}
```

`local_path`只在WorkBuddy与本机MCP进程之间使用，是文件字节交接能力，不是后端来源标识。MCP必须
确认路径位于预配置缓存根内、路径本身不是软链接、对象是普通文件且basename与`filename`完全一致，
随后计算真实大小、mtime和SHA-256。后端清单只保存
`workbuddy-attachments/submitted-attachments/<attachment_id>/<filename>`形式的逻辑来源。
`submission_id`决定稳定幂等键；同一提交事件的附件集合或策略发生变化时，必须由后端返回
`IDEMPOTENCY_CONFLICT`，不能创建第二批。由于WorkBuddy缓存可能过期，MCP必须在一次调用中尽力完成
字节传输；中断后由WorkBuddy使用原提交事件和当前有效附件引用再次调用，已接收项由后端幂等复用。

### 5.2 统一返回示例

```json
{
  "batch_id": "batch-001",
  "upload_id": "item-001",
  "status": "WAITING_DUPLICATE_CONFIRMATION",
  "stage": "EXACT_CHECK",
  "job_id": "job-001",
  "review_id": "review-001",
  "review_revision": 2,
  "duplicate_group_id": null,
  "group_revision": null,
  "group_member_item_ids": [],
  "final_document_id": null,
  "final_working_copy_id": null,
  "allowed_decisions": ["USE_EXISTING_FILE", "CONTINUE_UPLOAD", "CANCEL_UPLOAD"],
  "display_markdown": "检测到内容完全相同的文件，请选择使用已有文件、另存一份或取消。"
}
```

实际结果同时包含完整候选对比，不能只有一条提示文字。`display_markdown`是本项目业务字段，由后端生成；它不是WorkBuddy保证逐字直出的内置开关。WorkBuddy使用原生MCP结构化结果展示候选；同批重复结果还包含`duplicate_group_id`、`group_revision`和完整`group_member_item_ids`。用户在聊天中选择后再次调用`duplicate_decide`，并原样带回`review_id`、`review_revision`、`group_revision`、`group_member_item_ids`、所选候选ID和决定。刷新或重新连接后必须先调用`batch_get`恢复尚未处理的选择，不能依赖本地聊天气泡或内存状态判断。成员加入导致组修订变化时，旧决定必须被拒绝，刷新后重新展示完整成员集合；新成员不得继承旧决定。

### 5.2.1 对话搜索、读取、证据问答和明确重命名契约

WorkBuddy必须为同一聊天线程持续传入稳定`conversation_ref`。MCP将其单向哈希为长度受控的内部会话ID，
不直接把外部主键用作数据库主键。所有文件范围均使用`file_search`返回的`document_id`；工具不接受服务器
路径、工作目录相对路径、正文、SQL条件、检索分数或模型参数。

| 工具 | 必填参数 | 关键边界 |
| --- | --- | --- |
| `file_search` | `conversation_ref,query` | 固定调用只读`/api/search`，查询文字不会进入可执行写操作的通用Agent；无结果时只返回后端提示 |
| `file_read` | `conversation_ref,document_ids,read_mode` | `read_mode`只允许`READ/SUMMARY/EXPLAIN`，禁止任意instruction把只读工具变成改名、删除或移动入口 |
| `evidence_answer` | `conversation_ref,question,document_ids?` | 只提交问题和可选文件范围；答案、页码、单元格和引用由File Agent从完整原文生成并校验 |
| `file_search_clarification_resolve` | `clarification_id`及后端签发选项 | 不接受模型根据文件名猜测候选ID；过期、归属错误或选项变化由后端拒绝 |
| `file_rename` | `conversation_ref,renames[]` | 每项必须含`document_id,source_filename,target_filename`；只允许basename，禁止路径、同文件重复映射和before=after |
| `operation_plan_get` | `plan_id` | 从后端恢复真实状态，不依赖聊天气泡断言计划仍有效 |
| `operation_plan_confirm` | `plan_id,confirmation` | 仅在用户明确确认后调用；MCP不修改计划目标，后端确认前再次校验用户、状态、目标修订、冲突和白名单执行器 |

`batch_get` 中的 `result.final_filename` 和顶层 `ingest_final_filename` 都表示导入完成时的审计快照，
后续重命名不得覆盖；顶层 `current_filename` 和 `current_file_status` 则根据
`final_working_copy_id` 实时读取。分页投影必须批量读取工作副本，禁止逐条产生 N+1 查询；旧批次仅有
`result.final_filename` 时仍须兼容为 `ingest_final_filename`。回收站、未发布和关联失效必须分别返回
`TRASHED`、`NOT_PUBLISHED`、`UNAVAILABLE`，不能继续把历史名称伪装成活动文件的当前名称。

明确重命名沿用现有项目语义：用户已经同时明确目标文件和新文件名时，File Agent内部仍创建并审计
OperationPlan，再按现有明确授权来源执行；普通模糊“整理一下名称”不得调用`file_rename`。如果后端返回
范围歧义、同名冲突、等待工作副本或待确认计划，WorkBuddy必须展示结构化结果并使用对应恢复/确认工具，
不能改写文件名、替换Document ID或自行声称已经完成。原件保持不变，动作只作用于活动工作副本。

### 5.3 错误与重试约定

| 错误                               | 处理                                          |
| -------------------------------- | ------------------------------------------- |
| `SOURCE_CHANGED`                 | 本地源文件在清单后或传输中变化；该项暂停/失败，不覆盖原条目字节；重新纳入需新上传事件 |
| `IDEMPOTENCY_CONFLICT`           | 同键不同请求或已完成内容不同，拒绝                           |
| `DUPLICATE_REVIEW_REVISION_CONFLICT` | 候选修订变化，重新读取review并等待重新选择                          |
| `DUPLICATE_GROUP_REVISION_CONFLICT`  | 同批成员范围变化，重新读取完整成员和组修订后再选择                      |
| `LEASE_EXPIRED`                  | 旧OCR结果不写入有效记录，重新领取；相同已提交批次重试返回原结果           |
| `WORKING_COPY_REVISION_CONFLICT` | 重新读取同一ID状态，不能改用同名其他文件                       |
| `TARGET_NAME_CONFLICT`           | 用户指定名称占用；报告错误，不覆盖                           |
| `EXTRACTION_PARTIAL`             | 保留成功页，按质量政策继续或等待；不能伪装全文完成                   |
| `UNSUPPORTED_REQUEST`            | 附带请求当前未支持，明确记录；不影响已完成的导入结果                  |
| `UNSUPPORTED_FILE_TYPE`          | 扩展名、真实格式或MIME不在允许范围；拒绝进入解析和整理                  |
| `MIME_EXTENSION_MISMATCH`        | 扩展名与MIME/容器识别不一致；按现有风险策略拒绝或进入人工复核，不静默放行      |
| `FILE_TOO_LARGE`                 | 单文件超过现有上传限制；停止接收该项并清理临时字节                     |
| `BATCH_LIMIT_EXCEEDED`           | 清单文件数或批次总字节超限；拒绝seal/继续接收并返回当前计数             |
| `USER_QUOTA_EXCEEDED`            | 服务端核算用户容量不足；拒绝新增字节，不信任客户端配额判断                 |
| `MACRO_RISK`                     | 保留风险标记且不执行宏；是否继续读取按现有格式策略处理                    |
| `ENCRYPTED_FILE`                 | 不尝试破解；要求可读副本或进入人工处理                                |

## 6. 上传状态机和逐阶段实现

### 6.1 状态与阶段分离

`status`只使用`PENDING/RUNNING/WAITING_DUPLICATE_CONFIRMATION/WAITING_EXTERNAL_EXTRACTION/WAITING_EXISTING_RESULT/SUCCEEDED/PARTIAL/FAILED/CANCELLED/EXPIRED/SKIPPED`。条目和批次必须分别聚合，不能把条目等待直接等同于整个批次未完成。`SKIPPED`仅表示按明确规则排除的清单项，不计为成功导入；用户主动取消使用`CANCELLED`，不能计入失败。

`stage`使用`RECEIVE/EXACT_CHECK/PARSE_STAGING/NEAR_CHECK/ARCHIVE/MATERIALIZE/ORGANIZE/INDEX/EXTRA_REQUEST/DONE`。同名已知候选可以在EXACT_CHECK阶段进入确认；OCR前后新候选分别记录确认覆盖范围。

等待外部动作时不持有数据库事务、行锁或占用worker轮询。阶段job保存状态后退出，只有有效用户决定、OCR提交、依赖完成或显式重试才重新排队。业务等待不能让FilesystemJob因无意义重试耗尽三次额度。只要仍有可自动执行的条目，WorkBuddy显示“处理中”；当已无运行项、只剩重复确认等用户待办时，本轮自动处理结束并生成最终回执。

### 6.2 接收、清单与上传幂等

1. MCP校验根目录与相对路径，拒绝越界软链接、Windows junction/reparse越界、设备文件和管道；递归时不追踪形成环的链接。
2. 枚举时固定清单，保留相对路径和大小等快照。读取失败或按规则排除的项也应形成可解释统计，不静默消失。
3. 本地使用持久化传输清单记录`batch_id/item_id`，逐项向后端上传。受管事实由File Agent记录，本地只保存传输状态。
4. 后端先按条目ID和幂等性领取接收权，再流式写临时文件和SHA-256；不得先调用会commit的新建Document逻辑再检查幂等键。
5. 完整传输后核对大小、预期指纹与哈希，原子发布至隔离暂存区，登记版本并安排查重。短传、超限或失败留下的临时文件由回收任务清理。
6. 客户端上传前后检查源文件身份、大小和时间，后端哈希作为已接收字节的权威值。对无法稳定读取的活动文件返回SOURCE_CHANGED。
7. 首期“断点续传”定义为文件级续传：已完成文件不重传，传一半的文件可从头重传到同一条目；不承诺字节分块续传。
8. MCP进程退出后未传字节不能继续凭空上传；再次连接根据原清单恢复。已在后端的文件处理可继续；外部OCR仍可能等待WorkBuddy上线。
9. 新通道复用现有上传格式白名单、扩展名与MIME双重校验和基础风险检查；客户端MIME只能作输入信号，后端不得仅凭扩展名或客户端声明放行。
10. 单文件大小不得超过现有`UPLOAD_MAX_FILE_SIZE_MB`；批次文件数、批次总字节数和用户总容量必须在接收清单与接收字节前分别校验。现有系统没有对应限额时，必须先增加受控配置和错误码，不能默认无限制。
11. 宏文件保留风险标记且不执行宏；加密文件进入人工处理/上传可读副本流程，不尝试破解，也不能以空正文继续自动分类和命名。
12. 成功发布后清理无用传输临时文件；取消、失败、确认过期后的未发布字节、暂存正文和OCR页图按配置期限清理。Document、Version、review、决定、哈希、错误和ChangeSet等必要审计记录继续保留，不能为了清理文件删除业务事实。

`FileUploadService.upload`应抽出不自行commit的`stage_upload(...)`核心，由旧入口与新增入口分别管理事务和是否启动处理。旧入口在项目规则正式调整前不因选择附件而自动执行；新接口已经通过指定目录和调用`file_batch_ingest`表达提交授权，不再要求额外聊天文字。

### 6.3 精确/同名检查与正文相似检查

- 精确检查用原始字节SHA-256，与OCR无关。需核验候选当前工作副本内容，不能仅凭历史原件哈希复用已被编辑的工作副本。
- 对MCP新流水线，拆开现有`_check_upload_duplicates`中的“无候选即_enqueue_archive”行为：需要正文相似检查时先转暂存解析。
- 原生文本可读取时直接解析；扫描/混合文件只为缺失页创建外部OCR任务。
- 正文比对前只做保守的空白/Unicode规范化；保留日期、数字、否定词和表格信息，避免把关键差异抹掉。
- 首期可复用token Jaccard作为候选评分手段，但应有分批候选召回、全文比对及已定位差异。索引只用于召回，不等同于全文相同判定；记录候选上限和实际覆盖。
- 两次扫描即使OCR文本相同，也只作为内容近似候选，不升级成字节精确重复。
- 暂存正文可以写入提取表并用于相似检查，不能进入正式用户全文索引或默认搜索计数。
- 原生解析或OCR部分失败时，按新通道配置允许保守整理并标记PARTIAL；已知重复仍需确认，未完成正文查重必须在回执中明确。

### 6.4 重复决定与并发

保留现有`USE_EXISTING_FILE/CONTINUE_UPLOAD/CANCEL_UPLOAD`，为尚未就绪的任务增加`WAIT_AND_REUSE`，候选引用真实条目，不虚构工作副本ID。

| 决定   | 后端行为                                    |
| ---- | --------------------------------------- |
| 使用已有 | 校验候选当前授权和比较版本并绑定最终文件ID；默认不重新自动命名或分类，不覆盖人工结果；可补齐缺失索引并继续只读附带请求 |
| 保留两份 | 新建独立逻辑文件和工作副本；现有归档路径包含上传版本ID，可复用该方式防覆盖  |
| 等待复用 | 固定主任务引用，主任务成功后绑定；主任务失败/取消则报告，不自动另存，并终止该等待项的后续用户任务 |
| 取消   | 终止该条目及附带请求，清理本次未发布暂存；不删除已有目标或其他条目       |

取消必须检查发布边界：尚未发布可取消导入；已经发布但附带请求仍在执行时，只取消剩余请求并保留真实导入成功状态。不能因条目尚未整体结束就删除已发布文件。

对同批/并发相同内容，在授权范围内使用数据库锁或唯一约束确定组和主任务。组确认包含明确成员和修订号；“只留一份”必须同时允许主任务继续和其余成员等待，禁止主任务也在等待自己的结果。

不要持锁等待用户或OCR；新加入成员不继承旧选择。跨用户处理任务不能通过组信息泄露；只有用户有权访问的候选才能用于提示和复用。

现有`decide`对已解决记录主要检查decision，改造时必须同时比较候选ID、组成员及参数摘要，防止同样USE_EXISTING_FILE却选择不同文件的重试被错误接受。绑定附带请求不能提前将暂存文件标记为`USED_IN_MESSAGE`导致复用/取消被旧条件阻止。

### 6.5 混合批次中的部分重复处理

批次以条目为隔离单元。发现部分文件重复时，只暂停对应条目，非重复文件立即继续解析、分类、命名、按主分类落位和索引。用户长时间不处理重复项，不得阻塞其他文件完成，也不得让worker持续轮询等待。

| 条目情况/用户决定 | 条目处理 | 对同批其他文件的影响 | 附带请求与回执 |
| --- | --- | --- | --- |
| 非重复文件 | 立即继续完整整理流水线 | 无影响 | 分类与实际命名完成后自动执行总结等附带请求 |
| 与库内已有文件重复 | 该条目进入`WAITING_DUPLICATE_CONFIRMATION` | 其他条目继续 | 最终回执列为“等待确认”，保留结构化选择 |
| 与同批主任务重复且主任务处理中 | 使用`WAIT_AND_REUSE`等待固定主任务 | 主任务和其他非重复条目继续 | 主任务成功后绑定；失败或取消则报告并终止该等待项，不自动另存、不继续其用户任务 |
| `USE_EXISTING_FILE` | 不创建新逻辑文件，绑定所选已有文件 | 无影响 | 只读总结/读取可以继续；不重新自动命名或分类，不覆盖人工名称和分类 |
| `CONTINUE_UPLOAD` | 创建独立逻辑文件和工作副本 | 无影响 | 正常整理；名称冲突按稳定条目ID后缀处理，不覆盖已有文件 |
| `CANCEL_UPLOAD` | 记为`CANCELLED`并清理未发布数据 | 无影响 | 不执行该项附带请求；取消单列统计，不计为失败 |
| 候选或组修订已变化 | 返回`DUPLICATE_REVIEW_REVISION_CONFLICT`或`DUPLICATE_GROUP_REVISION_CONFLICT`并要求刷新 | 只影响该条目/重复组 | 新成员不继承旧决定，已经完成的文件不重跑 |
| 确认期限届满 | 待确认条目标为`EXPIRED` | 已成功和已失败条目保持原结果 | 批次状态更新为`EXPIRED`，追加新版回执，不默认继续上传 |

批次状态和展示按以下规则聚合：

1. 仍有任一条目可以自动运行时，批次显示“处理中”，后台状态为`RUNNING`。
2. 已无运行项时立即显示“文件处理完成”并生成覆盖全部清单项的最终回执；等待确认是待处理事项，不阻止本轮回执生成。
3. 最终回执中仍有等待确认项且尚未过期时，后台状态为`PARTIAL`；因此`3个成功 + 1个等待确认 + 1个失败`显示“文件处理完成”，后台状态为`PARTIAL`，回执必须分别列出三类结果。全部条目都等待确认时也按此规则生成回执，不能假装成功。
4. 成功项之外只有用户主动取消项时，后台状态为`SUCCEEDED`，但回执必须单列取消数量和文件，不能把取消项写成成功导入。
5. 全部重复文件都选择`USE_EXISTING_FILE`时，后台状态为`SUCCEEDED`；回执显示新增文件数为0、复用文件数为实际数量，并返回稳定的已有文件ID映射。
6. 只要确认期限届满，批次状态更新为`EXPIRED`；此前成功、失败、取消和复用结果继续保留，不能回滚。
7. 没有等待或过期项时，成功与真实失败混合为`PARTIAL`，全部失败为`FAILED`，全部由用户取消为`CANCELLED`。
8. 待确认条目后续解决时重新聚合批次状态：全部实际保留项成功且没有失败/等待时更新为`SUCCEEDED`；仍有真实失败时保持`PARTIAL`。每次变化都递增`result_revision`并追加回执修订。

普通“分类”附带请求就是系统默认分类，不创建第二次分类任务；用户明确指定分类节点时才作为本次约束合并。总结请求必须在对应最终文件完成分类和实际命名后执行：逐份总结按条目就绪顺序运行；汇总全部文件使用本轮已经确定的最终文件ID并明确列出尚待确认、失败或取消的排除项。待确认条目之后通过`duplicate_decide`恢复时，只续跑该条目并递增`result_revision`，追加后续回执；不得重跑其他文件。只有用户明确要求修改所复用的已有文件时，才允许进入文件修改链路。

### 6.6 OCR准备、领取和结果写入

1. 修改`extract_document_text`/原生解析入口，明确`ocr_mode=EXTERNAL`时只解析文本层并返回缺页；混合PDF的原生文本不可被OCR覆盖。
2. File Agent渲染缺页并保存资源引用，创建外部任务；内部源版本可以是UPLOAD层版本，不需要已存在工作副本。
3. `extraction_claim`在事务中发放租约及随机token，返回`task_id/source_sha256/source_version_id/page_numbers`和真实可读取资源。
4. MCP下载授权页面至受控暂存目录；WorkBuddy调用已配置OCR工具。只返回字符串`source_ref`不算页面传输完成。
5. 回写包含页号、文本、实际提供的元素/表格/坐标、provider信息、处理版本及错误。缺少置信度或位置时保留缺失，不填假的固定值。
6. 后端验证身份、租约、源哈希、版本、页集合和数据规模；同一提交幂等，冲突结果不静默覆盖。
7. 复用`FileExtractionRepository`、现有页面/元素和证据规范化能力保存结果，任务覆盖完成后安排NEAR_CHECK或后续补齐阶段。
8. 旧`structured_extraction`任务继续恢复旧AgentRun；新任务通过明确`origin=WORKBUDDY_INGEST`只恢复`ingest_items`，不能两个恢复器都执行。

最新`ocr/service.py`增加了云OCR特定错误的fallback逻辑。新外部模式必须绕开该内部调用链；保留它供旧通道使用，不能让外部失败又偷偷触发内部OCR产生重复调用和费用。

解析/OCR计算缓存应绑定源哈希、解析器/Provider配置与输出结构版本，并限制授权范围。首版可以只对同批明确相同内容复用计算；不必先实现全局共享缓存。复用计算后仍应建立各逻辑文件自己的版本/证据关联。

第一阶段先交付外部OCR功能闭环，不把完整的外部Provider边界治理作为P1–P8的阻塞项。当前阶段仍必须沿用现有认证、用户/工作区授权、任务租约、页级范围校验、结果幂等和审计要求；更细的数据驻留、Provider准入、敏感材料分级与授权提示作为后续专项方案处理，不能在本文中伪装成已经完成。

### 6.7 归档、工作副本与默认整理

1. 完成适用查重和决定后，复用`_archive_upload`及IMPORT流程保存原件、建立工作副本；处理中副本保持`ORGANIZING`，防止正式搜索提前发布。
2. 将暂存提取结果关联/复制为目标版本的提取运行。字节不变且提取配置有效时不重复解析；选择已有异内容文件则读取已有版本，不能挂入新上传正文。
3. `organization_service`调用现有`InitialWorkingCopyOrganizer`、分类Runtime和命名服务，增加“使用已存在提取运行”的参数，不让`suggest_for_initial_import`重复触发OCR。
4. 明确用户名称/分类优先；普通“分类”仍使用系统默认分类；正文推断与材料包政策结果分别记录证据来源；已合规名称、命名依据不足、受保护表单及人工更正允许`NO_CHANGE`，且单纯`NO_CHANGE`不降低整体成功状态。
5. 将现有`_finalize_initial_organization`拆出“逻辑分类保存”“目标名称决定”“目标路径决定”“原子发布”四项职责。新通道默认`BY_CATEGORY`并按最终主分类解析路径；主分类不明确时使用受控中性落位或进入待复核，不得猜测目录。`NEUTRAL`仅作为用户明确选择或降级策略。
6. 执行真实文件动作后再更新数据库名称和回执。禁止以`rename_status=READY`或有`proposed_filename`直接生成COMPLETED。
7. 首次工作副本发布复用现有发布机制；复用已有文件并明确要求改名时，走共享受控执行器，授权来源记为本次明确请求。
8. 文件系统与数据库无法靠一个数据库事务天然原子完成：先持久化操作意图和预期前后状态，执行不覆盖的原子改名/发布，再提交结果。进程崩溃后按路径、文件身份、哈希和操作ID核对后续跑，不重复改名；不能只凭目标哈希相同就认定是本次操作产物。

可保留内部OperationPlan作为审计对象，但新自动整理记录`authorization_source=AUTO_ORGANIZE_POLICY`和policy_version；不得写一条虚构“用户确认”来骗过旧入口。将`WorkingCopyOperationService._execute_item`的校验与文件动作抽成可复用执行器，旧确认入口和新直接入口分别传入真实授权上下文。

### 6.8 来源目录规则接入

新增`SourceProvenance`读取服务：优先从导入条目的`source_root_ref + source_relative_path`读取来源，再兼容现有受管源`ManagedFile.relative_path`。实际归档路径仍使用后端控制的`uploads/...`。

更新`UploadedRenameSuggestionService._managed_source_relative_path`、分类器source_context构造和来源包落位入口，让MCP复制导入也能识别已配置的材料包规则。来源路径是规则上下文，不是后端可写目标；任何来源名称不能越权指定工作区或分类目录。

`source_path_policy.py`当前包含固定根目录和忽略规则。将其暴露为版本化、可选的规则集，而非对所有用户本地目录默认应用。被过滤的文件记录SKIPPED和原因；不能把“uploads”“我的文档”等普通目录名在任意根下静默排除。

### 6.9 索引、附带请求与回执

- 复用`chunks/service.py`的`DocumentIndexService`及现有分类投影；正式索引发布只针对已获准目标，名称/分类变更只更新对应投影，不重新OCR。
- `request_runner`在最终ID固定、默认分类和实际命名完成后处理原始附带请求。总结、查询、统计留在File Agent，不要求WorkBuddy读取全文重写答案；普通分类请求直接消费默认分类结果，不重复分类。
- 第一阶段允许复用现有后端会话/AgentRun，但必须建立`external request → owned conversation/run`映射，明确固定输入文件集合，避免二次执行默认导入。
- 批次附带“逐份总结”与“汇总所有文件”需要区分：前者在各目标完成分类和命名后逐项执行；后者在本轮已确定的最终ID上执行一次，并明确列出等待、失败和取消的排除项，按用户选择及最终文件ID去重计数，不按上传记录数误算。待确认文件后续恢复时只追加该项结果和必要的增量汇总。
- 收到超出首期支持范围的附带请求时持久化并返回待支持/失败原因；不能宣称已经全部完成。上线批量导入前至少验证常用“整理并总结”和“指定名称/分类”分支。
- 返回`ingest_status/extraction_status/organization_status/index_status/user_task_status`及逐文件明细，后端生成display_markdown，WorkBuddy只展示。
- 总状态必须按第6.5节聚合：仍有可自动执行的处理或OCR任务时显示“处理中”；只剩重复确认时允许显示“文件处理完成”并生成最终回执。回执必须汇报成功文件数、等待确认数、复用数、取消数、失败数、过期数、部分完成数、排除数以及实际保留的逻辑文件数。

## 7. 本地与后端配置

### 7.1 建议新增配置项

以下为拟议配置，不是现有开关。接入时加入`core/config.py`、示例配置和对应测试。

| 配置                                     | 建议默认               | 作用                        |
| -------------------------------------- | ------------------ | ------------------------- |
| `INTEGRATION_INGEST_ENABLED`           | false，上线试点时开启      | 仅控制新通道                    |
| `INTEGRATION_EXTERNAL_OCR_ENABLED`     | true               | 新通道外部OCR策略，旧通道单独保留        |
| `INTEGRATION_PLACEMENT_MODE`           | BY_CATEGORY        | 按最终主分类自动落位；主分类不明确时受控降级  |
| `INTEGRATION_RULE_PROFILE`             | content_based      | 与最新旧通道材料包策略分开选择           |
| `INTEGRATION_COLLISION_POLICY`         | STABLE_ITEM_SUFFIX | 自动名称冲突处理                  |
| `INTEGRATION_ALLOW_PARTIAL_EXTRACTION` | true               | 识别不完整允许保守整理，但返回PARTIAL    |
| `LOCAL_IMPORT_ROOTS_JSON`              | 显式配置               | 仅MCP读取，映射根引用到用户授权目录       |
| `LOCAL_TRANSFER_STATE_DIR`             | 受控本地目录             | 断点清单存储                    |
| `LOCAL_UPLOAD_CONCURRENCY`             | 2作为初始值             | 避免同时大量传输，试点后按机器容量调整       |
| `EXTERNAL_EXTRACTION_LEASE_SECONDS`    | 可配置                | 按页批次耗时设定并支持续租，不把固定时间当业务保证 |
| `INTEGRATION_MAX_BATCH_FILES`          | 显式配置               | 单批清单最大文件数，登记清单时校验          |
| `INTEGRATION_MAX_BATCH_BYTES`          | 显式配置               | 单批总字节上限，清单和实际接收两次校验       |
| `INTEGRATION_USER_QUOTA_BYTES`         | 显式配置               | 当前用户可用总容量，不能信任客户端自行统计     |
| `INTEGRATION_STAGING_RETENTION_HOURS`  | 与现有暂存策略一致         | 取消、失败和过期条目的未发布数据清理期限      |
| `EXTERNAL_PAGE_RETENTION_HOURS`        | 显式配置               | 外部OCR页图和中间资源的清理期限            |

不要直接把现有所有自动分类/自动落位开关全局打开。新通道的有效策略必须可读取、可记录并随任务冻结；旧UI和既有受管根行为保持单独控制，避免试点时重排历史文件。单文件大小、扩展名/MIME、宏和加密检查复用现有后端能力；新增的批次总量、用户容量和中间资源期限必须在上线前给出配置值和测试，不能只在MCP本地限制。

### 7.2 部署和运行边界

继续复用现有PostgreSQL、API和Filesystem worker。新阶段job注册到明确的队列并加入worker支持列表；建议先复用RECONCILE/ANALYSIS/IMPORT相关队列，若增加`INGESTION`队列，同步更新启动脚本、Compose、预检和runbook，避免排队后无人消费。

MCP本机进程只需要后端地址、已授权身份凭证、受控源目录和暂存目录。后端凭证由现有认证机制或新增明确的客户端凭证机制提供，不能在Skill中保存明文密码，也不能让模型填写任意user_id。

本次会修改模型、Schema和依赖，不能只按“Windows API/Web代码热更新”替换源码。根据最新`deploy/`分层镜像方案评估是否需重建依赖层；至少部署对应迁移、API、worker和配置，保持版本一致。不执行重置工作副本脚本作为迁移手段。

## 8. WorkBuddy接入约束

项目指令/接入Skill明确：

1. 本地材料导入走`file_batch_ingest`，不自行遍历后逐个调用不受控shell上传。
2. 受管文件检索、总结、分类和命名由File Agent完成。
3. 仅File Agent返回待提取任务时调用OCR，回传结果后由后端继续。
4. 按任务ID查状态；遇到重复确认时使用MCP结构化结果展示真实候选及同批完整成员。用户在聊天中作出选择后，WorkBuddy再次调用`duplicate_decide`并传回`review_id/review_revision/group_revision/group_member_item_ids/candidate_id/decision`；组修订冲突时先刷新再重新选择，不得根据自然语言自行猜测候选ID或把旧选择扩展到新成员。
5. 已明确目标的普通整理直接处理；不额外生成用户确认环节，也不绕过客户端/组织本身的访问控制。
6. 展示后端最终答案、文件名和出处，不增补业务事实，内部ID仅用于工具参数。
7. 刷新、重启或重新连接后调用`batch_get`读取最新`result_revision`、待确认条目和允许决定，恢复尚未处理的选择；本地缓存只优化展示，不能成为恢复依据。
8. 批次仍有自动运行项时显示“处理中”；当只剩重复确认等用户待办时显示“文件处理完成”和最终回执，同时保留可继续操作的重复确认入口。

此阶段只写配置模板和接入说明；不要求先开发新的WorkBuddy UI或发布公共插件。项目指令不是可靠权限屏障，后端状态机才负责强制检查。严格逐字展示须额外渲染集成，不能承诺仅靠Skill实现。

## 9. 验收与必要测试

### 9.1 新增测试文件建议

在`apps/api/app/tests/`新增：`test_integration_ingest_api.py`、`test_ingest_batch.py`、`test_ingest_workflow.py`、`test_ingest_duplicate_groups.py`、`test_external_extraction.py`、`test_ingest_auto_organization.py`、`test_ingest_receipts.py`。

在`apps/mcp/tests/`新增：`test_local_import.py`、`test_tool_contract.py`、`test_transfer_resume.py`。模型和OCR使用deterministic fake；租约、并发、唯一约束与提交恢复增加真实PostgreSQL集成测试，不能仅靠SQLite内存测试证明并发行为。

### 9.2 必须通过的场景

| 场景 | 验收结果 |
| --- | --- |
| 单个普通文件，无附带请求 | 自动分类、真实命名或合理NO_CHANGE、可检索，源文件哈希不变 |
| 扫描PDF/混合PDF | 首先字节哈希；缺页OCR回写后做相似检查，原生文本不被覆盖 |
| 精确重复 | OCR可跳过；确认前不正式发布；选择复用返回已有ID，选择另存返回独立ID |
| 同名异内容 | 展示真实差异，不自动覆盖、合并或登记为旧版本 |
| 两次扫描正文近似 | 精确哈希不同，OCR后提示近似，不能丢弃新内容 |
| 回收站重复 | 新建活动文件，不恢复原回收记录 |
| 同批相同内容 | 一次组确认固定成员；主任务无等待环；分别保留有不同工作副本ID |
| 混合批次：3成功、1等待确认、1失败 | 非重复项不被阻塞；无运行项后显示“文件处理完成”，批次为PARTIAL，最终回执列全5项 |
| 成功文件加用户主动取消文件 | 批次为SUCCEEDED；取消项单列且不计为成功或失败，其他文件结果不变 |
| 全部重复且均选择使用已有文件 | 批次为SUCCEEDED；新增数为0，复用数和已有文件ID映射正确，不改已有人工名称/分类 |
| 同键不同已有候选 | 拒绝幂等冲突，不复用第一次之外的选择 |
| 用户未选择且其他文件仍运行 | 重复项等待，其他文件继续，批次显示处理中 |
| 用户未选择且已无运行项 | 显示文件处理完成并生成含等待项的最终回执；刷新后仍可继续选择 |
| 确认过期 | 不默认继续；批次更新为EXPIRED，既有成功/失败/复用结果不回滚 |
| WAIT_AND_REUSE主任务失败或取消 | 等待项报告依赖失败，不自动另存，不执行该项后续用户任务 |
| 上传响应丢失再重试 | 同一条目/任务/版本，不多一份归档 |
| 导入中断并恢复 | 已成功文件不重传、不重新改名；待传项继续 |
| 本地文件在传输中改变 | SOURCE_CHANGED/指纹不一致，不能悄悄将变化内容当原事件 |
| OCR租约过期/重复提交 | 过期结果拒绝；相同已提交请求返回相同结果 |
| OCR部分成功 | 保留成功页，失败页可重试，回执PARTIAL及覆盖信息 |
| 命名依据不足 | 保留原名并记录NO_CHANGE；其他必要步骤成功时文件和批次不因此降级 |
| 用户指定名称/分类 | 一次应用；不要求指定名称出现在正文中；精确名称冲突不覆盖 |
| 普通分类和附带总结 | 分类只执行系统默认流程；总结在分类与实际命名完成后自动执行，不读取旧短预览代替正文 |
| 使用已有文件后附带只读请求 | 替换为已有文件ID并继续总结/读取；不重新命名、分类或覆盖人工结果 |
| 指定目录后无额外聊天文字 | `file_batch_ingest`正常提交并自动分类、命名、落位和索引；未指定目录时不得自动提交 |
| WorkBuddy消息提交多个附件且无额外文字 | `workbuddy_attachment_ingest`固定本轮附件清单并自动整理；只选择未提交的附件不触发 |
| 附件缓存路径越权、软链接或文件名不一致 | MCP在读取字节前拒绝；后端不出现清单、暂存文件或绝对路径 |
| 格式、MIME、容量、宏和加密校验 | 与现有后端边界一致；批次/用户超限明确拒绝，宏不执行，加密文件不尝试破解 |
| 取消/失败/过期数据清理 | 未发布字节、暂存正文和OCR页图按期限清理，必要审计与最终结果仍可查询 |
| 最新职称/应聘/保留原名规则 | 选定相应规则集时生效，来源路径可追溯；普通目录不误套规则 |
| 工作副本并发改名 | revision检查生效，不改到另一个同名对象 |
| 文件动作后进程退出 | 恢复后核对实际动作，审计不重复，成功回执对应真实路径 |
| 无权访问的重复候选 | 不展示名称/路径/任务细节，不跨范围复用 |
| 整理成功但总结失败 | 两种状态分别返回，不说全部完成 |
| 搜索文字伪装成删除或重命名指令 | `file_search`只进入只读搜索API，不产生文件动作或OperationPlan |
| 读取工具传入任意写操作文字 | Schema只允许固定`READ/SUMMARY/EXPLAIN`，在调用后端前拒绝 |
| 证据问答由客户端提交引用 | Schema无Evidence输入；后端只从完整原文和持久化证据生成引用 |
| 明确重命名携带路径、重复Document ID或同名映射 | MCP前置拒绝；合法映射仍由后端校验权限、当前名称、冲突和审计 |

优先回归现有`test_files.py`、`test_file_lifecycle.py`、`test_file_lifecycle_storage.py`、`test_filesystem_jobs.py`、`test_document_classifier.py`、`test_collision_naming.py`、`test_trial_evaluation_naming.py`、`test_managed_source_path_policy.py`、`test_operations.py`、`test_document_index_service.py`及相应提取测试。

从仓库根运行示例（环境准备后由实施者执行，本次未运行）：

```bash
python -m pytest apps/api/app/tests/test_files.py apps/api/app/tests/test_file_lifecycle.py apps/api/app/tests/test_filesystem_jobs.py
```

新增测试文件完成后再运行对应新增测试；发布前按AGENTS.md要求执行规定回归。迁移检查至少包括现有`test_alembic_migration_graph.py`及真实PostgreSQL升级验证。

## 10. 按顺序实施的工作包

| 工作包            | 修改/新增重点                                                    | 前置依赖   | 完成标志                             |
| -------------- | ---------------------------------------------------------- | ------ | -------------------------------- |
| P0：冻结契约与样本     | 同步规则文档、确定上述API/状态/默认策略、混合批次聚合表和安全配置，选代表性小样本                  | 最新代码基线 | 团队按同一新通道规则开发，旧行为不被误改             |
| P1：批次模型与基础API  | models、迁移、integrations、ingestion repository；认证、幂等、result_revision | P0     | 可建立清单、登记条目、分页查看、恢复待办，不处理真实文件也能验证状态约束 |
| P2：单文件接入与MCP骨架 | 抽stage_upload核心，file_ingest、job_get，事务内安排处理                | P1     | WorkBuddy单文件普通文档能进入后端任务，无需伪造聊天消息 |
| P3：本地目录传输      | local_import、transfer_state、file_batch_ingest/batch_resume、格式/MIME与容量限制 | P2     | 小批量可传、可恢复、来源路径保留；客户端退出不丢清单，超限不落入无界暂存 |
| P4：两阶段查重与确认    | duplicate_service、review修订/组表、duplicate_decide、混合批次独立推进       | P1–P3  | 精确/同名/同批分支完整；非重复项不阻塞，刷新后可恢复选择     |
| P5：外部OCR闭环     | 原生解析拆分、external_extraction、页资源、领取/回写/续租、worker续跑           | P2、P4  | 扫描/混合文件OCR后完成近似查重，不重复调用内部OCR     |
| P6：自动整理与真实发布   | organization_service、organizer、共享执行器、来源政策、revision、索引      | P4、P5  | 非重复项直接整理，重复项决定后续跑；真正分类/改名，保留原件和人工结果，无二次确认 |
| P7：附带请求与批次回执   | request_runner、receipt/result_revision、WorkBuddy结构化确认与恢复模板 | P6     | 整理后总结、复用后只读请求、等待项最终回执和增量续跑正确展示   |
| P8：故障恢复与试点     | 并发/崩溃/租约/权限/清理测试，部署迁移、API和workers                       | P1–P7  | 下述试点验收通过后，才导入整批本地材料              |
| P9：WorkBuddy会话附件适配 | attachment registry、固定提交事件、批量附件清单、缓存根校验和工具契约 | P2–P8 | 已提交附件无需额外文字并复用完整导入链路，宿主路径不进入后端 |
| P10：对话文件能力MCP化 | 只读搜索、固定读取、证据回答、澄清恢复、明确重命名和OperationPlan工具 | P7–P9 | WorkBuddy可按稳定文件ID完成读写分离的文件任务，写操作不绕过后端审计 |

建议每个工作包一个或多个独立提交；不要同时重写Agent Runtime、前端和部署框架。P2是开发里程碑，不是“上传流程已经完整上线”；批量正式使用必须至少通过P4–P8相关场景。

### 10.1 首批材料试点

1. 使用单独后端试点归档根和工作副本根，接入用户指定本地源目录，COPY模式。
2. 选择约20–30个代表性文件，包括文本PDF、扫描PDF、混合PDF、Word、Excel、图片、同名异内容和精确重复；另纳入本批真实材料包样本。
3. 检查源文件数及哈希不变、逐文件结果可追溯、命名和分类符合已选择规则集。
4. 主动中断一次本地传输、一次后端worker和一次OCR租约，验证恢复不重复写入。
5. 再扩大到较大子目录，观察实际CPU/内存、磁盘、OCR吞吐及等待项数量，然后处理整批。并发按实测调整，不承诺未经测量的吞吐或工期。

2026-09-09 首轮执行记录：

- 使用 `/tmp` 隔离的上传暂存、不可变归档、工作副本和回收站根，对项目 `docs/` 材料建立 26 项固定清单；项目源文件仅以只读 COPY 方式使用。
- 清单包含 23 个普通样本、精确重复及同名异内容样本。非重复项先行整理；3 个重复项经结构化选择 `WAIT_AND_REUSE` 后复用已有结果，最终 26 项全部成功，保留 23 个逻辑文件。
- 最终只读报告确认 `changed_sources`、`missing_server_items`、`unexpected_server_items`、`unfinished_items`、`missing_final_mapping`、`incomplete_organization`、`missing_naming_receipt`、`missing_duplicate_reviews` 和 `source_mapping_mismatches` 均为空。
- 实际中断/恢复覆盖本地传输状态、后端 worker、刷新后重复选择和 OCR 租约。OCR 中文样本因本机 Provider 缺少中文语言包形成真实 `PARTIAL`，该结果证明失败页和保守发布边界；它不替代部署环境安装中文 OCR Provider 后的识别质量验收。
- 本轮 `docs/` 实物样本没有同时提供文本 PDF、扫描 PDF、混合 PDF、Word 和 Excel。相关解析及混合 PDF 外部 OCR 行为已有自动化覆盖；上线真实业务目录前仍须按本节第2项补齐这些格式的实物烟测，并记录 OCR 准确率、耗时、CPU、内存和磁盘指标。

### 10.2 回退方式

关闭新通道提交入口，暂停新增批次，保留已存在任务、审计和数据库记录。处理中OCR及传输可暂停并后续恢复；已成功导入的文件不通过回退自动删除。旧UI/旧受管源入口继续使用兼容路径。

修复后按批次/条目恢复，不运行清空工作目录或重置数据库脚本。若需要撤销某个已执行文件动作，另按ChangeSet和当前冲突状态处理，不保证无条件恢复旧路径。

## 11. 第二、三阶段具体接入点

### 第二阶段：独立检索与总结

- 在同一`integrations`入口新增`file_query`、`file_query_continue`，保留用户原始问题、确定文件引用和任务ID。
- 复用`evidence_answer/service.py`的EvidenceAnswerService、`retrieval/`和`spreadsheet_analysis/`；由后端完成查询理解、证据读取、生成和统计，MCP只转接，WorkBuddy只展示。
- 复用P7的外部会话映射、后端结果投影、等待/澄清和任务恢复约定，不重新实现第二套查询状态机。
- 验收重点：多同名目标澄清、版本一致性、全文覆盖、权限、统计口径、无命中与未就绪区分。

### 第三阶段：独立文件操作

- 新增`file_resolve`和`file_action`外部工具，复用P6共享执行器和最新明确改名直接执行能力。
- 完整开放RENAME/MOVE/TRASH/RESTORE/ORGANIZE，按稳定ID及revision处理，不走模型猜测路径。
- 普通操作不二次确认；用户目标不明确才要求补充。旧覆盖选项不自动暴露到新通道。
- 验收重点：回收/恢复身份连续、目录冲突、批次固定范围、逐项幂等和权限，避免对原件执行删除/改名。

## 12. 同步文档清单与实施注意

实施时同步更新：

- `AGENTS.md`与`agent.md`：新通道上线时明确“指定目录并调用导入接口”已经构成提交授权，已提交附件无需再输入额外文字；普通整理直接执行、重复确认保留。规则修改必须与后端状态机和测试同批交付，不能用修改规则文件绕过真实权限与审计。
- `docs/api-contract.md`：新增外部API、错误、状态、最终ID映射和版本条件。
- `docs/database-schema.md`：新增表、约束、来源上下文及暂存正文发布边界。
- `README.md`、`docs/runbook.md`：MCP启动、认证、worker队列、外部OCR恢复及批次运维。
- `deploy/`对应说明和示例配置：迁移/镜像/worker保持同版本，不能只更新Web或API代码。
- 两份流程文档：保持File Agent承担全部检索、总结、分类、命名的最新分工。

此前`docs/2026-09-07-workbuddy-mcp-file-agent-architecture.md`包含将分类语义判断和答案生成迁往WorkBuddy的早期设计，不作为本次实施依据。发生冲突，以当前用户要求和本方案为准。

**开发起点：P0 → P1 → P2。正式处理整批本地文件的交付门槛：P1–P8完成并通过试点；不能只接通上传HTTP接口就开始批量整理生产材料。**
