# Harness第二步：LLM建议接入、策略校验与验收

日期：2026-11-09。依据：[最小闭环方案第7节第2步](feedback-driven-harness-mvp-plan.md#7-开发顺序与验收)。工程验收通过：实际Provider接线、请求载荷和故障使用确定性Fake/HTTP替身验证；尚未向真实模型发起建议调用，也未执行任何候选实验。

后续更新：本文保留第二步交付时的能力边界。受控复测和独立比较已在[第三步](harness-step3-acceptance.md)另行交付，propose本身仍不执行候选。

## 1. 实现到哪一步

```text
第一步安全反馈
→ 核对完成标记、产物散列并从原报告重建
→ 构造安全载荷和固定能力目录
→ 【默认只检查，显式--live才继续】
→ 请求一次LLM，返回NO_CHANGE或一个单变量建议
→ schema及策略校验
→ 保存VALIDATED / NO_CHANGE / REJECTED / OPTIMIZER_FAILED
→ 结束，不执行候选、不修改env或线上策略
```

这次实际接入现有`OpenAICompatibleLLMClient.complete_json_with_usage`，不是用条件分支伪造“增加超时”作为模型答案。代码没有预定真实模型应建议120秒，也不保证模型一定能提出改进。测试中的建议均显式标记Fake。

## 2. 文件变动

| 文件 | 变动 | 职责 |
|---|---|---|
| `apps/api/app/modules/evidence_harness/optimizer.py` | 新增 | 验证反馈、构造安全Prompt、接真实客户端、一次建议调用及私有持久化 |
| `.../evidence_harness/policy.py` | 新增 | 白名单、反馈引用、当前基线、单变量和失败历史校验 |
| `.../evidence_harness/schemas.py` | 修改 | 增加单变量建议、NO_CHANGE和固定校验结果协议 |
| `apps/api/app/scripts/evidence_harness.py` | 修改 | 增加propose子命令；默认检查，--live请求一次建议 |
| `apps/api/app/tests/test_evidence_harness_optimizer.py` | 新增 | 真实HTTP协议替身、拒绝/故障、载荷脱敏、预算及不可覆盖验收 |
| 原方案、README、runbook及本文 | 更新/新增 | 明确第二步完成范围和真实调用步骤 |

没有改变评估器、评估Prompt、Shadow行为、已有env、快照、人工标签或旧报告。没有新增数据库迁移、线上API或执行器。

## 3. 模型究竟读取什么

只发送第一步`optimizer-input.json`对应的安全投影、固定白名单和响应schema。原始题干、文件名、quote、人工答案/标签、身份、API key和来源路径不进入用户载荷。内部Gold散列及语义标签也不发送。

建议模型沿用基线记录中的模型与服务：运行前核对模型名散列及服务地址散列，密钥只用于客户端请求头，不写产物。若当前env换了模型或服务则先拒绝，需另外明确实验条件。

建议协议要求：

- decision为NO_CHANGE或PROPOSE_EXPERIMENT；target固定evaluation_runtime。
- feedback_refs引用真实且与当前基线同条件的反馈，不能编造或重复。
- NO_CHANGE时change为null；PROPOSE_EXPERIMENT只有一个change。
- change.from是当前基线值；字段仅允许max_output_tokens或timeout_seconds。
- 输出额度候选3000/6000/8000；超时候选60/120。
- 同条件下已经失败/不完整的相同Profile被拒绝；不同条件的历史不冒充当前依据。
- 解释只作假设，不授权动作或参与评分。敏感文本被拒绝；无效/敏感原始响应不落盘，只保留散列及固定原因。

例如，当前基线为6000/60，回退到同条件已失败的3000/60会被拒绝。模型也可提议NO_CHANGE。若建议120秒并得到VALIDATED，仅表示满足候选规则；普通env的60秒上限仍未改变，120秒不会在本步骤生效。离线ExperimentProfile及实际复测属于第三步。

## 4. 当前调用预算

| 项目 | 第二步固定值 |
|---|---|
| 建议模型请求 | 最多1次，内部重试0次 |
| 建议模型输出上限 | 3000 Token |
| 建议模型HTTP超时参数 | 60秒 |
| 建议输入上限 | Prompt及用户载荷共64000字符；超过即拒绝，不静默截断 |
| 候选复测请求 | 0次，尚未实现执行器 |

这些是“提建议”调用的预算，与建议中拟调整的“评估调用”预算分开。模型不能给自己的请求加时或加Token。一次调用失败立即记录并结束，不自动修复JSON后反复请求。响应JSON可解析但协议错误时REJECTED；请求/JSON解析失败时OPTIMIZER_FAILED。

目录和STARTED记录在请求前创建。若中断，只留下STARTED或部分产物，不视为成功，不自动重发可能已计费的调用。请求完成后的Token若缺失则null，不宣称零消耗。

## 5. 用户操作步骤

无需重传文件、重启前端/API/worker，或手改env。

### 第一步：进入项目根目录

```bash
cd /Users/zhouhexin/PycharmProjects/file-agent
```

### 第二步：先检查，零模型调用

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py harness propose \
  --feedback experiments/harness-v1/step1-real-v1 \
  --output experiments/harness-v1/step2-live-v1
```

看到`CHECK_PASSED`后，说明反馈与来源匹配、载荷不超限且目标可用；默认不创建输出目录、不请求模型。该命令已在当前真实反馈上通过。它不验证真实供应商网络、余额或下一次响应是否正确。

### 第三步：请求一次真实建议

在上一条命令末尾增加`--live`：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py harness propose \
  --feedback experiments/harness-v1/step1-real-v1 \
  --output experiments/harness-v1/step2-live-v1 \
  --live
```

这条命令会请求一次已配置模型并消耗Token；不会再跑三题三轮，也不会应用建议。无需连续执行多次。

所有路径相对`runtime/evidence-test/`。真实产物将位于：

```text
runtime/evidence-test/experiments/harness-v1/step2-live-v1/
  request.json             # 安全请求载荷、预算、输入/Prompt/代码版本散列
  invocation-started.json  # 请求前持久化，帮助识别中断
  proposal.json            # 安全且通过schema的模型原建议，或null与返回散列
  validation.json          # 校验状态及固定原因
  invocation.json          # OPTIMIZER角色、诊断、耗时、已知用量
  manifest.json            # 最终状态与candidate_executed=false
```

产物文件0600、目录0700，不覆盖历史。若提示OUTPUT_ALREADY_EXISTS，先检查已有结果；不要删目录来自动重跑。后续人工决定新一次请求时另用新目录名。输出不能嵌入旧反馈目录，反馈产物中的符号链接也会被拒绝。

## 6. 怎么理解结果

| 最终stage | 含义 |
|---|---|
| VALIDATED | 模型提出的候选通过规则校验，可以进入未来第三步实验；不表示有效或已经执行 |
| NO_CHANGE | 模型认为目前没有合适调整，流程正常结束 |
| REJECTED | 建议越界、引用无效、重复失败方案或结构不符；保存固定拒绝原因，不擅改为另一建议 |
| OPTIMIZER_FAILED | 建议模型超时、接口/解析失败等；与证据评估失败分开记录，CLI退出码2 |

CLI不打印假设、供应商原文或异常原文，只显示固定状态与散列。需要看模型建议时读取私有proposal.json。敏感/无效响应的proposal为null，不能从散列还原原文，也不冒充已经保存完整原始HTTP响应。

工程拒绝也可以是正常完成的建议流程，CLI为0；VALIDATED不等于“优化成功”。这一步没有独立效果评分或自动采纳。

## 7. 验收结果

相关回归262项通过，覆盖第一/二步、启动器隔离、固定输入、诊断、输出额度、Shadow集成及原证据回答。全部使用隔离数据、deterministic Fake或HTTP替身；没有真实付费请求。有一条既有Jieba依赖弃用提示。

| 方案第二步要求 | 验收情况 |
|---|---|
| NO_CHANGE与合法单变量候选 | 均验证；安全建议原样保存，不预定真实策略 |
| 两变量、非法字段/值、布尔值、额外Shell字段 | 拒绝，不修补后重问 |
| 编造/重复/异条件反馈引用 | 拒绝 |
| 重复同条件历史失败Profile | 拒绝；不声称已完成基于Gold的效果比较 |
| 真实客户端接线 | HTTP替身穿过真实客户端，核对实际消息载荷、max_tokens、超时及零重试 |
| 真实发送载荷的Gold/原文/身份/路径隔离 | 已检查client实际收到的payload，而不只检查磁盘文件 |
| 建议模型网络、格式和未知异常 | 单独OPTIMIZER_FAILED；不变成MISSING或EVALUATION_FAILED；最多一次请求 |
| 失败/中断不自动重试，产物不覆盖 | 请求前STARTED、目录排他创建、重复调用被阻止 |
| 未知用量不当0，敏感响应不落盘 | 已验证 |
| 源报告被改、产物散列不符、链接越界 | 调用前拒绝，零模型请求 |
| 原配置、数据、反馈与Shadow保持不变 | 真实检查前后文件散列一致；原链路相关回归通过 |

零调用真实反馈验收保存在`runtime/evidence-test/experiments/harness-v1/step2-acceptance-v1/acceptance.json`。真实反馈18条历史记录可构造建议载荷；实际模型建议尚未请求，不能把Fake的VALIDATED当作模型已给出有效改进。

可复测命令与第一步相同，新增测试文件：

```bash
DATABASE_URL=postgresql://fake:fake@127.0.0.1/file_agent_unit_tests \
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest -q \
  apps/api/app/tests/test_evidence_harness_optimizer.py \
  apps/api/app/tests/test_evidence_harness_feedback.py \
  apps/api/app/tests/test_evidence_test_environment.py \
  apps/api/app/tests/test_frozen_evidence_inputs.py \
  apps/api/app/tests/test_evidence_evaluation_diagnostics.py \
  apps/api/app/tests/test_evidence_output_budget.py \
  apps/api/app/tests/test_evidence_evaluation_integration.py \
  apps/api/app/tests/test_evidence_answer.py
```

下一步才开发受控复测执行器及独立比较器，落实总预算、实际实验Profile、逐轮Gold评分和中断记录；当前CLI没有run能力，不会自动执行或上线。
