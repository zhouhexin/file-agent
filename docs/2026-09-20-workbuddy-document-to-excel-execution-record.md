# WorkBuddy 文档内容提取到 Excel：执行记录

## D00：开发基线

- 状态：自动验证通过；真实 WorkBuddy 能力验证待 D01。
- 基线提交：`c8f7668`。
- 迁移 head：`20260920_0001`。
- 工作树：保留既有图片累计表格 Demo 与其他用户变更，未清理或重置。
- 已执行命令：

```bash
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest apps/api/app/tests/test_structured_datasets.py apps/api/app/tests/test_structured_extraction.py apps/api/app/tests/test_external_extraction_api.py apps/api/app/tests/test_config.py -q --tb=short
PYTHONPATH=apps/mcp /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest apps/mcp/tests/test_dataset_tools.py apps/mcp/tests/test_attachment_transfer.py apps/mcp/tests/test_workbuddy_submission.py apps/mcp/tests/test_client.py apps/mcp/tests/test_server.py -q --tb=short
```

- 结果：API 93 passed（5 条 PyMuPDF SWIG deprecation warnings）；MCP 56 passed。
- 未完成项：D01 真实 WorkBuddy 能力矩阵和样本尚未执行；该项需要在真实宿主环境完成，不能以自动假结果替代。

## D02–D08：仓库实现状态

- 状态：代码闭环已实现，本地确定性测试通过；D01、D09 的真实环境验收仍未完成。
- D02–D03：已增加冻结 `WORKBUDDY_DOCUMENT` profile、服务端派生执行者、任务与版本策略字段、连续迁移 `20260920_0002`，默认开关关闭；DEFAULT 模式继续排除新增默认字段以保持摘要兼容。
- D04–D05：已实现独立 get/claim/renew/source/submit API、租约校验、受控原文件下载、大小和 SHA-256 核验、完整/部分/失败验收、逐区段页面/元素持久化及 COMPLETE 后续流程。结果请求在 Pydantic 深层解析前执行 4 MiB 原始请求体限制；claim、renew 和 submit 均写入不含 token、路径或正文的 ToolInvocation、ChangeSet 和 ChangeItem。精确重复先进入原决策；选择继续后先等待宿主正文，不能直接归档。取消或终态条目拒绝晚到领取、续租、下载和新提交。
- D06：受保护版本只复用同版本 `workbuddy-external-document` 完整运行；正文读取、转换、force reprocess、索引的最新运行选择均禁止回落服务器解析。发布工作副本时复制正文、元素、覆盖 metadata 和不可回退策略。
- D07：source 返回正式 `source_units`；Word 页码保持 null，PDF 使用真实页码。append 使用 `source_unit_id + quote + start/end` 校验字段证据，并在派生元素保存来源运行、来源页、引文范围及原元素 ID。数据集返回文件数和格式计数，空结果不推进 revision。
- D08：MCP 仅在两个开关同时打开时注册 `document_extraction_get/claim/renew/submit`；claim 实际流式下载并校验后才返回 ResourceLink。插件 Skill 已改为图片、Word、PDF 的完整调用顺序和正式 evidence_refs。

## 自动验证证据

```bash
PYTHONPATH=apps/api:apps/mcp /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest \
  apps/api/app/tests/test_structured_datasets.py \
  apps/api/app/tests/test_document_extraction_contracts.py \
  apps/api/app/tests/test_document_extraction_api.py \
  apps/mcp/tests/test_dataset_tools.py \
  apps/mcp/tests/test_document_extraction_tools.py \
  apps/mcp/tests/test_server.py -q
# 28 passed

PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest \
  apps/api/app/tests/test_file_lifecycle.py \
  apps/api/app/tests/test_integration_ingest_api.py \
  apps/api/app/tests/test_document_index_service.py \
  apps/api/app/tests/test_config.py -q --tb=short
# 141 passed, 5 existing PyMuPDF SWIG deprecation warnings

PYTHONPATH=apps/api:apps/mcp /opt/homebrew/anaconda3/envs/py311/bin/python -m compileall -q \
  apps/api/app apps/mcp/file_agent_mcp
git diff --check
# passed
```

提交前完整回归：

```bash
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest apps/api/app/tests -q --tb=short
# 1375 passed, 19 skipped, 9 warnings

PYTHONPATH=apps/mcp /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest apps/mcp/tests -q --tb=short
# 95 passed
```

当前 Alembic 唯一 head 为 `20260920_0002`；已对当前配置的 PostgreSQL 开发库执行 `upgrade head`。文件域重置清单已同步包含三张 structured dataset 表。

新增自动场景包含：Word 无物理页码、PDF 真实页码、正式证据 UUID/引文范围、跨用户向同一数据集追加、四条记录合计 400、XLSX 下载核对、MCP 工具条件注册、claim 下载哈希校验、PARTIAL 不归档、工作副本正文和元素复制。

## 尚未完成且不能由本地假结果代替

- D01：尚未在真实 WorkBuddy 列出实际图片、DOC/DOCX、PDF 工具及定位能力，也未创建和运行计划规定的 11 条无隐私样本与失败样本。
- D02/D09：当前 PostgreSQL 开发库已完成 upgrade；尚未在独立 PostgreSQL 测试库执行 downgrade，并验证并发 claim、submit 和 dataset append。
- D09：尚未用真实 WorkBuddy 完成 11 行、金额 1400.00 的 XLSX 人工核对，以及 DOC 独立 3 行、500.00 验证；也未完成重启恢复烟测。
- D10：连接器 1.0.3 已携带两个 Demo 开关，本机开发配置也已启用，Settings 和 `.env.example` 仍默认关闭。尚未重新安装插件、重启 API/worker 并完成真实 WorkBuddy 会话验收。

因此当前结论是“仓库内 Demo 实现基本完成，本地自动验证通过；两份方案定义的整体任务尚未严格验收完成”。

## 2026-09-20：Excel 目标表与按文件名持续追加

- 需求与验收基准已新增到
  `docs/2026-09-20-workbuddy-excel-target-continuation-spec.md`，本节只记录实际执行结果。
- 新增连续迁移 `20260920_0005`，当前唯一迁移 head 更新为该版本；增加目标文件名、工作表、上传模板已有行和
  模板来源版本字段。
- API 新增上传 Excel 注册目标和按完整文件名解析目标；解析时重新读取当前真实 XLSX 标题，并返回稳定
  dataset、working copy、document、current version ID。不存在、歧义、多工作表、schema 变化、同名冲突、
  未携带稳定绑定和确认期间版本变化均关闭式失败。
- 新建目标使用用户指定或系统可读文件名；当前物理 basename、文件列表名和下载名一致。系统生成目标位于
  `WORKING_COPY_STORAGE_ROOT/structured-datasets/{dataset_id}/{filename}`；上传 Excel 复用原有普通工作副本，
  不复制第二个普通文件。历史版本只保存在 `.versions` 和 DocumentVersion 中。
- 受控改名已增加累计表窄范围适配：只移动当前可见文件，同步数据集目标名和 ManagedFile/FileObject，不移动
  `.versions` 历史内容；改名后可按新文件名继续追加到同一 Document/WorkingCopy。
- MCP 新增 `structured_dataset_create_from_excel`、`structured_dataset_resolve`，append 会把嵌套证据 UUID 和
  目标稳定 ID 转换为 JSON 字符串。WorkBuddy Skill 固定优先级为“本轮新 Excel → 已有完整文件名 → 提示词
  新建”，并明确预览阶段只读、用户确认和正文提交后才创建/注册/追加/导出。
- 插件清单和市场清单已同步到 `0.1.15`，生成安装包
  `integrations/workbuddy/file-agent-marketplace-0.1.15.zip`；SHA-256 为
  `ED4D059B8F1F7E7EE40A8F9C39835CA6F61AAFA49DC3280B3B66D9D45D84F49C`。

自动验证：

```powershell
$env:PYTHONPATH='apps/api'
python -m pytest apps/api/app/tests/test_structured_datasets.py `
  apps/api/app/tests/test_alembic_migration_graph.py `
  apps/api/app/tests/test_document_extraction_api.py `
  apps/api/app/tests/test_file_lifecycle_storage.py `
  apps/api/app/tests/test_file_lifecycle.py::test_rename_move_trash_and_restore_only_change_working_copy -q
# 23 passed, 2 Alembic 配置弃用 warnings

$env:PYTHONPATH='apps/mcp'
python -m pytest apps/mcp/tests -q
# 94 passed, 2 skipped

python -X utf8 C:/Users/zhouhexin/.codex/skills/.system/skill-creator/scripts/quick_validate.py `
  integrations/workbuddy/file-agent-plugin/skills/file-agent
python -X utf8 C:/Users/zhouhexin/.codex/skills/.system/skill-creator/scripts/quick_validate.py `
  integrations/workbuddy/file-agent-plugin/skills/file-agent-structured-dataset
# 两个 Skill 均 valid
```

自动测试覆盖上传 Excel 已有行保留、真实标题读取、指定/生成文件名、同名和多工作表冲突、按文件名解析、
稳定 ID/版本冻结、证据 UUID 序列化、改名后续写、单一当前文件及多 DocumentVersion。当前尚未执行规范第 8 节
列出的真实 WorkBuddy GUI 五组验收；安装 0.1.15、执行迁移并完全重启 API/MCP/WorkBuddy 后，仍须使用无隐私
样本逐项核对，不能把上述自动测试记为真实宿主验收。

## 2026-09-21：WorkBuddy 图片宿主 OCR 边界（0.1.16 方案已被下述修正取代）

- WorkBuddy 附件和本地目录图片在外部 OCR 开关不可用时关闭式失败，不再进入归档后的服务器 OCR。
- `workbuddy-document-v1` 图片预览和提交、旧 `workbuddy-ocr-v1` 页面提交均只接受
  `provider_name=tencent-docs-ocr`；模型视觉、通用读取和 File Agent 内部 OCR 不能冒充结果来源。
- Word/PDF 的既有 WorkBuddy 文档读取边界保持不变；普通网页/API 非 WorkBuddy 图片仍使用部署配置的 OCR Provider。
- 插件 Skill 已固定图片调用 `tencent-docs ocr.extract --accurate --positions`，失败时停止，不降级。
- 套件版本更新为 `0.1.16`，生成
  `integrations/workbuddy/file-agent-marketplace-0.1.16.zip`，并同步刷新既有市场地址使用的
  `file-agent-marketplace.zip`；二者 SHA-256 均为
  `566F20024106D247D5D055B9A236994C3B1AC626A019F821864EE4F32EE19CB3`。
- 自动验证：API 外部 OCR/完整文档提取及重复确认关键路径 14 项通过；扩展文件生命周期/集成导入回归中
  先发现并修复重复确认分支调用位置问题，其余 95 项通过，修复后的两个失败用例均已单独复测通过；MCP 全量
  94 项通过、2 项跳过；两个插件 Skill 校验通过；Python compileall 与 `git diff --check` 通过。真实 WorkBuddy
  `tencent-docs` OCR 宿主票据仍需安装 0.1.16 后执行 GUI 烟测。

## 2026-09-21：WorkBuddy OCR Provider 中立修正（0.1.17）

- 修正 0.1.16 过度约束：WorkBuddy 上传图片仍必须在 WorkBuddy 宿主侧 OCR，但不固定
  `tencent-docs ocr.extract` 或 `tencent-docs-ocr`。
- 完整文档和旧分页 OCR 接口接受任意符合 schema 的非空 `provider_name`，并把实际来源写入
  预览摘要、提取运行与页面元数据，不再设置 Provider 白名单。
- 关闭式保护保持不变：WorkBuddy 图片无法创建外部提取任务时，禁止继续归档并调用
  File Agent 内部 OCR。普通网页/API 非 WorkBuddy 上传仍按部署 OCR 配置执行。
- 插件 Skill 改为在 WorkBuddy 已安装且已授权的 OCR 能力中动态选择，先读取实时 schema，
  再回填实际 Provider；没有可用 OCR 时报告阻塞，不向服务器 OCR 降级。
- 套件版本更新为 `0.1.17`，生成 `file-agent-marketplace-0.1.17.zip` 并同步刷新
  `file-agent-marketplace.zip`；二者 SHA-256 均为
  `437296D1131AFB10D44ABCC8DF7EE1D3137690D55B25DDE0A0387A4E42729FE7`。
- 自动验证：API 外部 OCR/完整文档提取 12 项通过；文件生命周期与集成导入 100 项通过；
  MCP 全量 94 项通过、2 项跳过；两个插件 Skill 校验通过。真实 WorkBuddy GUI 仍需在安装
  0.1.17 后用当前环境中实际可用的 OCR 能力烟测。

## 2026-09-21：PDF 盖章属实项提取 Skill（0.1.18）

- 新增 `file-agent-sealed-item-extraction`，仅在用户明确要求只提取盖章/签章声明确认的 PDF 编号项目时启用。
- 每个可写入项目必须同时具备签章区域、属实声明、可唯一映射的编号范围和项目原文。
- 证据不完整、编号缺失/重复/越界、“以上属实”无法唯一绑定时只列入 `review_items`，不进入 Excel。
- OCR/版面工具和 Provider 由 WorkBuddy 实际能力选择，不固定为任何厂商；但必须能返回可验证的签章区域，
  不得回退 File Agent 内部 OCR 或仅凭文本猜测。
- 详细证据、范围展开、预览确认与 Excel 映射契约见
  [WorkBuddy 盖章项目属实范围提取 Skill 规范](2026-09-21-workbuddy-sealed-item-extraction-skill.md)。
- 套件版本更新为 `0.1.18`，生成 `file-agent-marketplace-0.1.18.zip` 并刷新通用市场包；
  二者 SHA-256 均为 `E73B1BF7A11328DA71B6320AA160C89D11E1B2546C68E3928C3EF6AAC3AEB869`。
- 三个 WorkBuddy Skill 结构校验通过；MCP 全量 94 项通过、2 项跳过。真实印章、单范围、
  多范围和模糊范围 PDF 仍需安装 0.1.18 后在 WorkBuddy GUI 中验收。

## 2026-09-21：WorkBuddy 多行任务文字匹配修复（0.1.19）

- 根因：Hook 提交单保留多行 prompt，WorkBuddy transcript 可直接删除换行；MCP 旧 `_normalize()`
  把换行折叠为单个空格，仍会将“上一行 下一行”和“上一行下一行”判定为不同。
- 修复：可信任务文字匹配忽略所有 Unicode 空白，非空白字符序列仍必须完全一致。
- 安全边界不变：仍校验固定会话、短时提交单、transcript 授权根、真实附件引用、时间窗与唯一候选；
  任何非空白文字变化都继续拒绝。
- 新增单元回归和 Hook -> transcript -> MCP -> 文档快照导入端到端回归，覆盖多行文字被宿主删除换行的场景。
- 套件版本更新为 `0.1.19`，生成 `file-agent-marketplace-0.1.19.zip` 并刷新通用市场包；
  二者 SHA-256 均为 `2B31B33C3C9996B523C572CA3BBE2CEDC8FB8EA8412BCF7A29EAED9A3959E716`。
- MCP 全量回归 97 项通过、2 项跳过。

## 2026-09-21：claim 结构化输出、唯一预览源与旧版 XLS 读取（0.1.20）

- 根因一：`document_extraction_claim` 虽然构造了 `structuredContent`，但工具没有声明可验证的输出模型，
  MCP 工具清单中的 `outputSchema` 为空，宿主无法可靠发现租约、固定版本、哈希、保存策略和已校验资源字段。
- 修复：新增严格 `DocumentExtractionClaimOutput` 输出模型，并以
  `Annotated[CallToolResult, DocumentExtractionClaimOutput]` 注册 claim；工具清单现在公开非空
  `outputSchema`，同时继续用 `ResourceLink` 交付宿主本地已校验资源。
- 根因二：Skill 虽要求展示 preview，但没有完整禁止本地 Markdown、临时 JSON/脚本或直接 API 重建，
  也没有明确认证配置的安全失败边界。三个相关 Skill 现统一规定
  `document_extraction_preview.preview_result` 是确认前唯一展示来源；claim/preview 结果不完整时停止，
  禁止读取、打印或搜索 `mcp.json`、连接器配置、环境变量、token/API key，也禁止 curl、Shell、
  临时脚本或 File Agent 私有 API 绕过 MCP。
- 旧版 `.xls` 与 `.xlsx` 现由完整文档任务返回 `source_kind=SPREADSHEET`；WorkBuddy 使用真实电子表格
  读取能力生成每工作表一个 `SHEET` unit，保留 `sheet_name`，`TABLE_CELL` 必须携带真实行列索引。
  电子表格不得转图片 OCR，也不得由模型、自编 BIFF/OLE 脚本或 File Agent 内部解析器补写。
- 结构化数据来源投影保留 `sheet_name`，使后续 Excel 字段证据能定位到真实工作表。
- 套件版本更新为 `0.1.20`，生成 `file-agent-marketplace-0.1.20.zip` 并刷新
  `file-agent-marketplace.zip`；二者 SHA-256 均为
  `2EF074D9B172E09284FFEC8DAAED61D87FAAD772359561A7903C12DCB92CAE50`。
- 自动验证：API 文档提取与结构化数据集 20 项通过；MCP 全量 98 项通过、2 项跳过；三个 WorkBuddy
  Skill 通过 `skill-creator` 结构校验；安装包内两份清单均为 `0.1.20`，且不包含 `__pycache__`。
- 尚未冒充完成真实宿主验收：安装 0.1.20、重启 MCP/WorkBuddy 后，仍需分别用一个 `.xls` 样本和一个
  显式提取样本验证 WorkBuddy 的真实电子表格读取器、preview 展示及确认后保存流程。

## 2026-09-21：claim `source.resource_url` 输出模型修复（0.1.21）

- 0.1.20 的 `DocumentExtractionClaimOutput` 使用 `extra=forbid`，但其嵌套 source 模型漏声明后端 claim
  实际返回的相对 `resource_url`。MCP 客户端下载和哈希校验已经成功后，FastMCP 在输出校验阶段因此返回
  `source.resource_url Extra inputs are not permitted`，WorkBuddy 无法取得 claim 结果。
- `DocumentExtractionClaimSource` 现显式声明必需 `resource_url`。它是固定任务的后端相对下载路由，不是
  token；真正下载仍由 MCP 客户端携带短期租约完成，Skill 仍禁止 WorkBuddy 绕过 MCP 直接调用该路由。
- 回归测试由内部 `_tool_manager.call_tool(..., convert_result=False)` 改为公开 `mcp.call_tool(...)`，确保
  FastMCP 实际执行 `outputSchema` 校验；fixture 使用与真实 API 一致的 source 字段集合。
- 套件版本更新为 `0.1.21`，生成 `file-agent-marketplace-0.1.21.zip` 并刷新通用市场包；二者
  SHA-256 均为 `DA24712C8DEBAB5DCCC1688F93CB3461D65B7D1F27511508B033ADA63BC0E38E`。
- 针对性 MCP 与插件清单测试 13 项通过。0.1.20 应由 0.1.21 替代，不再用于真实宿主验收。

## 2026-09-21：WorkBuddy 丢弃结构化结果的兼容修复（0.1.22）

- 真实宿主调用显示：0.1.21 的 MCP claim 已成功领取和校验，但当前 WorkBuddy 只向 Agent 提供第一条
  `TextContent` 摘要，没有暴露 `structuredContent` 和 `ResourceLink`，导致租约、固定版本、哈希和本地资源
  引用在宿主侧不可用。随后直接读取原始本地 PDF 的行为不属于允许降级，必须由 Skill 明确禁止。
- claim 现先用 `DocumentExtractionClaimOutput` 严格校验完整结果，再把同一个 `model_dump(mode="json")`
  同时作为 `structuredContent` 和第一条 `TextContent` JSON 返回，并继续附带 `ResourceLink`。两种 JSON
  表示逐字段一致，包含 `source.resource_url` 与已校验的 `source.resource_uri`。
- Skill 优先读取 `structuredContent`；宿主不暴露时，只允许读取同一次 claim 的第一条完整 JSON。
  一句摘要、字段缺失或 Agent 自行重建均失败关闭；禁止读取配置、环境变量、私有 API 或绕过任务直接读取
  宿主原始附件。
- 回归测试解析 claim 第一条文本 JSON，并断言它与 `structuredContent` 完全相等且包含租约和本地资源引用。
- 套件版本更新为 `0.1.22`，生成 `file-agent-marketplace-0.1.22.zip` 并刷新通用市场包；二者
  SHA-256 均为 `E305E371C2794CA8F6AD8C83653F4F88B3A372383BAC6F9C8A8C7B1C4E5344C3`。
  MCP 全量 98 项通过、2 项跳过，两个受影响 Skill 结构校验通过，`git diff --check` 无错误。
  真实 WorkBuddy 仍需在更新 MCP 和插件、完全重启后复测同一任务。

## 2026-09-21：带圈序号、整项审批与勾选辅助（0.1.23）

- `file-agent-sealed-item-extraction` 支持阿拉伯数字、中文数字及 `①②③…⑳` 带圈数字的确定性规范化；
  原始编号字符继续保留在预览和 evidence metadata 中，模糊字形不猜测。
- 审批声明未列出具体子选项，但声明、签章和版面能唯一绑定某一编号项时，该完整编号项及其全部子选项
  视为审批通过并作为一个 `verified_item`，不能任意截取。
- 声明区域包含多个编号项且文字无法区分时，允许使用紧邻编号前、同一行或同一选项框内的
  `✓/√/☑/可验证手写勾` 辅助选择；仅勾选项通过。勾选不能替代签章或审批声明，位置不清、冲突、
  涂改或无法唯一绑定时仍进入 `review_items`。
- preview 必须展示原始编号样式和判定方式；使用勾选辅助时还必须展示勾选位置证据，用户确认前不保存。
- 规范文档新增带圈范围、整项审批和勾选辅助三组验收用例。插件静态契约测试 9 项通过，Skill 结构校验通过。
- 套件版本更新为 `0.1.23`，生成 `file-agent-marketplace-0.1.23.zip` 并刷新通用市场包；二者
  SHA-256 均为 `A5BDE30955F7E6836F7D830220E17C77987CD9DA7005AB21471A1555579E2F9C`。

## 2026-09-21：Excel 对勾展示值与复核分流（0.1.24，历史规则）

- 盖章审批 Skill 为每个 verified 且实际写入的审批内容文本生成独立 `excel_value=source_text+√`；整项通过时
  每个具体文字分别加勾，部分通过时只给对应文字加勾。review、未勾选、范围外和证据不足内容不加勾、不写入。
- 新增对勾只属于 Excel 展示派生值；OCR 原文、unit text、证据 quote 和 evidence ref 保持原样。重试和幂等
  重放只保留一个末尾 `√`，编号、页码、日期、金额和坐标等非审批内容字段不自动加勾。
- 显式提取按本轮提示词冻结两条路径：明确要求先预览/复核/确认或未说明时为
  `REVIEW_REQUIRED + CONFIRM_BEFORE_SAVE`；明确要求无需复核/确认、直接生成并保存/写入时为
  `DIRECT_SAVE + DIRECT_ARCHIVE`。表达冲突先澄清，执行中不得静默切换。
- `DIRECT_SAVE` 仅跳过 preview/confirm，仍执行真实宿主读取、相同证据筛选、目标文件与 schema 解析、
  同名/版本冲突校验、append/query/export 和逐项回执。review 项自动排除；没有 verified 项时不导出标题空表。
- 三个相关 Skill、显式提取分流方案、结构化数据 Demo 和盖章 Skill 规范已同步；插件契约测试 9 项通过，
  三个 Skill 结构校验通过。
- 套件版本更新为 `0.1.24`，生成 `file-agent-marketplace-0.1.24.zip` 并刷新通用市场包；二者
  SHA-256 均为 `DAD1F1D21C09458D4AC3A22156B573942A2D71CAFC8826991CB8F9FBF1E1879A`。

> 0.1.24 的“小项逐项加勾”规则已由 0.1.26 的“标题级加勾”规则取代，不得继续用于新任务。

## 2026-09-22：签字盖章双证据、残缺声明与无编号定位（0.1.25）

- verified 项现必须同时具备可定位签字和盖章；只有签字没有章、只有章没有签字均不算审批通过。该规则收紧
  0.1.24 以前把签章区域作为单一证据的行为，直接路径同样不能绕过。
- 声明文字残缺时只保留确定片段，不用上下文或模板补字。能够独立确认的项号、端点和内容可继续使用；
  “属实/审批通过”事实本身不可确认时整组待复核，末项兜底不能修复审批事实。
- 未识别出序号时，从同一栏第一条真实数据行按 1 开始，排除表头、空行、说明和续行；当前页为续页时向前
  查找之前页面中的同栏起始行并跨页连续计数，不能在每页重置。
- 绑定优先级为：明确范围 → 唯一整项 → 编号前勾选 → 同栏跨页行序 → `LAST_ITEM_FALLBACK`。
  末项兜底只在候选均属于同一栏、同一签字盖章审批区域且顺序确定时选择候选集合最后一项，并记录全部候选、
  理由和位置；不能跨栏、跨审批区域或选择整份文件最后一项。受控范围/顺序也无法确定时仍待复核。
- 带圈序号、整项通过、对勾 Excel 展示值、复核/直接保存分流和上传 Excel 优先 schema 均保持不变。
- 契约测试 9 项、MCP 全量 98 项通过（2 项跳过）且 Skill 结构校验通过。套件版本更新为 `0.1.25`，生成
  `file-agent-marketplace-0.1.25.zip` 并刷新通用市场包；二者 SHA-256 均为
  `72A4A45FF47130147D5EA097008E0A35007E84E9605D4F3D0AB914CB459B533B`。

## 2026-09-22：标题级对勾与 Excel 落盘完成条件（0.1.26）

- `√` 从小项展示值改为标题级状态：只有标题下全部必需小项均 verified 且没有 review 项时才生成
  `title_excel_value=title_source_text+√`；所有小项字段保持不带新增对勾的 `source_text`。部分通过时标题不加勾。
- 预览和直接保存回执按标题展示全部必需小项、已验证小项和 `all_requirements_verified`，不得把源文件中的
  勾选证据直接复制为标题通过标记。
- 新建、更新或追加 Excel 的任务在 append/query 成功后必须调用 `structured_dataset_export`；只有 export 返回
  完整 `working_copy_id`、`document_id`、`document_version_id`、`version_number` 和 `filename` 后才能报告完成。
- 2026-09-22 10:17 的真实调用停在 `structured_dataset_resolve` 的 HTTP 422，未调用 create、append 或 export，
  因而没有生成本轮 Excel。运行时根目录来自 `.env` 的 `WORKING_COPY_STORAGE_ROOT=./storage/working-copies-test1`，
  并非默认 `storage/working-copies`；检查发现仅有 2026-09-20 的隐藏历史版本，当前同名工作副本缺失。
- 新增回归测试会主动移除测试环境中的当前同名文件，再对同一 revision 重复 export，验证后端能够从不可变
  版本重新发布当前文件。WorkBuddy MCP 全量测试 `98 passed, 2 skipped`，插件契约测试 `9 passed`，目标数据集
  回归测试通过，三个 Skill 均通过结构校验。
- 套件版本更新为 `0.1.26`，生成 `file-agent-marketplace-0.1.26.zip` 并刷新通用市场包；二者 SHA-256 均为
  `BFF243F5573496E843EDEF2A3BA017D150426A9BF38E17C2B32691C3E45E2B20`，包内不含 `__pycache__` 或 `.pyc`。

## 2026-09-22：旧版 XLS 累计目标 Skill 编排（0.1.27）

- 新上传目标优先级从“唯一 `.xlsx`”扩展为“唯一 `.xls` 或 `.xlsx`”；同时上传多个电子表格时必须选择，
  不得静默优先 `.xlsx` 或按上传顺序判断。
- `.xls` 内容仍由 WorkBuddy 真实电子表格能力读取。复核预览同时冻结源 `.xls`、最终 `.xlsx`、工作表、标题、
  已有行和格式提升说明；`DIRECT_SAVE` 也不能跳过转换、冲突和 schema 校验。
- Skill 仅在 `structured_dataset_create_from_excel` 的真实工具 schema 提供 `extraction_run_id`、
  `legacy_target_policy="CONVERT_TO_XLSX"` 和异步任务结果时启动格式提升；后端尚未实现时安全失败并要求用户另传
  `.xlsx`，禁止用 `structured_dataset_create`、模型、Shell 或临时转换重建近似目标。
- 转换任务必须以固定 `filesystem_job_id` 使用 `job_get`；完成后用原参数和原幂等键重试注册。只有返回完整
  dataset/working copy/document/source version/current version/filename 且 `converted_from_legacy=true` 后才能 append。
- 目标最终只能有一个用户可见 `.xlsx`，原 `.xls` 仅作为历史来源版本；最终 export 回执必须说明文件名转换和
  后续应使用的 `.xlsx` 名称。主附件 Skill 同步增加同一安全失败边界。
- 本轮只更新 Skill、契约测试、说明和插件包；方案中的服务端格式提升、MCP 新参数及异步 job 尚未实现，不得把
  0.1.27 描述成已经解除后端 `.xlsx only` 限制。
- 插件契约测试 `9 passed`、MCP 全量 `98 passed, 2 skipped`，三个 Skill 结构校验通过。套件版本更新为
  `0.1.27`，生成 `file-agent-marketplace-0.1.27.zip` 并刷新通用市场包；二者 SHA-256 均为
  `FD189847E07FD5CC5988E4DFF8FB4FE05BDD80982B5E62551C5A5975596FC6FA`，包内不含 `__pycache__` 或 `.pyc`。

## 2026-09-22：印章信号判定更新（0.1.30）

- 盖章审批 Skill 明确盖章信号可来自三类宿主识别结果：带“圆章”OCR 文本的图像元素、印章检测区域，或图内红色圆形印记检测结果。
  每类信号都必须关联 PDF 页码；本版本已由 0.1.31 的“签字非必需”规则取代。
- 只有印章信号不代表项目通过；本版本关于签字的通过条件已由 0.1.31 取代。
- 套件版本更新为 `0.1.30`，生成 `file-agent-marketplace-0.1.30.zip` 并刷新通用市场包；二者 SHA-256 均为
  `8B118780E1AB86CF462F1E9EEDCF3589B4CD0ED830B84E4EBC583E9C6F97C644`，包内不含 `__pycache__` 或 `.pyc`。

## 2026-09-22：盖章审批规则改为签字非必需（0.1.31）

- 可写入项目固定需要审批声明、可验证盖章证据和唯一项目映射；签字可作为辅助信息，但不再是必需条件。
- 保留三类盖章信号及 PDF 页码要求。没有盖章、审批声明或唯一项目映射时仍进入待复核，不写入 Excel。
- 套件版本更新为 `0.1.31`，生成 `file-agent-marketplace-0.1.31.zip` 并刷新通用市场包；两者 SHA-256 均为
  `8F2F14E5E15F299619695D38D786C25E7A5C3142C663C17ABAD640C25793F103`；包内不含 `__pycache__` 或 `.pyc`。
