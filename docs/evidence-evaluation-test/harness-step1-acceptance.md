# Harness第一步：实现说明与验收记录

日期：2026-11-09。依据：[最小闭环方案第7节第1步](feedback-driven-harness-mvp-plan.md#7-开发顺序与验收)。结论：本步骤验收通过；仅完成协议和反馈整理，尚未完成LLM建议、策略校验、复测执行或独立比较器。

后续更新：本文保留第一步交付时的能力边界；新增propose接线及校验见[第二步交付记录](harness-step2-acceptance.md)。第一步验收范围与历史产物不变。

## 1. 大白话：新增在什么位置

```text
已有3000/6000 Token真实评测报告 + 已冻结数据集
→ 【新增】核对输入/配置/逐轮记录是否匹配
→ 【新增】整理失败类别、阶段、预算、耗时、用量及历史尝试
→ 【新增】分别保存本机追溯反馈、未来可给建议模型的安全输入
→ FEEDBACK_READY，结束，不调用模型
```

相当于先把给“优化器”看的问题清单整理好。系统现在能明确区分“模型调用失败”和“材料缺少金额”，不会把R034没有判断出来误写成正常，也不根据人工答案反过来改写评估结果。

本步骤独立于上传、解析、检索、证据回答与Shadow生产链路，不增加LangGraph节点、线上Tool、数据库表或迁移。没有修改语义评估提示词、人工标签、真实env或原报告。

## 2. 文件清单

| 文件 | 新增/修改 | 用途 |
|---|---|---|
| `apps/api/app/modules/evidence_harness/__init__.py` | 新增 | 独立离线研究模块边界 |
| `.../evidence_harness/schemas.py` | 新增 | 严格反馈结构；区分内部反馈与未来模型载荷 |
| `.../evidence_harness/feedback.py` | 新增 | 只读核验和整理报告，输出受控私有产物 |
| `apps/api/app/scripts/evidence_harness.py` | 新增 | 只提供prepare命令，不能调用策略模型或执行实验 |
| `apps/api/app/tests/test_evidence_harness_feedback.py` | 新增 | 故障区分、脱敏、绑定、状态一致性、逐轮完整性及路径/覆盖测试 |
| `scripts/run_evidence_test.py` | 修改 | 增加harness子命令，沿用独立目录和配置隔离 |
| `apps/api/app/tests/test_evidence_test_environment.py` | 修改 | harness也必须拒绝旧库配置 |
| 本文、原方案、README及runbook | 新增/更新 | 说明实际交付范围、使用方式和验收证据 |

`schemas.py`本次只实现FeedbackPacket及其安全子结构，没有提前提供未实现的Proposal执行入口。预算只是观察到的历史数值，不构成新的实验授权。

## 3. 两类反馈分别保存

| 产物 | 内容与边界 |
|---|---|
| `feedback.json` | 本机追溯：数据集/人工标签散列、每轮有效语义标签与执行诊断；没有题干、原文、参考答案文本或密钥 |
| `optimizer-input.json` | 未来A阶段建议模型的唯一候选输入：散列化案例引用、证据条数、字符计数、历史预算、安全错误/形态、逐轮耗时与用量；不含Gold、语义标签或本地路径 |
| `source-index.json` | 本机查看用，将散列引用映射回R001/R016/R034及测试目录内来源文件；不能发送给建议模型 |
| `manifest.json` | 最后发布的FEEDBACK_READY完成标记和三份产物散列；整理报告/轮次数，model_calls=0 |
| `acceptance.json` | 本次真实报告验收结果，不是模型改进建议或模型准确率报告 |

目前没有构造或发送优化模型Prompt。“不含Gold”验收针对已经准备好的模型输入文件与严格schema；真正接入模型时仍需在第二步测试真实发送的载荷。

反馈会读取每一条`cases[].rounds[]`，不采用顶层按首轮计算的`evaluation_failures`或稳定率作全批结论。失败或部分检查的语义观察为UNAVAILABLE；不将空issues自动视为NORMAL。不完整Token用量为null，而非0。

历史尝试使用数据/标签、模型、提示词、代码、依赖及预算的散列关联条件。HAS_FAILURES仅表示执行存在失败，不等于已经完成策略效果比较，也不会凭空生成NO_GAIN、建议或新的参数值。

## 4. 使用命令

在项目根执行。以下命令已经执行，首次产物已存在；再次原样执行会返回OUTPUT_ALREADY_EXISTS，这是防覆盖的预期行为：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py harness prepare \
  --dataset datasets/frozen-real-v1/cases-v1.json \
  --baseline datasets/frozen-real-v1/runs/live-output6000-v1-01.json \
  --history datasets/frozen-real-v1/runs/live-v1-01.json \
  --output experiments/harness-v1/step1-real-v1
```

路径均相对于 `runtime/evidence-test/`。输入限制在该目录的datasets中，输出限制在`experiments/harness-v1/`的子目录；包括整个根目录被符号链接替换出测试区的情况，也必须拒绝。产物目录0700，JSON文件0600。

原始报告、数据集、模型配置和快照均不覆盖。若确需新增一次整理，使用新的输出子目录如`step1-real-v2`。不需重传文件、重启服务、连接数据库或重新调用模型。

错误只显示固定码，例如REPORT_SCHEMA_INVALID、REPORT_ROUNDS_INCOMPLETE、REPORT_CONFIG_HASH_MISMATCH或OUTPUT_ALREADY_EXISTS，不输出Pydantic输入值、数据库地址或异常原文。校验全部通过后才创建输出目录；中断未发布manifest时不视作完成。

## 5. 按方案逐项验收

| 第一步要求 | 结果 | 证据 |
|---|---|---|
| 用现有两组真实报告整理反馈，不再请求模型 | 通过 | 2份报告、18条逐轮记录，model_calls=0 |
| 明确区分截断与读取超时 | 通过 | R034：3000组3条OUTPUT_TRUNCATED，6000组3条TIMEOUT；保留阶段、异常类型、HTTP状态和安全响应形态 |
| R034不当成NORMAL或MISSING | 通过 | 6条均UNAVAILABLE，semantic_judgment_available=false，issue_types为空；此空列表不是正常判断 |
| 不完整Token用量不当0 | 通过 | R034 6条input_tokens/output_tokens为null |
| 模型输入不含Gold、原文、身份、路径、密钥 | 通过 | 真实题干、文件名、quote片段、参考解释不在载荷；合成敏感哨兵和额外字段测试通过 |
| 输入和报告可追溯，不能偷换数据或丢轮次 | 通过 | 数据集/配置/输入散列、案例集合、逐轮序号、状态一致性及引用白名单核验 |
| 保留历史尝试，不能把回放当真实实验 | 通过 | 两组live按条件和预算关联；本步骤拒绝rules/replay作为真实历史来源 |
| 原始材料与env不变，输出不覆盖 | 通过 | 真实验收前后对原数据集目录JSON及测试env做字节散列对比；重复prepare返回2，旧产物字节不变 |
| 读取/写入范围及私有权限 | 通过 | 路径和符号链接越界测试；真实产物目录0700、JSON0600 |

真实验收产物：`runtime/evidence-test/experiments/harness-v1/step1-real-v1/`。其中18条记录包含12条有效语义判断和R034的6条调用失败。两组历史的R001/R016记录均已保留，不因人工标签而改写模型观察。

## 6. 自动测试

相关回归232项通过，含新反馈整理、启动器隔离、固定输入、错误诊断、输出额度入口、评估集成及原证据回答测试。Jieba有一条已有依赖弃用提示。

可复测命令：

```bash
DATABASE_URL=postgresql://fake:fake@127.0.0.1/file_agent_unit_tests \
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest -q \
  apps/api/app/tests/test_evidence_harness_feedback.py \
  apps/api/app/tests/test_evidence_test_environment.py \
  apps/api/app/tests/test_frozen_evidence_inputs.py \
  apps/api/app/tests/test_evidence_evaluation_diagnostics.py \
  apps/api/app/tests/test_evidence_output_budget.py \
  apps/api/app/tests/test_evidence_evaluation_integration.py \
  apps/api/app/tests/test_evidence_answer.py
```

测试使用隔离文件、SQLite和deterministic fake。此处fake PostgreSQL地址只供既有日志配置读取，不连接真实库；不写入env。首次扩展回归未传DATABASE_URL时，有4个既有回答用例因配置缺失失败，补充临时测试配置后232项全部通过，未修改业务实现来绕过失败。

本步骤无前端变更，没有运行前端build或宣称整项目MVP验收通过。验收范围严格对应方案第7节第1步；策略建议模型、白名单策略校验与自动复测属于后续步骤。
