# 用自己上传的真实文件测试证据状态评估器

当前启动/建库以[独立新库与精简env](standalone.md)为准，使用`runtime/evidence-test/.env`；本页后续五阶段验收步骤继续适用，第2、3节的覆盖配置启动方式仅保留供旧环境参考，不要与独立启动器混用。

本说明对应你的测试目标：

```text
自己上传真实文件 → 解析正文 → 建立全文索引 → 检索证据 → 证据状态评估
```

不使用模拟TXT、固定证据包或预置模型响应作为验收输入。
评估接在证据包建立后、答案生成前，shadow只记录结果，不改变原答案。

## 1. 这次到底需要准备什么

| 项目 | 本次要求 |
|---|---|
| Jieba | 补齐。正式流程以项目的Jieba中文分词作为验收条件 |
| PostgreSQL及已有表/索引 | 使用已有可连接的数据库；全文索引依赖已有迁移中的pg_trgm等扩展 |
| LLM连接 | 用现有已授权配置，进行真实内容问答及MISSING/CONFLICT判断 |
| API、上传生命周期worker、正文分析worker | 必须运行，承担实际归档、解析和索引；API健康不代表文件已可检索 |
| 前端聊天页 | 用于你自行上传、查找、选择文件和提问 |
| Embedding、Neo4j | 本次全文检索链不依赖，不需要先安装或启用 |

Jieba缺失时程序确实有备用分词，但本次不把降级分词结果当作正式Jieba检索效果。
此前检查本机未安装Jieba。你可以在现有Python环境中自行补装并检查，以下命令未由助手执行：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python -m pip install 'jieba>=0.42.1,<0.43'
/opt/homebrew/anaconda3/envs/py311/bin/python -c 'import jieba; print(jieba.__version__)'
```

安装后重启相关进程，并使用新上传文件建立索引；若复用以前用备用分词建立的索引，应先通过已有受控重处理入口更新，不能假定新旧分词一致。
真实扫描PDF/图片还需现有OCR可用；旧DOC/XLS需要已有LibreOffice转换能力。先确认文件确实解析出了正文。

## 2. 加载真实文件测试配置

使用[evaluation-test.env.example](evaluation-test.env.example)。它不含密码，不能覆盖或替换根`.env`。
根`.env`继续提供已有数据库、存储目录、LLM_API_KEY、LLM_BASE_URL和LLM_CHAT_MODEL。
真实上传会按原流程写入工作副本与数据库，原件保护和权限边界不变；本说明不清空或重置任何既有数据。

在仓库根目录，每个需要启动API/worker的终端都先执行：

```bash
set -a
source docs/evidence-evaluation-test/evaluation-test.env.example
set +a
```

该配置固定全文检索、Jieba、本地抽取式后台摘要，开启真实LLM问答和shadow评估，并关闭答案缓存和图谱/向量增强。
加载只影响当前终端及之后启动的进程。已运行服务不会自动更新；需要你自行按实际启动方式重启。
真实问答与评估会调用配置中的模型并计费；助手没有替你调用。

## 3. 让上传、解析和索引流程实际运行

复用已运行的对应进程。尚未运行时，下面是已有的macOS启动方式，各命令在独立终端执行，且先完成第2节。
已有实例占用端口或消费相同队列时，先确认运行状态，不重复启动。

API：

```bash
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python \
  -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

上传查重、归档、导入及工作副本物化worker：

```bash
PYTHONPATH=apps/api FILESYSTEM_WORKER_ID=lifecycle-1 \
  FILESYSTEM_WORKER_QUEUES=DUPLICATE_CHECK,ARCHIVE,FILE_OPERATION,MATERIALIZE,IMPORT \
  /opt/homebrew/anaconda3/envs/py311/bin/python -m app.modules.managed_files.worker
```

正文解析、索引及异步Agent任务worker：

```bash
PYTHONPATH=apps/api FILESYSTEM_WORKER_ID=source-analysis-1 \
  FILESYSTEM_WORKER_QUEUES=SOURCE_ANALYSIS,ANALYSIS,AGENT \
  /opt/homebrew/anaconda3/envs/py311/bin/python -m app.modules.managed_files.worker
```

前端沿用已有API地址配置，在 `apps/web` 运行 `npm run dev`，打开聊天页登录。
现有scheduler/watcher及部署目录权限按[runbook](../runbook.md)保留。
如果测试请求涉及结构化图片/表格抽取，还需相应STRUCTURED_EXTRACTION worker；普通正文证据问答不要求启动GRAPH worker。

## 4. 按五个阶段执行

### 第一阶段：你自己上传真实文件

新开测试会话，上传你选定的一小批真实PDF、Word、Excel或其他已支持文件，并同时输入明确任务文字，例如：

```text
请读取并整理这批文件，供我之后检索和内容问答使用。
```

只有附件不能空文字提交。若出现查重或相似文件选择，由你明确决定保留哪些文件。
初次整理可能标准化工作副本名称，后续按回执中的实际文件名选择，不能假设上传原名始终是当前名称。

### 第二阶段：确认正文解析成功

等待文件处理完成，通过预览/读取确认正文可用。
扫描件关注OCR是否成功，Excel关注工作表及单元格是否实际读取。
空正文、加密、转换/OCR失败等应先处理，不能直接算评估器漏判。

### 第三阶段：确认全文索引已建立

正文能预览不等于已能检索。确认后台相应DocumentVersion的索引成功，
例如日志 `document.index.completed`，或管理员文件处理详情中的完成状态。
INDEX_PENDING、PARTIAL_INDEX、INDEX_FAILED都不能当作索引完整。
本阶段生成词法索引、Chunk和EvidenceSpan，不要求生成Embedding。

### 第四阶段：先检索，再选定回答范围

先提出与你真实文件内容有关的查找请求，例如：

```text
在本次上传的文件中，查找与我关心的业务主题有关的材料。
```

请把“业务主题”替换为你的真实主题，不照抄占位词。
检查返回的文件是否相关，出现选择卡时明确选择一份或多份。
测试CONFLICT时必须让两份涉及同一事实的材料都进入范围，不能只选其中一份。

### 第五阶段：针对选中文件提事实问题

接着要求“根据这些选中的文件回答……”并写出你实际关心的事实。
应问具体条件、金额、时间、同口径差异或缺失字段，不要只说“找文件”或“简单总结”。
纯文件搜索只返回文件，未进入evidence-answer就不会触发本次评估器。
问题进入取证后，系统按原链路检索EvidenceSpan、构造EvidencePackage，再运行shadow检查。

## 5. 用真实材料怎样判断结果对不对

先由你阅读真实文件，记录预期结果，再与本次评估日志对照。

| 检查 | 如何选真实材料/问题 | 预期 |
|---|---|---|
| 正常对照 | 选能完整回答问题的材料 | 不应凭空报缺项或矛盾 |
| MISSING | 问一个必须包含多项事实的问题，而选定材料确实未提供其中一项 | 对当前证据包报告缺少该项 |
| CONFLICT | 两份真实记录对同一实体、年度、范围、事实状态给出不兼容说法 | 报告冲突并引用两方 |
| 不同口径反例 | 材料分别说申请额与批准额，或不同年度/不同部门 | 不能仅因数字不同报冲突 |

MISSING只针对当前检索证据包。如果原文件有事实、但检索没有取到它，应先区分上游检索遗漏与评估器本身的判断。
如果两份冲突材料没有同时被检索到，也不能要求模型识别它看不到的另一方。

OUTDATED第一版仅指“同一对象的旧内容版本”，不指政策日期早或业务规定失效。
正常链路主动读取当前版本，所以真实上传测试首先验证不会误报OUTDATED。
真实正例须有受控版本替换、新版索引已完成、评估包仍引用旧版，以及明确询问当前状态。
目前没有聊天按钮专门构造这个场景；把新旧文件分别上传成两个独立对象，不算版本替换，也不能承诺必然触发OUTDATED。
本次不伪造版本关系或改生产数据库来凑正例。

## 6. 查看本次真实任务的证据评估记录

shadow结果不添加到普通聊天答案，查看实际LOG_DIR下的JSONL日志。
默认目录为项目根`logs/`，可自行检索：

日志目录被 `.gitignore` 忽略，以下命令使用 `--no-ignore` 防止 `rg` 跳过。
独立测试配置的实际目录为 `runtime/evidence-test/logs/`，使用该配置时替换命令中的目录。

```bash
rg --no-ignore -n 'document.index.completed|retrieval.search.completed|evidence_answer.retrieval_completed|evidence_answer.package_built|evidence_evaluation.shadow' logs/
```

先按上传时间/文档ID确认后台索引，再按本次提问的agent_run_id核对取证和评估；不同后台任务的AgentRun不能混为一个。

| 事件/字段 | 核对内容 |
|---|---|
| `evidence_answer.retrieval_completed` | 本次是否找到了可用原文证据 |
| `evidence_answer.package_built` | 是否已建立待评估证据包；这是工作副本路径的事件 |
| `evidence_evaluation.shadow` | 本次真实评估是否执行；工作副本与受管源侧都使用该事件 |
| `issue_types`、`issues` | 问题类型、真实证据ID及固定原因码 |
| `status`、`version_status`、`semantic_status` | COMPLETED、PARTIAL、NOT_EVALUATED还是EVALUATION_FAILED |
| `error_codes`、`limitation_codes` | 判断是否失败、未完整检查或无法核验 |

没有证据包时，原链路可能提前返回NO_EVIDENCE，评估器不会运行；先检查范围、解析和索引。
EVALUATION_FAILED不是“没有问题”，PARTIAL也不是“证据充足”。
日志只记录受控诊断，不打印真实文件正文、问题全文、模型自由理由或密钥。

建议每个真实问题记一行：选定文件、预期事实/页码/Sheet、预期问题类型、本次AgentRun、实际issue_types、是否失败及上游是否漏检。
这样才能判断整条链在哪里出问题，而不是仅看最终回答像不像正确。

## 7. 本次交付范围

提供真实文件测试步骤并完善覆盖配置。没有替你上传文件、安装Jieba、调用LLM、
执行迁移、启动服务或修改根`.env`。测试结束后在未加载覆盖配置的新终端中按原方式重启，恢复原运行配置。
