# 文件助手 WorkBuddy 附件桥接

当前套件 **0.1.84**：上传更新后的成果明细表时，支持两种明确任务：只刷新余额表，保持来源表不变；或先同步来源表“汇总”页并发布同名新版本，再刷新余额表。两种任务都优先核对个人页 J 列逐项金额与“合计”，余额表的支出只由已有登记重新计算。需同步部署本版 API/MCP 代码并让 WorkBuddy 更新插件；无需数据库迁移。

0.1.83：新增来源表“汇总”页同步并发布同名新版本，随后可用该版本刷新余额表。

0.1.81：上传新版教师成果明细汇总表，可按姓名刷新已发布余额表的科研成果资助总额；总支出仍由现有个人登记汇总，首页和个人页同步重算。新版回传仍以个人明细页为准。

0.1.80：新版成果资助余额表首页每位教师只显示资助、支出和余额汇总；逐笔登记放在个人页，各人序号从 1 开始。下载后在个人页核对、修改、删除或新增登记，再同名回传，服务端重算首页。已发布旧版表继续按旧版首表规则回传，其他 Excel 回传不变。

0.1.79：成果资助余额表同名回传可在首表中间或末尾加入独立支出，新行 A 列留空；服务器按顺序重新编号、扣减余额并更新个人页和来源记录。已有行仍使用下载时的 A 列编号。

0.1.77：成果资助余额表支持删除首张汇总页中的完整登记行后同名回传，服务器重算余额、同步个人页并更新活跃来源和删除审计。

0.1.76：成果资助手写登记支持 `269+6560+2030.+3885+17250` 等明确加法式，逐项核对后求和；整式引用和逐项引用都可由服务端验证，不能重复扣减。

0.1.75：成果资助使用登记支持按同名余额表追加新图片、继续扣减已发布余额；下载后只修改汇总页即可回传，服务端同步个人页。教师引进汇总表仍按本轮是否上传目标 Excel 选择新表或同名追加。须同步部署对应 API/MCP 代码。

0.1.71：教师引进汇总表 L 列每篇只要有已确定等级，就把学校 A++/A+/A/B 或正式来源的 CCF A/B/C 等级排在该篇序号之后、其他事实之前。判级和证据规则不变。须同步部署更新后的 API，再安装本版插件；已发布的旧 Excel 不会自动重排。

0.1.70：教师引进汇总表 L 列按每篇论文判断 CCF。正式来源已有 CCF 标签的篇目保留 CCF、分区和被引等原文，不再匹配 2019 版共享目录或追加学校等级；同列其他篇目继续按目录判级。须同步部署更新后的 API，再安装本版插件。

0.1.69：新增数据格写入 Excel 前将 `<br>`、`<br/>`、`<br />`（大小写不敏感）替换为换行；服务端展示和发布核对只忽略等价换行及多余空白，行高与自动换行样式不足不再单独阻止发布。原文、证据、论文等级与旧格保护仍保持校验。须同步部署 API/MCP 更新后安装本版插件，已发布的旧 Excel 不会自动重写。

0.1.68：教师引进汇总表 L 列明确分区须作为 `ZONE` 提交，期刊 ISSN/刊名在 L 中提供正式来源引用；服务端支持 `SCI/A&HCI/SSCI中科院1 区` 等组合写法，并拒绝纯文本漏标。须同步部署 API 更新后再安装本版插件；已发布的旧 Excel 需通过同名手工修订回传更正，不得重复追加旧人员。

0.1.67：教师引进汇总表新增 I–P 受控列内格式；L 列通过只读 `reference_catalog_get/query` 使用系统共享的 2019 版目录。须同步部署 API/MCP、执行 `20261009_0002` 迁移并在服务器运行 `python -m app.modules.reference_catalogs.seed`；填表时不上传目录。新增行的多行内容自动设置行高，手工补全仍由原修订 Skill 回传。

0.1.65：新增 `file-agent-teacher-recruitment-excel`，按上传教师引进汇总表的真实列填表，同一位教师的多份材料合并为一行，后续按完整文件名追加。该流程依赖同步部署 API/MCP 的 `TEMPLATE_ALL` 多来源写回契约与 `20261009_0001` 迁移；手工补全仍由 `file-agent-manual-excel-revision` 回传。

0.1.64：移除 `file-agent-structured-dataset` Skill 及其任务参数参考，清理附件桥接的旧累计表路由和打包依赖。教授汇总表 B–I/L 与按文件名追加仍用 `file-agent-a-i-scoped-excel`，手工补全同名 Excel 回传仍用 `file-agent-manual-excel-revision`；通用完整 Excel 写回由现有 `file-agent-excel-writeback` 承担。服务端 `structured_dataset_*` 工具与 API 本次未删除。

0.1.63：移除 `file-agent-sealed-item-extraction` Skill 及其列来源表，清理现行入口对它的引用。教授汇总表继续使用 `file-agent-a-i-scoped-excel` 的 B–I 与 L 定向流程；盖章属实项目提取、A–AA 全列及印章核验请求报告当前插件不支持，不自动缩小任务。

0.1.62：移除 `file-agent-full-professor-excel` Skill。普通教授汇总表追加继续使用 `file-agent-a-i-scoped-excel` 的 B–I 与 L 定向流程；明确要求 A–AA 全列及印章核验时报告当前插件不支持，不自动缩小任务。

0.1.61：`file-agent-a-i-scoped-excel` 的 B–I 允许逐列留空，已有值仍须逐字引用来源，缺失列标明 `SOURCE_FIELD_NOT_FOUND` 或 `OCR_UNREADABLE`，不阻止该来源追加一行。PDF L 列仍只看物理第 25 页。

0.1.60：PDF L 列只看物理第 25 页。页数不足或第 25 页裁剪图没有真实考核栏目时 L 留空，不再向前找页。新任务为 v4；未完成 v2/v3 可用 `scoped_pdf_fixed25_upgrade` 重签，旧租约失效。

0.1.59：已签发但未完成的 v2 固定末页任务可用 `scoped_pdf_review_discovery_upgrade` 受控重签为 v3。该倒序流程仅为历史兼容，当前 Skill 不再调用。

0.1.58：`file-agent-a-i-scoped-excel` 的 L 列从 PDF 实际末页向前逐页定位“任现职以来的考核情况”，找到后冻结真实页码和栏目范围；优先用 PDF 文字层，扫描件每次仅向 OCR 发一页定位图。命中后复用完整的定位 OCR 文本或只补一次栏目 OCR；查至首页未找到则 L 留空并记录原因。

0.1.57：未指定列范围的“把 PDF/Word 信息追加到已发布的教授业绩汇总 `.xlsx`”默认由 `file-agent-a-i-scoped-excel` 处理 B–I 与 L；只有本轮明确要求 A–AA 全列同时填写且核对印章时才进入 `file-agent-full-professor-excel`。插件 Skill 的简介、入口前置条件及桥接路由均已同步，避免仅凭“已有表追加、直接保存、给链接”误选整篇 PDF 的全列流程。

0.1.56：原 `file-agent-a-i-scoped-excel` 增加物理 L 列，只从“任现职以来的考核情况”提取考核优秀年度。新版 PDF 只 OCR 第 1、3 页和末页三个冻结区域，B–I 与 L 同轮写入；无优秀或文字不清时 L 留空并标记，J、K、M–AA 不处理。首次一份 Excel 与 1–20 份 PDF/Word，后续按完整 `.xlsx` 文件名追加并发布。需同步部署 API/MCP、重启服务并安装新版插件；无需数据库迁移。

0.1.55：新增 `file-agent-full-professor-excel`，首次可用一份 Excel 与 1–20 份 PDF 填写 A–AA 全列，后续只上传 PDF 并按完整 `.xlsx` 文件名续写。PDF 正文一次读取并复用页证据；模糊 OCR 在 Excel 中明确标记，不触发重复识别，也不计入核验或对勾。完整行同轮一次发布。

0.1.54：`file-agent-manual-excel-revision` 支持把与本轮上传文件完全一致的服务器可见 Excel 直接编辑结果登记为新版本。用户基于已发布 Excel 手工补全后，本轮上传同名 `.xlsx`；WorkBuddy 用编辑前冻结的版本 ID/SHA 回传，服务端发布为同一工作副本的新版本，旧版保留。

0.1.52：A–I 定向 PDF 支持已核对的 25 页参考版式与 22 页 A4 版式，仍只 OCR 第 1、3 页的 B–I 区域。服务端修复版式不符时的归档无限重试，并能补偿旧版留下的等待态。

0.1.51：`file-agent-a-i-scoped-excel` 一轮支持 1–20 份 PDF/DOC/DOCX，每份来源填写一行；首次另带一份 Excel，后续在任务文字中写明完整已发布 `.xlsx` 文件名。`scoped_excel_sources_ingest` 按可信附件顺序绑定来源，PDF 仍只做 B–I 定向 OCR，Word 使用真实正文证据；所有行一次写入并发布同一目标新版本。

0.1.50：首次上传 PDF + Excel；后续只上传一份 PDF 时可通过完整目标文件名续写，自动补 A 序号并安全扩展数据区。

0.1.49：A–I 任务调用 `excel_writeback_apply` 时，`rows` 每行只传 B–I 八个值；MCP 根据冻结目标列数补齐 A 与 J–AA 的 Python 空值，避免 WorkBuddy 宿主改写显式 `null` 占位。空白字段在 `payload` 中只传 `blank_reason`。

0.1.48：A–I 任务中，B 列引文若为“单位 计算机科学与工程学院”，OCR 引文保留完整标签，提交值、候选值和 Excel B 格只填写“计算机科学与工程学院”；服务端在 OCR 提交与写回发布时校验。

0.1.47：A–I 流程冻结新上传 XLS 目标时必须传真实 `sheet_name` 与 `layout.header_range/data_range`；缺失布局的复杂模板由服务端入队前返回 `TARGET_EXCEL_LAYOUT_REQUIRED`，不再先耗时转换后给出笼统失败。失败后修正参数须使用新的 `request_key`。

0.1.46：新增 `file-agent-excel-grid-archive`，把 WorkBuddy 真实逐格网格转换为符合服务器契约的 SHEET unit 与表头 TABLE_CELL 坐标；A–I、盖章模板和累计表流程在目标 Excel 归档前引用它。多行表头保留真实文字和字符偏移，A–I 约束仅在定向任务启用。

0.1.45：本轮上传 Excel 与唯一已发布目标同名时，WorkBuddy 继续以上传表为新目标；服务端发布成功后归档旧目标，按文件名只命中新表。归档工作副本的文档与版本 ID 可搭配本轮上传暂存版的提取 run ID，服务端严格校验来源并映射到归档复制 run。`file-agent-a-i-scoped-excel` 仍只裁剪 PDF 第 1、3 页的 B–I 相关区域，J–AA 不参与 OCR 和写入。需同步部署对应 API 代码、重启 API 与文件队列 worker。

0.1.41：Excel 新建、按文件名续写和已发布行定向更正默认经证据与版本校验后直写；
仅用户明确要求先复核时等待确认。重复上传仍须展示候选对比链接并等待用户选择。
本轮有新上传目标 Excel 时必须注册独立新目标，不能按旧文件名追加；只有没有新目标上传时才按
文件名读取旧表字段并续写。此版同时更新 API/MCP 默认参数；安装新套件、更新服务并重开 WorkBuddy 会话后生效。

0.1.37：新增并行的 `file-agent-excel-writeback` Skill 与 `excel_writeback_*` MCP 工具。
由 WorkBuddy 本机定向修改冻结的 XLSX 副本，File Agent 核对来源、目标版本和工作簿差异后发布同名新版本；
旧 `structured_dataset_*` 路径不变。启用时 API 与 MCP 都设置 `WORKBUDDY_EXCEL_WRITEBACK_ENABLED=true`，
并保留 `WORKBUDDY_STRUCTURED_DATASET_DEMO_ENABLED=true`；需部署最新 API/MCP，重启 API/MCP 进程，
再安装 `file-agent-marketplace-0.1.37.zip` 并重启 WorkBuddy。当前新路径只处理已注册的 `.xlsx` 目标；
新上传 Excel 先受控注册，`.xls` 先由服务器完成格式提升。真实 WorkBuddy 宿主效果仍需安装后验收。

0.1.36：盖章项目专用 Skill 默认在真实读取与证据校验后直接发布 Excel，不等待人为复核；
用户本轮明确要求先复核时才进入确认路径。普通显式内容提取仍默认复核，OCR 复用逻辑未变。
0.1.35 修复完整提取状态错配和旧任务受控恢复，并要求先验收 Excel 网格再读取 PDF。
0.1.34 将专用模板物理 B–I 列视为身份列，免盖章审批和资格判断但保留原文证据。
0.1.33 已支持复杂 Excel 模板在预览前检查真实表头坐标、转换失败返回可行动终态。
0.1.32 已支持整栏属实无需明确项号、逐列候选预览/确认/commit 驱动服务器 Excel 发布，
完整列条件满足时只在人员数据格末尾加一次 √，不改表头、不逐小项加勾。签字仍非必需，Provider 不固定。
必须同步更新 API/worker/MCP 并迁移至 `20260922_0002`；仅更新 Skill 不会启用新任务接口。
安装 `file-agent-marketplace-0.1.37.zip` 后完全退出并重开 WorkBuddy、新建会话。
以本段和当前 Skill 为准；下面是历史变更说明，其中旧签字/标题文字规则已经失效。

打包：项目根运行 `python integrations/workbuddy/build_marketplace.py`。
本地升级服务：`python -m http.server 8175 --bind 127.0.0.1 --directory integrations/workbuddy`。
下载地址：`http://127.0.0.1:8175/file-agent-marketplace-0.1.62.zip`。

2026-09-22 盖章审批规则更新（历史套件 0.1.31）：审批声明、盖章证据和唯一项目映射成立即可，签字不再是必需条件。
盖章证据可由 `type=Image` 且 OCR 返回“圆章”的元素、印章检测区域或图内红色圆形印记检测结果提供；每项仍须带 PDF 页码。
安装 `file-agent-marketplace-0.1.31.zip` 后新建 WorkBuddy 会话。

2026-09-22 确认保存和复杂模板修复（套件 0.1.29）：MCP 预览缓存可用短参数提交，支持确认并保存；
租约过期受控恢复且不重做 OCR。复杂模板指定 `layout.header_range/data_range`，支持最多 40 列和完整长标题，
只填预留行，保留序号、合并表头与尾注。更新时先执行 `alembic upgrade head`，再重启 API、worker 和 MCP；
安装 `file-agent-marketplace-0.1.29.zip` 后新建 WorkBuddy 会话。代码测试不等于真实宿主验收。

2026-09-22 Excel 原样续写与落盘完整性修复（套件 0.1.28）：上传的 `.xlsx` 目标表改为在 OOXML 包内
只更新指定工作表、关联表格和筛选范围，保留其他工作表、样式、公式、合并单元格、数据验证、图表与绘图部件；
服务器只发布一个与用户所见同名的普通工作副本，并继续用 DocumentVersion 保存历史。导出前强制校验累计
revision 与成员链，存在断链或没有已确认记录时明确失败，不再把只有标题行的 Excel 报告为成功。更新时安装
`file-agent-marketplace-0.1.28.zip`，部署最新 API/MCP 代码并完全重启 API、队列 worker 和 WorkBuddy。

2026-09-22 旧版 XLS 累计目标编排（套件 0.1.27）：本轮唯一新上传 `.xls` 可以被规划为新目标，内容仍由
WorkBuddy 真实电子表格能力读取；预览必须同时展示源 `.xls` 和最终 `.xlsx` 文件名。submit 后只有当后端
`structured_dataset_create_from_excel` 真实提供受控转换参数与异步任务结果时才执行格式提升；当前后端能力
未启用时安全失败并要求用户另传 `.xlsx`，不得用模型、`structured_dataset_create`、Shell 或临时转换冒充成功。
更新时安装 `file-agent-marketplace-0.1.27.zip` 并完全重启 WorkBuddy。

2026-09-22 标题级对勾与 Excel 落盘闭环（套件 0.1.26）：`√` 只追加到“其下全部必需小项均已验证”的
标题末尾，小项文字始终保持原样；部分通过、存在待复核项或无法映射标题字段时，标题不加勾。新建、更新或
追加 Excel 的任务在 append/query 成功后必须执行 `structured_dataset_export`，且只有返回完整工作副本、文档、
版本和文件名标识后才能报告完成。更新时安装 `file-agent-marketplace-0.1.26.zip` 并完全重启 WorkBuddy。

2026-09-22 签字盖章双证据与无编号定位（套件 0.1.25）：盖章审批 Skill 要求签字和盖章同时存在，
只有签字或只有章均不算通过；残缺声明只读取可确定片段。未识别到序号时从同栏第一条真实数据行跨页连续
计数，必要时回查之前页面；仍有多个候选时，只能在同一栏、同一签字盖章审批区域内选择最后一项并记录
`LAST_ITEM_FALLBACK`，不能选择整份文件末项。更新时安装 `file-agent-marketplace-0.1.25.zip` 并完全重启 WorkBuddy。

2026-09-21 Excel 对勾与复核分流（套件 0.1.24，已由 0.1.26 的标题级规则取代）：该版本曾对每个 verified
审批内容文本生成 `原文字√`；0.1.26 起禁止给小项逐项加勾。原始 OCR 和 evidence quote 始终保持不变。
提示词明确要求
无需复核/确认或直接生成并保存时走 `DIRECT_SAVE`；明确要求预览/复核/确认或未说明时走
`REVIEW_REQUIRED`。直接路径仍排除 review 项并执行目标、同名和版本校验。更新时安装
`file-agent-marketplace-0.1.24.zip` 并完全重启 WorkBuddy。

2026-09-21 盖章审批项识别扩展（套件 0.1.23）：`file-agent-sealed-item-extraction` 新增
`①②③…⑳` 带圈序号规范化；审批声明未限定子选项但能唯一绑定编号项时，整项及其全部子选项视为通过；
文字无法区分具体编号项时，可用紧邻编号前且位置可验证的勾选标记辅助选择。勾选不能替代签章/审批声明，
位置不清或证据冲突仍进入待复核。更新时安装 `file-agent-marketplace-0.1.23.zip` 并完全重启 WorkBuddy。

2026-09-21 WorkBuddy 结构化结果兼容（套件 0.1.22）：针对部分宿主只保留 MCP 普通文本内容、丢弃
`structuredContent` 与 `ResourceLink` 的行为，claim 现在把同一份严格校验 JSON 同时放入
`structuredContent` 和第一条普通文本，并继续附带 `ResourceLink`。JSON 包含短期租约、固定版本、哈希、
保存策略和已校验 `source.resource_uri`；Skill 只允许使用这份 MCP 原样 JSON 作为兼容输入，禁止直接读取
原始本地附件、配置或私有 API。更新时安装 `file-agent-marketplace-0.1.22.zip`，加载最新 MCP 源码并完全
重启 WorkBuddy。

2026-09-21 claim 来源字段修复（套件 0.1.21）：补齐严格 MCP 输出模型中的后端相对
`source.resource_url`，避免 claim 已成功下载文件，却在向 WorkBuddy 返回结果时触发
`DocumentExtractionClaimOutput source.resource_url extra_forbidden`。回归测试改为走 FastMCP 对外调用路径，
确保实际执行 `outputSchema` 校验。更新时安装 `file-agent-marketplace-0.1.21.zip`，加载最新 MCP 源码并
完全重启 WorkBuddy。

2026-09-21 文档提取契约修复（套件 0.1.20）：`document_extraction_claim` 现在通过 MCP
`outputSchema` 明确返回租约、固定版本、哈希、保存策略和已校验资源；确认前只允许展示
`document_extraction_preview.preview_result`。插件禁止读取 `mcp.json`、连接器配置、环境变量或认证材料，
也禁止用 Shell、curl、临时脚本或私有 API 绕过工具。旧版 `.xls` 与 `.xlsx` 作为
`SPREADSHEET` 来源由 WorkBuddy 真实电子表格能力读取，并保留工作表名及单元格行列坐标。
更新时安装 `file-agent-marketplace-0.1.20.zip`，加载最新 API/MCP 源码并完全重启 WorkBuddy。

2026-09-21 多行任务文字匹配修复（套件 0.1.19）：修复 Hook 保留换行、WorkBuddy transcript 直接删除换行时，
MCP 把同一任务文字判定为不一致并驳回导入的问题。可信匹配现在忽略 Unicode 空白差异，但非空白字符序列必须
完全一致；固定会话、时间窗、附件宿主引用和唯一候选校验保持不变。更新套件后还必须加载最新 MCP 源码，
并完全重启 WorkBuddy。

2026-09-21 PDF 盖章属实项提取（套件 0.1.18）：新增 `file-agent-sealed-item-extraction` Skill。
当用户只要求把 PDF 中盖章声明“第 X 项至第 Y 项属实”覆盖的项目写入 Excel 时，Skill 必须
同时验证签章区域、属实声明、编号范围和项目原文；证据不完整的项目只展示待复核，不写入。
预览和用户确认规则、Excel 文件名续写与版本管理继续复用现有流程。更新时安装
`file-agent-marketplace-0.1.18.zip` 并完全重启 WorkBuddy。

2026-09-21 WorkBuddy 图片 OCR 执行边界修正（套件 0.1.17）：无论是直接归档还是显式字段提取，
WorkBuddy 入口中的图片都必须在 WorkBuddy 宿主侧通过当前可用的 OCR 能力识别，但不固定
`tencent-docs ocr.extract` 或 `tencent-docs-ocr`。提交时 `provider_name` 必须保留实际调用来源；宿主没有
可用 OCR 时任务保持阻塞或失败，不降级到 File Agent 内部 OCR、模型视觉或通用读取工具。
更新时安装 `file-agent-marketplace-0.1.17.zip`，部署最新 API/MCP 代码并完全重启 API、worker 和 WorkBuddy。

2026-09-20 Excel 目标表与按文件名续写（套件 0.1.15）：本轮新上传 `.xlsx` 优先成为新的目标表；
没有新 Excel 时可按完整文件名解析已有目标，或根据提示词字段创建指定名称的新表。显式提取仍须先展示实际
字段值和证据，用户确认并完成正文提交后才允许注册/创建数据集、追加和导出。服务器当前工作副本、文件列表和
下载名称保持一致，后续通过同名文件继续追加并创建同一 Document 的新版本。更新时安装
`file-agent-marketplace-0.1.15.zip`，部署最新 API/MCP 代码、执行数据库迁移并完全重启 WorkBuddy。

2026-09-20 预览展示与证据追加修复（套件 0.1.14）：`document_extraction_preview` 现在把仅供当前
WorkBuddy 会话展示和调整的完整 `preview_result` 返回给 Agent，发送到 File Agent 服务器的预览请求仍只有
结果指纹与计数，不含正文。Skill 强制展示实际字段值、原文和来源后才能询问确认。同时修复
`structured_dataset_append` 携带 `evidence_refs.source_unit_id` 时 Python UUID 无法 JSON 序列化的问题，
避免任务看似完成但 Excel 只有标题行。更新时安装 `file-agent-marketplace-0.1.14.zip`，部署最新 MCP 代码并
完全重启 WorkBuddy；本补丁不增加数据库迁移。

2026-09-20 显式提取确认补充（套件 0.1.13）：用户明确要求从附件提取正文、字段或表格时，插件以
`WORKBUDDY_DOCUMENT + CONFIRM_BEFORE_SAVE` 导入。WorkBuddy 先登记不含正文的结果指纹并展示逐文件
提取结果；用户在后续消息明确确认后，才提交完全相同的正文并继续累计表写入或导出。只要求归档、分类或
整理时仍使用 `DIRECT_ARCHIVE`，归档内部解析/OCR 不增加确认。更新时安装
`file-agent-marketplace-0.1.13.zip`，同时部署 API/MCP 新代码、执行数据库迁移并完全重启 WorkBuddy。

2026-09-20 单一累计表文件补充（套件 0.1.12）：`structured_dataset_export` 现在把累计 Excel 发布为
服务器 `WORKING_COPY_STORAGE_ROOT/structured-datasets` 下的一个普通工作副本。后续导出继续更新同一
逻辑文件并创建新的 `DocumentVersion`；文件列表不增加第二个同名文件，当前文件和版本信息均可通过
服务器接口读取与下载。WorkBuddy 的 `ResourceLink` 只是宿主交付副本，不是唯一保存位置。更新时安装
`file-agent-marketplace-0.1.12.zip`，同时部署 API/MCP 新代码并执行数据库迁移，然后完全重启 WorkBuddy。

2026-09-20 文档累计表补充（套件 0.1.11）：新增图片、DOC/DOCX、PDF 的宿主正文提取与累计 Excel
编排。连接器 1.0.3 同时开启 `WORKBUDDY_STRUCTURED_DATASET_DEMO_ENABLED` 和
`WORKBUDDY_DOCUMENT_EXTRACTION_ENABLED`，并允许可信的 `workbuddy_attachment_ingest`、
`workbuddy_submission_ingest` 入口正常注册。更新后必须重新安装本地市场并完全重启 WorkBuddy；仅更新
后端或 Skill 文件不会刷新正在运行的 stdio MCP 工具清单。

2026-09-13 重复对比展示补充（套件 0.1.10）：发现 `WAITING_DUPLICATE_CONFIRMATION` 后，Skill 必须先对
最新 review 的每个可对比候选调用 `duplicate_comparison_get`，在回复中逐项展示“打开对比页面”的
Markdown 链接，之后才能询问继续上传、使用已有文件或取消。该能力主动展示入口，但不会替用户自动打开
浏览器。更新时安装 `file-agent-marketplace-0.1.10.zip` 并完全重启 WorkBuddy；后端和 `mcp.json` 无需调整。

2026-09-13 分类回执补充（套件 0.1.9）：`batch_get.items[]` 对已发布成功文件返回当前生效的
`primary_category` 和 `classification_outcome`，插件按文件展示主分类路径及 `CLASSIFIED/OTHER`。
本次不在批次回执中增加分类证据和关键词。更新后需安装 `file-agent-marketplace-0.1.9.zip`，部署最新
后端代码并完全重启 WorkBuddy；配置键和 `mcp.json` 内容不变。

2026-09-13 TXT 附件补充（套件 0.1.8）：WorkBuddy 可信文档附件桥接新增 `.txt`，复用后端既有
纯文本解析、查重、归档与分类链路，不新增解析器。已安装旧版套件的电脑需要安装
`file-agent-marketplace-0.1.8.zip`、加载本项目最新 MCP 代码并完全重启 WorkBuddy；原有环境变量无需修改。

2026-09-13 中文称呼更新（套件 0.1.7）：用户可在上传附件的同一条消息中说“请用文件助手归档并分类本轮附件”，
或直接说“请归档并分类本轮附件”；不再要求输入英文产品名。仅上传、查看或讨论附件仍不会默认导入。
本次只更新 WorkBuddy 套件的 Skill、Hook 提示和用户可见文案；底层 `FILE_AGENT_*` 配置键、
`FILE_AGENT_HOST_SUBMISSION` 标记、MCP 工具名与后端接口均不变。已安装 0.1.6 的电脑需要安装
`file-agent-marketplace-0.1.7.zip` 并完全重启 WorkBuddy，单独修改服务器或 `mcp.json` 不会更新本机 Skill。

2026-09-12 文档桥接补充：本机 WorkBuddy 5.5.3 的 DOCX、PDF、XLS 真实消息没有 `image_blob_ref`，
但宿主会在用户正文之前的 `<attached_files>` 与 `<user_references>` 中同时列出本轮选中的本地文件。
新版 MCP 只在固定会话、任务文字和唯一消息匹配后解析这两段宿主上下文，生成绑定消息 ID 的附件标识，
再把文件流式快照到插件私有 `document-cache`，复用原有查重、归档、解析与分类上传链路。
用户正文中手写路径或伪造 `<attached_files>` 不会授权导入。该适配依赖 5.5.3 的本机记录格式，
不是 WorkBuddy 官方稳定附件 API；DOC、XLSX 已有自动化覆盖，仍需各自完成真实 GUI 验收。
该文档引用捕获能力从 0.1.6 沿用；中文称呼需重新安装 0.1.7 套件，并完全重启 WorkBuddy。

当前试点版本：**0.1.34**。0.1.6 在 0.1.5 的附件捕获修复上补齐 WorkBuddy 外部 OCR 编排：图片进入
`WAITING_EXTERNAL_EXTRACTION` 后，不再只查询状态，而是由 Agent 调用 `extraction_claim` 领取固定页，
使用 WorkBuddy 已启用的 OCR 能力识别，再通过 `extraction_submit` 回填实际 Provider，最后继续查重、
归档和分类。File Agent 后端不会因此启用内部 OCR。

0.1.5 已修复 Windows Hook 把宿主 UTF-8 管道按 GBK 解码导致的中文乱码；
捕获与调用校验 Hook 固定读取 UTF-8，输出使用 ASCII JSON 转义。无效 UTF-8、截断 JSON 或孤立代理项
会拒绝创建提交单。消息记录可早于提交单最多 30 秒（兼容实测约 6 秒的宿主附件准备耗时），仍须唯一匹配
固定会话、任务文字和已验证的宿主附件引用。未来时钟容差仍为 5 秒，提交单有效期仍为 10 分钟。

0.1.6 历史升级包：`file-agent-marketplace-0.1.6.zip`。升级套件后完全退出并重开 WorkBuddy，同时加载本项目最新 MCP
代码；`mcp.json` 环境变量、File Agent API 和 worker 本次不需要调整。只升级套件而未加载最新 MCP 时，
模型仍可能只查状态而不执行 OCR。旧版乱码提交单不能修复后重用，必须重新上传并发送任务。
这段仅记录 0.1.6 图片 OCR 升级；上面的文档桥接补充需要新增独立缓存授权根。

验收时用同一张图片分别测试拖拽和左下角添加，两轮使用不同测试编号，均须收到真实工具批次回执。
如果某种选择方式只在用户正文里生成本地路径、没有图片块或宿主生成的双重文档引用，仍不支持该轮附件导入；
不能仅凭“从哪个按钮选择”判断是否成功上传。`PENDING_MANIFEST_CREATED` 只证明捕获成功，不证明已完成归档。
新增 `PENDING_INPUT_ENCODING_INVALID` 诊断码表示宿主任务文字损坏。自动化结果以本次发布时测试输出为准；
两种 GUI 上传方式仍需升级后实测，不能把自动化通过当作 GUI 端到端完成。

此插件将用户在本轮 WorkBuddy 消息上传、并明确要求导入、归档、整理、解析或分类的附件交给文件助手。它不把本机路径、附件 ID 或正文交给模型。

图片使用已实测的 `image_blob_ref`；Word、PDF、Excel 和纯文本使用上述宿主文档引用，支持
`.doc/.docx/.pdf/.xls/.xlsx/.txt`。
不在这两类可信来源中的附件仍会被安全跳过。

在 WorkBuddy 的「技能 → 套件」中添加市场，然后安装
`file-agent-workbuddy-bridge`。如果当前图形界面只接受市场地址，可以在本机
`integrations/workbuddy` 目录打包 `.codebuddy-plugin` 与 `file-agent-plugin` 两项为
`file-agent-marketplace.zip`，再通过仅绑定 `127.0.0.1` 的本机 HTTP 服务提供该 ZIP。
测试地址示例：`http://127.0.0.1:8765/file-agent-marketplace.zip`。此地址只在
HTTP 服务运行期间有效；不能把未发布到 GitHub 的仓库地址当作已可安装的市场。

如果界面允许添加本地目录，市场根是
`E:\PycharmProject\file-agent\integrations\workbuddy`，不是 `file-agent-plugin`
子目录或 `file-agent-connector` 目录。

安装后，在插件设置填写：

- `python_executable`：例如 `D:\anaconda\envs\myenv\python.exe`。
- `workbuddy_home`：通常为 `C:\Users\zhouhexin\.workbuddy`。
- `bridge_state_dir`：例如 `C:\Users\zhouhexin\.workbuddy\file-agent-bridge-state`。

然后在 `C:\Users\zhouhexin\.workbuddy\mcp.json` 的 `file-agent` 环境变量中添加：

```json
"FILE_AGENT_WORKBUDDY_ATTACHMENT_ROOTS": "[\"C:/Users/zhouhexin/.workbuddy/blobs\",\"C:/Users/zhouhexin/.workbuddy/file-agent-bridge-state/document-cache\"]",
"FILE_AGENT_WORKBUDDY_BRIDGE_STATE_DIR": "C:/Users/zhouhexin/.workbuddy/file-agent-bridge-state"
```

配置前先创建 `bridge_state_dir/document-cache`，它必须是独立目录且显式列在授权根中；
不能把整个下载目录、工作区或 `.workbuddy` 根加入白名单。本机配置已加该目录，重新发送文档后才会生成
有效的 10 分钟提交单；旧消息不能直接复用。文档快照保留在插件私有缓存中，后端只收到原文件名、逻辑来源
和字节，不收到用户本机绝对路径。
当前试点不会自动清理 `document-cache`；长期试用需监控磁盘占用，不要在批次上传或重试期间删除其中的快照。

`bridge_state_dir` 与 `FILE_AGENT_WORKBUDDY_BRIDGE_STATE_DIR` 必须完全一致，且不能指向附件缓存目录或项目源代码目录。重启 WorkBuddy 后，上传图片并输入“把这个附件归档并分类”。只有生成本轮可信提交单时，AI 才能调用 `workbuddy_submission_ingest`。若图片等待外部提取，插件 Skill 会继续调用 `extraction_claim`，从 WorkBuddy 当前已安装且已授权、能返回真实 OCR 文本的能力中选择工具，再调用 `extraction_submit`；不能写死 Provider，也不能仅重复 `batch_get`/`job_get`。只查看或讨论附件时不会导入。

WorkBuddy 5.5.3 本机已安装并启用的腾讯文档个人版工具当前声明：`ocr.extract` 返回按顺序的
`texts: string[]`，打开位置参数后还返回 `text_detections`；不承诺置信度、Provider 版本或请求 ID，
这些缺失字段回填 `null`。`ocr.toword` 和 `ocr.toexcel` 只返回 `file_id`、`file_url`，不能代替逐页正文回填。
通用 `Read` 以及图片美化套件没有供此链路依赖的结构化 OCR 输出契约。腾讯文档 OCR 的宿主票据仍须在
真实 WorkBuddy 会话内验收；“已安装并启用”不等于已经完成一次有效 OCR 调用。

本机 0.1.4 兼容 WorkBuddy 5.5.3 的 Hook 时序：Hook 不再前置扫描尚未包含本轮消息的 transcript，而是立即生成绑定当前会话、固定 transcript 和任务文字的短时待解析引用；AI 调用 `workbuddy_submission_ingest` 时，MCP 再从该固定 transcript 中唯一匹配本轮消息。0.1.4 同时兼容宿主 JSON 把 emoji 等字符表示为 UTF-16 代理对的情况，写入提交单前会还原为合法 Unicode，不会因 UTF-8 编码异常丢失本轮引用。这个过程不延时轮询、不扫描 WorkBuddy 目录，也不会把路径交给模型。MCP 仍会校验消息时间窗、会话 ID、任务文字、稳定消息 ID 以及附件缓存授权根；缺失或匹配不唯一时直接拒绝。

标准 WorkBuddy 布局下，MCP 会从 `FILE_AGENT_WORKBUDDY_ATTACHMENT_ROOTS` 中以 `blobs` 结尾的授权根推导同级 `projects` transcript 根，不需要新增配置。非标准布局必须另行设置 `FILE_AGENT_WORKBUDDY_TRANSCRIPT_ROOTS` 为路径字符串 JSON 数组。

插件会在 `bridge_state_dir/capture-diagnostics.jsonl` 追加有限状态码（文件不超过 128 KiB），不记录任务文字、文件名、附件 ID、缓存路径或密钥。0.1.4 每次有效发送只应新增一行：`PENDING_MANIFEST_CREATED` 表示已创建待解析引用；`PENDING_INPUT_REJECTED` 或 `MANIFEST_WRITE_FAILED` 表示引用创建失败。旧版遗留的 `SESSION_MESSAGE_MISSING`、`PROMPT_MISMATCH` 或 `MATCHED` 可以忽略。失败时不得复制附件到工作区再调用 `file_batch_ingest` 冒充附件桥接成功。0.1.4 仍需 WorkBuddy 图形客户端的真实上传验收，不能仅以自动化测试通过宣称端到端成功。

提交单默认 10 分钟失效；可用 `FILE_AGENT_WORKBUDDY_SUBMISSION_TTL_SECONDS`（60–3600）调整。
