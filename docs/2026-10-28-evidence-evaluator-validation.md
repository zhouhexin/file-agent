# 证据评估器 MVP 验证报告

日期：2026-10-28。版本：`evidence-eval-v1`。
实现及文件清单见[交付说明](2026-10-28-evidence-evaluator-mvp.md)。

2026-11-05补充：已完成[真实输入冻结与独立评测入口](evidence-evaluation-test/frozen-real-input-usage.md)。
支持受控采集、同次版本事实复用、输入/金标散列校验、逐轮完整报告、逐调用回放与隔离启动。
相关回归328项通过；64条工程fixture的CLI两轮回放保存128条结果且无失败。
尚未重新采集用户三题的真实输入或执行新一轮付费模型评测，不能把本次开发测试视为真实业务验收。

2026-10-29补充：完成[失败分类与安全诊断](2026-10-29-evidence-evaluation-error-diagnostics.md)。
新增39个诊断场景；包括模型客户端、评估与回答、Agent Runtime/Adaptive/来源问答在内的相关回归共425项通过。
记录错误类别、调用阶段、白名单异常类型、可取得的HTTP状态及固定中文说明；不记录接口错误体、密钥或正文。
本轮仍使用deterministic fake，未调用真实模型，历史 `SEMANTIC_CHECK_FAILED` 的原因不能事后补齐。

## 1. 本次结论

已实现默认关闭的只读评估和两条证据回答链的 Shadow 接入。
缺项、冲突、旧内容版本可同时出现，失败与不确定显式区分。
本次验证使用隔离数据库、确定性Fake和合成案例，没有调用真实付费模型或开展生产灰度。
因此本报告证明工程链与规则回归，不证明真实业务上的语义F1或稳定识别率。
指南要求的业务人员独立人工标注、真实模型准确率及生产Shadow成本验收仍需执行。

## 2. 自动化测试

| 检查 | 结果 |
|---|---|
| 改动前证据回答与检索就绪基线 | 41通过 |
| 最终本功能及关联回归（7个测试文件） | 215通过，3.11秒 |
| 最终全量API测试 | 1665通过、19跳过、9失败，167.51秒 |
| 前端 `npm run build` | TypeScript检查和Vite构建通过 |
| 差异空白检查 `git diff --check` | 通过 |

专项命令见[runbook](runbook.md#42-证据状态旁观评估2026-10-28)。
全量命令为在 `apps/api` 执行：

```bash
DATABASE_URL=postgresql://test:test@localhost/test \
  /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest -q
```

占位DATABASE_URL供配置校验使用，测试复用已有隔离fixture，没有迁移或改写真实数据库。

保护的主要行为：

- 同口径矛盾、多标签共存、缺一年的信息与不确定要求。
- 申请/批准、不同年度/组织/口径不误报；等值金额的Decimal换算。
- 当前版本替换、历史旧版合法、工作副本不存在/回收、索引未就绪。
- 源修订独立路径及内容版本绑定；跨用户、跨工作区、越权对象集合不能扩权。
- 模型超时、无效结构、未知状态、额外字段、伪造引用、伪造冲突原值、敏感输出失败。
- 引用中的伪指令只作为数据；评估不修改输入证据包。
- LLM关闭不外发；off跳过检查；缓存命中不重复评估。
- 大包分批固定要求、后批支持修正前批缺失、截断不能强判缺项。
- 固定字段表要求、八字段上限、全文覆盖不作过度承诺。
- SAVEPOINT不flush主会话；真实SQL错误仅回滚保存点，原答案与引用仍能保存。
- 工作副本和源侧两条链在评估成功/失败时，原答案、引用、回答模型调用次数及持久化事实一致。
- 模型输出上限、usage统计和缺失/异常usage的旧客户端兼容。

故障与权限回归包含十类非法输出，另有超时、JSON异常、权限失败、数据库异常等用例。
保存点回归使用SQLite隔离库；未连接真实PostgreSQL开展故障注入。
本次没有新增UI，未进行真实模型的浏览器手工烟测。

## 3. 64条冻结合成案例

数据：`apps/api/app/tests/fixtures/evidence_evaluation_cases.json`。
正常16、MISSING16、CONFLICT16、OUTDATED16；其中一条冲突案例同时缺少拨付日期，故MISSING正例17条。
每条包括问题、原文证据与定位、工作副本/源修订身份、版本快照、预期问题/引用/解释、固定语义响应。
全部为逐条编写的合成资料，不是真实学校文件，也尚未经业务人员独立复核。

默认重复两次的replay结果：

| 类别 | TP | FP | FN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| MISSING | 17 | 0 | 0 | 1.00 | 1.00 | 1.00 |
| CONFLICT | 16 | 0 | 0 | 1.00 | 1.00 | 1.00 |
| OUTDATED | 16 | 0 | 0 | 1.00 | 1.00 | 1.00 |

**replay的语义响应是测试桩，以上不是实际模型准确率，不能作为真实模型F1验收。**

- 精确问题及引用集合匹配：64/64。
- 所有48次非空问题引用均属于输入包，可追溯率100%。MISSING允许没有引用，只列缺少的要求。
- 正常案例误报0/16；重复问题类型/引用/状态稳定64/64。
- 评估失败0；30条为PARTIAL，主要由于问题没有明确当前/历史时间线索，或全文覆盖未证明。
  这些结果不应被解读为证据充足。
- 回放调用Fake128次，无真实模型调用，token用量0。
- 本地回放p50/p95均低于1毫秒的记录精度，报告为0；这不是生产延迟或模型成本测量。

仅规则模式使用同一冻结版本事实：OUTDATED的TP16、FP0、FN0；
MISSING/CONFLICT明确NOT_EVALUATED，不能声称关闭LLM也能完成语义检测。
规则模式64条均PARTIAL，真实模型调用0。

## 4. 全量测试的既有9项失败

使用 `git archive HEAD` 在 `/private/tmp/file-agent-eval-baseline` 导出修改前完整代码，
在相同Python环境复跑以下四个既有测试文件。
本次全量发现的9项失败，在HEAD基线全部复现；两组失败集合交集9项，本次全量新增失败0项。
基线局部运行另有两个同文件的开关状态相关失败，故局部失败总数为11；未据局部计数冒充全量基线计数。

| 文件 | 本次全量失败数量 | 已有原因 |
|---|---:|---|
| `test_alembic_migration_graph.py` | 1 | 测试期望head为20260924_0002，现有head已为20261009_0002 |
| `test_file_data_reset_preserve_users.py` | 2 | 文件域重置清单遗漏既有reference_catalog_versions表 |
| `test_excel_writeback.py` | 1 | 测试请求被既有结构化数据集/Excel回传功能开关拒绝，返回WRITEBACK_DISABLED |
| `test_scoped_pdf_source.py` | 5 | 固定第25页相关场景在回传环节被同一既有开关拒绝 |

复跑命令（在基线导出目录的apps/api中）：

```bash
DATABASE_URL=postgresql://test:test@localhost/test \
  /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest \
  app/tests/test_alembic_migration_graph.py app/tests/test_excel_writeback.py \
  app/tests/test_file_data_reset_preserve_users.py app/tests/test_scoped_pdf_source.py -q
```

这些迁移、重置和Excel功能未在本次证据评估任务中修改。

## 5. 后续真实验收方法

先由业务人员复核合成标签并补入真实脱敏样本，冻结新数据集。
显式使用 `--mode live --repetitions 2` 调用已授权模型，报告逐类F1、误报、失败覆盖率、追溯、稳定性及实际token/延迟。
live不发送标签或预置语义响应，避免把测试答案泄漏给模型。
真实模型分数达标后再进行生产Shadow观察；补检、澄清或拒答决策不在本期实现。
大包跨批冲突、八要求/字段上限、技术时间意图不明等限制应继续按PARTIAL/NOT_EVALUATED保留，不能静默自动通过。
