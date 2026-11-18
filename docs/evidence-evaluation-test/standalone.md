# 独立新库与精简env测试

用于你自行上传真实文件，执行“上传→解析→全文索引→检索→证据评估”。
本次不再把配置覆盖到原.env上，而是使用一份独立、已填好连接信息的本机配置。

## 1. 已准备好的文件

- **实际使用的env：`runtime/evidence-test/.env`**，有24个配置项，权限0600，已被Git忽略。
- 测试专用评估等待上限：`EVIDENCE_EVALUATION_TIMEOUT_SECONDS=60`；普通模型调用沿用180秒默认值。
- 可分享的无密钥模板：[standalone.env.example](standalone.env.example)。
- 管理员建库SQL：[01-create-database.sql](01-create-database.sql)。
- 管理员启用扩展SQL：[02-enable-extensions.sql](02-enable-extensions.sql)。
- 启动入口：`scripts/run_evidence_test.py`。

实际env已沿用你已有数据库服务器/账号和LLM连接，仅把数据库名改为：

```text
file_agent_evidence_test_20261029
```

另生成独立JWT密钥，文件/日志路径全部在`runtime/evidence-test/`下。
原根.env、旧库`fileAgent`、旧文件均保持不变；没有复制任何旧用户、文档或模拟测试数据。

## 2. 新库当前尚未创建：先让管理员执行两步

已实际只读连接现有PostgreSQL核对：当前账号`fileagent_user`不是超级用户，也没有CREATEDB权限。
服务器可用vector和pg_trgm；计划的新库名尚不存在。**所以本次没有创建数据库或业务表。**

请在同一台数据库服务器上，用管理员账号操作：

1. 在自动提交模式下执行 `01-create-database.sql`，创建空库并把所有者设为`fileagent_user`。
2. 切换连接到新库，再执行 `02-enable-extensions.sql`。

第二份SQL会校验当前数据库名，防止误在旧库执行。
虽然本次不生成Embedding，既有表迁移仍依赖vector类型，因此新库也要安装vector扩展。
不要删除旧库，不要在旧库执行重置、DROP、TRUNCATE或downgrade。

## 3. 校验配置、建立所有业务表

从项目根目录运行：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py check
```

check只验证独立配置、测试库名、隔离目录及Jieba，不连接数据库或LLM。
确认管理员已经完成第2节后，再执行：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py migrate
```

migrate在新库执行正式Alembic `upgrade head` 建表，不使用自动create_all代替迁移，也不导入旧测试数据。
若出现权限/扩展/迁移错误，保留输出并停止，不要改为连接旧库或执行破坏性重置。
截至本次交付，未在新PostgreSQL库执行迁移，不能声称新库迁移已验证通过。

## 4. 启动真实文件测试

在三个独立终端中，从项目根分别运行：

```bash
# 终端一：API，沿用前端代理的8000端口；已有旧API时先由你自行停止旧实例。
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py api
```

```bash
# 终端二：上传查重、归档、导入和物化。
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py lifecycle
```

```bash
# 终端三：正文解析、索引和异步问答。
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py analysis
```

启动器把进程工作目录固定到独立测试目录，只传递必要系统环境变量。
它不加载根.env、不继承终端里旧的DATABASE_URL/LLM密钥/MANAGED_ROOT等应用配置。
所有动作先检查数据库名为`file_agent_evidence_test_`前缀、存储及日志路径位于独立目录，校验不通过即退出。
因此无需source旧env或把精简文件覆盖到根.env。

前端沿用项目既有方式在`apps/web`中运行`npm run dev`，开发代理仍指向8000。
新库没有旧账号，需要在聊天应用注册新测试账号；旧token不适用于新JWT密钥，先退出旧登录。

随后按[真实文件五阶段步骤](real-files.md#4-按五个阶段执行)自行上传文件、等待索引、检索、选中文件并进行内容问答。
本次只运行正文全文检索与评估，不需要Neo4j或Embedding。

## 5. 文件、日志和OCR

| 内容 | 位置 |
|---|---|
| 上传暂存 | `runtime/evidence-test/uploads/` |
| 归档原件 | `runtime/evidence-test/originals/` |
| 工作副本 | `runtime/evidence-test/working-copies/` |
| 回收站 | `runtime/evidence-test/trash/` |
| 评估日志 | `runtime/evidence-test/logs/file-agent-YYYY-MM-DD.log` |

在项目根目录查看评估事件。`runtime/` 被 `.gitignore` 忽略，必须加
`--no-ignore`，否则 `rg` 默认跳过日志目录，即使已有评估记录也可能没有输出。

```bash
rg --no-ignore -n -F 'evidence_evaluation.shadow' runtime/evidence-test/logs/
```

失败时查看 `error_details`：类别、阶段、异常类型、HTTP状态和固定中文说明。
例如 `QUOTA_EXHAUSTED` 表示额度/计费受限，`NETWORK_ERROR` 表示连接问题，
`RESPONSE_FORMAT_INVALID` 表示响应格式问题；详情见[诊断说明](../2026-10-29-evidence-evaluation-error-diagnostics.md)。
新字段需重启API和analysis后重新提问才会记录，旧日志不会自动补齐。
需要判断具体返回形态时，改用以下命令同时查看安全的类型/长度/结束原因记录：

```bash
rg --no-ignore -n 'evidence_evaluation\.(llm_response_shape|shadow)' runtime/evidence-test/logs/
```

详见[返回形态记录](../2026-10-30-model-response-shape-diagnostics.md)。`MODEL_JSON_OBJECT`仅说明JSON解析成功，
最终评估结果仍看同一agent_run_id的shadow事件。
不确定多个状态的含义时，看[大白话日志对照说明](log-reading-guide.md)，其中按一次真实请求解释各层状态。
回答结论被拦时还可查看 `evidence_answer.claims_validated` 的拒绝原因计数，
详见[评估超时与回答支持性修复](../2026-10-30-evaluation-timeout-and-answer-support-fix.md)。

精简配置使用项目默认全文检索/Jieba及本地抽取式后台摘要；只开启真实LLM问答和shadow评估。
Docling关闭，正文文件使用现有基础解析器。扫描件配置为本地PaddleOCR，仍需本机Paddle模型可正常初始化；本次未实际加载OCR模型或测试扫描文件。
若先使用文本型PDF、DOCX、XLSX、TXT，不要求OCR。
真实内容问答和评估会向已配置的LLM接口发送受控证据并计费，由你自行发起。

## 6. 这次做了什么、没做什么

已生成24项的实际私有env、无密钥模板、管理员SQL和隔离启动入口，已检查配置可读取及隔离保护。
没有修改旧库/原.env，没有安装或启动服务，没有自动调用真实LLM，没有上传真实或模拟文件。
新库创建受当前账号权限限制，业务表也尚未创建；完成管理员步骤后才能运行migrate并开始测试。
