# 证据状态评估器 MVP：实现与文件清单

依据：下载目录 `File_Agent_Evidence_Evaluator_MVP_Guide.docx` v1.0（2026-10-28）。实现版本：`evidence-eval-v1`。

## 1. 大白话说明：加在什么环节

原来是“找到有关原文 → 组织答案”，现在在中间加了一个检查员：

```text
用户提问 → 原来的权限与文件范围校验 → 原来的检索及原文取证
→ 建立本轮证据包
→ 【新增】检查缺信息、内容矛盾、旧内容版本，记录诊断
→ 原来的答案生成 → 原来的引用校验及保存 → 用户回答
```

第一版先旁观，不补搜、不改答案、不决定拒答，也不改文件。检查失败，原来的回答继续运行。

| 名称 | 大白话 | 例子与边界 |
|---|---|---|
| `MISSING` | 本轮材料没提供回答必需的信息 | 问两年的经费，只找到一年；不等于整个资料库没有 |
| `CONFLICT` | 同一事、同一年、同一口径说法不一致 | 最终批准预算分别是500万和450万；申请与批准不同不算 |
| `OUTDATED` | 问当前情况，却引用已被新内容版本替换的材料 | 须有当前版本及就绪索引证明；历史问题允许旧版本 |

三类可同时出现。文件年份早不能单独证明过期；法规、通知或业务事实是否失效本期不判断。

## 2. 新增了哪些文件

以下代码均以 `apps/api/app/` 为前缀：

| 文件 | 用途 |
|---|---|
| `modules/evidence_evaluation/schemas.py` | 严格定义问题、支持/不支持/不确定、冲突维度、检查状态和评估结果 |
| `modules/evidence_evaluation/version_checker.py` | 只读核对工作副本当前版本及受管源当前修订 |
| `modules/evidence_evaluation/semantic_checker.py` | 对照问题和证据找缺项与同口径冲突，控制输入、输出和调用次数 |
| `modules/evidence_evaluation/validator.py` | 拦截编造引用、无引用支持、伪造冲突原值和敏感输出 |
| `modules/evidence_evaluation/service.py` | 汇总检查、保留失败/不确定状态，写脱敏旁观诊断 |
| `scripts/evaluate_evidence.py` | 回放、仅规则、真实模型三种评测入口及指标报告 |
| `tests/test_evidence_evaluation.py` | 判断、异常、伪造引用、预算、注入文本和单位换算测试 |
| `tests/test_evidence_evaluation_integration.py` | ORM授权、两条回答链、缓存、故障隔离和逐条样本回归 |
| `tests/fixtures/evidence_evaluation_cases.json` | 64条合成案例：问题、证据、版本快照、预期问题/引用/解释、固定语义响应 |

另外新增本说明和 `docs/2026-10-28-evidence-evaluator-validation.md` 验证报告。
开始工作前，包目录的 `__init__.py` 已是用户暂存的空文件，本次沿用且没有修改或重置暂存区。

## 3. 改了哪些已有文件

| 文件 | 改动 |
|---|---|
| `apps/api/app/modules/evidence_answer/service.py` | 新增 `_evaluate_shadow`，在工作副本和受管源两条路径的包建立后、生成前调用；评估客户端独立注入 |
| `apps/api/app/core/config.py` | 默认关闭的模式、超时、输入字符、调用数、输出token；未知模式/越界环境预算启动报错 |
| `apps/api/app/modules/llm/client.py` | 增加限制输出并返回token用量的方法；旧 `complete_json` 的参数、返回与请求格式兼容，非标准usage不影响原回答 |
| `.env.example`、`deploy/.env.production.example` | 增加默认关闭的示例配置 |
| `README.md`、`docs/runbook.md` | 增加能力说明、开启、日志和评测命令 |

没有新增数据库表、迁移、HTTP/MCP接口、Tool、Skill、LangGraph节点或前端控件。
没有改变现有 EvidenceItem/EvidencePackage/StructuredAnswer 协议及两阶段检索。
评估是原 `evidence-answer` Tool 内的只读子环节，不另外创建 ToolInvocation、ChangeSet 或 OperationPlan；
原回答/引用和Tool审计仍按原流程保存。证据包、运行依赖和评估结果不写入图状态或普通用户回执。

## 4. 实现边界

### 版本检查

工作副本使用服务端已授权ID集合、当前用户会话、共享工作区、ACTIVE状态、证据文档/版本绑定、当前版本及成功索引。
受管源使用已授权修订集合、活动源文件、启用的受管根、分析文档/版本绑定、同一源文件唯一当前READY修订及成功索引。
修订ID与内容版本ID分别核对，绝不混用。不能靠传入document_id扩大访问范围。

只有问题明确含“当前/现在/最新/现行/目前”等线索才判当前；明确历史问题可用旧版。
时间意图不明或当前/历史混合时保留 `TEMPORAL_FOCUS_UNKNOWN`。
对象不存在、回收、无当前版本、源关系不完整或索引未就绪，保留 `VERSION_UNVERIFIABLE`，不强判过期。
授权异常不泄露其他对象存在性，并跳过语义外发。

数据库使用Core SELECT与连接级SAVEPOINT，不flush/commit主回答会话。异常仅回滚评估保存点。
同一对象/版本的多条证据共用查询快照，避免逐Chunk重复SQL。

### 缺项和冲突

模型最多拆八个必需事实，逐项返回 `SUPPORTED / UNSUPPORTED / UNCERTAIN`；支持必须给真实证据ID。
仅明确不支持的要求产生MISSING，未知保留不确定。判断始终只适用于当前包，并保留包自带limitations。

冲突候选必须有实体、属性、时间、口径、事实状态、原值及两个真实证据ID。
后端要求维度齐备且一致、值能在quote中定位、两个引用不同，模型同时确认断言实质不兼容。
不同年份、组织、申请/批准/实付不直接报冲突；维度未知记不确定。
元/万元/亿元、常见数量和百分比的等值由Decimal确定性换算拦截，不依赖模型心算。

输入明确JSON Schema，输出经严格Pydantic和引用白名单校验；未知状态、额外字段、越界ID均失败。
引用是数据，不能执行其命令。自由输出限制长度并拒收绝对路径、密钥样式等敏感结果。

### 总结、字段表和预算

- FIELD_TABLE由后端固定每个字段要求，模型不能只查已有字段；超过八个字段显式NOT_EVALUATED。
- FULL_SUMMARY只评价包内内容，保留 `FULL_SUMMARY_COVERAGE_NOT_VERIFIED`，不声称全文已齐备。
- 大包分批，后批逐字复用首批要求。任一批有支持即支持；全部批不支持才报缺项。
- 截断或调用预算用完后，未见支持的要求改为UNCERTAIN，不能证明MISSING。
- 多批没有穷举跨批冲突，明确标PARTIAL及 `CROSS_BATCH_CONFLICT_NOT_EVALUATED`。
- 单条超长quote只检查前段并标截断；schema、字段和锁定要求等元数据发送前计入字符预算。

### 状态与记录

COMPLETED代表检查已完成，空issues仅代表没发现问题；没有SUFFICIENT自动通过状态。
PARTIAL表示部分能力未检查或不确定；EVALUATION_FAILED表示模型、格式、权限或数据库异常；DISABLED表示关闭。
语义失败不抹掉已经确定的OUTDATED。

JSONL事件 `evidence_evaluation.shadow` 只记录版本、指纹、问题类型/证据ID/原因码、检查状态、错误/限制码、耗时和成本。
2026-10-29新增安全 `error_details`：错误类别、调用阶段、白名单异常类型、可取得的HTTP状态及固定中文说明。
详见[失败分类、字段与文件清单](2026-10-29-evidence-evaluation-error-diagnostics.md)。
2026-10-30补充[返回形态记录](2026-10-30-model-response-shape-diagnostics.md)：类型、空值、长度、
结束原因与JSON顶层类型；不记录模型正文或思考文字，格式失败时附在error_details.response_shape。
不记录问题、quote、模型自由理由、完整包、密钥或异常原文。
token只统计模型实际返回的用量；`token_usage_complete=false`表示成本不完整。
完整可解释结果由内部evaluate返回、离线报告保存，不展示到普通用户答案。

## 5. 开启方式

不需要数据库迁移。部署`.env`设置以下值，并重启提供回答的进程：

```dotenv
EVIDENCE_EVALUATION_MODE=shadow
EVIDENCE_EVALUATION_TIMEOUT_SECONDS=20
EVIDENCE_EVALUATION_MAX_INPUT_CHARS=48000
EVIDENCE_EVALUATION_MAX_CALLS=3
EVIDENCE_EVALUATION_MAX_OUTPUT_TOKENS=3000
```

MISSING/CONFLICT还需要原有 `LLM_ENABLED=true` 和已配置模型连接；LLM关闭时只核对版本，语义为NOT_EVALUATED。
仅打开全局LLM不会隐式开启评估。本次没有修改真实`.env`，保持默认off。
shadow同步执行，会增加延迟和费用：默认单次20秒、最多三次、无额外重试。
off没有评估SQL或模型调用；活动工作副本回答缓存命中不重复评估。源侧沿用原无提前缓存返回链，建立新包时评估。

## 6. 评测与验收

仓库根运行，使用现有Python环境，不新增依赖：

```bash
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python \
  -m app.scripts.evaluate_evidence --mode replay --output /tmp/evidence-replay.json
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python \
  -m app.scripts.evaluate_evidence --mode rules --output /tmp/evidence-rules.json
```

replay不连数据库/模型，重放固定语义响应，只证明工程协议；rules仅核对冻结版本事实。
`--mode live`是额外真实模型调用入口，会产生费用，仍需显式启用LLM。
live只发送问题、字段、证据，不发送预期标签或回放响应；版本仍用冻结快照，不访问生产文件。
默认每个案例重复两次，`--repetitions`可调；输出逐类Precision/Recall/F1、正常误报率、精确引用匹配、追溯率、稳定性、成本、p50/p95和逐条结果。

64条是逐条编写的合成案例，尚未由业务人员独立复核；本次不把它们冒充真实人工标注业务集。
没有开展真实模型或生产灰度验收，不能据此宣称真实业务F1达到0.85。
实际测试结果及既有失败见[验证报告](2026-10-28-evidence-evaluator-validation.md)。
