# 文件助手 WorkBuddy 附件桥接

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

当前试点版本：**0.1.10**。0.1.6 在 0.1.5 的附件捕获修复上补齐 WorkBuddy 外部 OCR 编排：图片进入
`WAITING_EXTERNAL_EXTRACTION` 后，不再只查询状态，而是由 Agent 调用 `extraction_claim` 领取固定页，
使用 WorkBuddy 已启用的腾讯文档 `ocr.extract` 识别，再通过 `extraction_submit` 回填，最后继续查重、
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

`bridge_state_dir` 与 `FILE_AGENT_WORKBUDDY_BRIDGE_STATE_DIR` 必须完全一致，且不能指向附件缓存目录或项目源代码目录。重启 WorkBuddy 后，上传图片并输入“把这个附件归档并分类”。只有生成本轮可信提交单时，AI 才能调用 `workbuddy_submission_ingest`。若图片等待外部提取，插件 Skill 会继续调用 `extraction_claim`、腾讯文档 `ocr.extract` 和 `extraction_submit`，不能仅重复 `batch_get`/`job_get`。只查看或讨论附件时不会导入。

WorkBuddy 5.5.3 本机已安装并启用的腾讯文档个人版工具当前声明：`ocr.extract` 返回按顺序的
`texts: string[]`，打开位置参数后还返回 `text_detections`；不承诺置信度、Provider 版本或请求 ID，
这些缺失字段回填 `null`。`ocr.toword` 和 `ocr.toexcel` 只返回 `file_id`、`file_url`，不能代替逐页正文回填。
通用 `Read` 以及图片美化套件没有供此链路依赖的结构化 OCR 输出契约。腾讯文档 OCR 的宿主票据仍须在
真实 WorkBuddy 会话内验收；“已安装并启用”不等于已经完成一次有效 OCR 调用。

本机 0.1.4 兼容 WorkBuddy 5.5.3 的 Hook 时序：Hook 不再前置扫描尚未包含本轮消息的 transcript，而是立即生成绑定当前会话、固定 transcript 和任务文字的短时待解析引用；AI 调用 `workbuddy_submission_ingest` 时，MCP 再从该固定 transcript 中唯一匹配本轮消息。0.1.4 同时兼容宿主 JSON 把 emoji 等字符表示为 UTF-16 代理对的情况，写入提交单前会还原为合法 Unicode，不会因 UTF-8 编码异常丢失本轮引用。这个过程不延时轮询、不扫描 WorkBuddy 目录，也不会把路径交给模型。MCP 仍会校验消息时间窗、会话 ID、任务文字、稳定消息 ID 以及附件缓存授权根；缺失或匹配不唯一时直接拒绝。

标准 WorkBuddy 布局下，MCP 会从 `FILE_AGENT_WORKBUDDY_ATTACHMENT_ROOTS` 中以 `blobs` 结尾的授权根推导同级 `projects` transcript 根，不需要新增配置。非标准布局必须另行设置 `FILE_AGENT_WORKBUDDY_TRANSCRIPT_ROOTS` 为路径字符串 JSON 数组。

插件会在 `bridge_state_dir/capture-diagnostics.jsonl` 追加有限状态码（文件不超过 128 KiB），不记录任务文字、文件名、附件 ID、缓存路径或密钥。0.1.4 每次有效发送只应新增一行：`PENDING_MANIFEST_CREATED` 表示已创建待解析引用；`PENDING_INPUT_REJECTED` 或 `MANIFEST_WRITE_FAILED` 表示引用创建失败。旧版遗留的 `SESSION_MESSAGE_MISSING`、`PROMPT_MISMATCH` 或 `MATCHED` 可以忽略。失败时不得复制附件到工作区再调用 `file_batch_ingest` 冒充附件桥接成功。0.1.4 仍需 WorkBuddy 图形客户端的真实上传验收，不能仅以自动化测试通过宣称端到端成功。

提交单默认 10 分钟失效；可用 `FILE_AGENT_WORKBUDDY_SUBMISSION_TTL_SECONDS`（60–3600）调整。
