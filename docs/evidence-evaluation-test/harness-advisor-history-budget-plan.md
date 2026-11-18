# 建议器历史越积越长：最小修复方案

日期：2026-11-17。按修改前冻结的方案完成实现及工程验收，未启动真实模型。本次只修实验控制器的输入管理。

## 1. 已核实的问题

`frozen-exploration-v1`尝试了两份候选，第3轮准备调用建议器时停止。原始输入按程序同一算法重建为413,642字符，超过宿主400,000字符限制；停止原因是`SELF_IMPROVEMENT_REQUEST_TOO_LARGE`，不是本次复测命令写错，也不是600次预算或7200秒用完。

历史里反复携带每份候选的逐题完整诊断，因此轮数增加会撑大请求。旧实验为`STOPPED`，复测入口要求`COMPLETED`，所以报`RETEST_SOURCE_NOT_COMPLETED`。完整记录已封存；不能手改状态、删除错误或重跑到同一目录。

## 2. 只修哪里

保留本地完整`history.json`、`feedback.json`、原始调用、试次、评分和封存。新探索改为给模型发送可重建的历史摘要，而不是反复发送旧逐题报告：

- 每份尝试保留身份、策略引用、分数、是否可评估、退步及失败信息；相同Profile用引用和共享表表示。
- 所有历史观察按原版/候选分别统计对错、执行状态、回退、固定预检、参数约束、契约不匹配、违规和调用错误。错误次数精确保留，摘要不推测业务根因，不把未验证当通过。
- 通用策略文字优先保留，过长时只展示明确标记的头尾节选，同时保留全文散列和省略字数。少量完整诊断示例在独立长度额度内选取，优先近期失败；明确示例不等于全部记录。
- 当前轮完整反馈、短编号枚举、Tool契约、输出schema保持原样，仍能从编号精确关联完整原反馈。
- 历史视图最多200,000字符，整条建议输入最多360,000字符，低于宿主400,000限制；使用宿主相同计量口径。先减少示例和策略节选长度，不截断JSON，不丢候选或错误统计。必需上下文或统计本身放不下时明确停止，不提高上限、不额外请求模型做摘要。

每轮新增`advisor-memory.json`，记录模型实际看到的历史视图、原历史身份、固定投影策略、请求身份及字符数。独立比较器从完整历史重新投影，逐字段核对，不能信缓存摘要。

## 3. 新旧记录怎么兼容

新探索登记`adaptive-campaign-experiment-v3`；旧v2保持原提示、原始完整历史输入和原停止结论。登记已独立核验的v2控制器身份`6e91f9175a389b309ab5b54a8950168ad56f508ab720fb2bb04e96fdcde15f25`，继续独立重放旧记录，不改写排名或失败。

`campaign-run`零调用检查同时组装第一轮摘要，显示请求字符数，避免开跑才发现历史过大。正式测试中每轮重新生成并核对摘要。提示词明确“你看到的是有长度约束的历史摘要；没有展开详情不代表没有错误”，保留合理NO_CHANGE，不预写业务策略。

仍禁止将STOPPED搜索直接交给`campaign-retest`。新实验使用`--source .../frozen-exploration-v1`承接两份已测候选和全部可核验开发历史，写到新`frozen-exploration-v2`。新预算需要明确授权，不自动续跑。完成后再复测新目录。

## 4. 不改哪些规则

候选仍由模型自主生成；冻结、OS隔离、完整Gold、总请求和总时间、最终严格门槛、保留集独立性不变。不改正式Planner、Tool、数据库、env、真实文件或已有实验。不自动调用真实模型。

## 5. 验收与交付

Fake验证历史压缩的确定性和不修改原对象、全部候选及错误计数保留、Profile引用、文字省略标记、示例额度、超限明确拒绝、当前反馈短引用保持、摘要篡改拒绝、十轮循环、旧协议回归。只读重建真实第3轮，证明旧请求仍为413,642且新投影低于上限；旧v2比较结果及旧目录散列保持一致。运行真实OS专项，仍只使用Fake模型。更新操作步骤、README及runbook。

## 6. 实际新增与修改的位置

以下代码路径均在`apps/api/app/`下，本次只增加实验控制器能力，没有给任何题目手写业务策略。

| 文件 | 新增/修改 | 加在哪个环节 | 大白话说明 |
|---|---|---|---|
| `modules/evidence_harness/improvement/advisor_memory.py` | 新增 | 调建议器之前 | 精确统计所有旧错误，按固定长度生成历史视图并记录身份 |
| `modules/evidence_harness/improvement/campaign.py` | 修改 | 实验检查、历史承接与建议提示 | 新v3协议；零调用检查显示输入长度；兼容旧v2 |
| `modules/evidence_harness/improvement/adaptive_iteration.py` | 修改 | 每轮建议调用 | 完整历史继续保存，发送摘要并写入`advisor-memory.json` |
| `modules/evidence_harness/improvement/iteration_comparator.py` | 修改 | 独立比较 | 从完整历史重建并核对摘要；保留旧v2原停止结论 |
| `modules/evidence_harness/improvement/campaign_retesting.py` | 修改 | 研究候选来源识别 | 认识新v3，仍只接受完整结束的来源 |
| `scripts/evidence_harness.py` | 修改 | 命令行检查回执 | 显示建议输入字符数与限制 |
| `tests/test_advisor_memory.py` | 新增 | 自动验收 | 检查有界摘要、统计、当前反馈不变、篡改/缺失拒绝和停止留档 |
| `tests/test_campaign_retesting.py` | 修改 | 十轮回归 | 每轮检查摘要与完整历史身份，保证十轮都能独立重算 |

新增本文，更新原探索方案、[操作文档第8节](harness-exploration-retest-usage.md#8-遇到retest_source_not_completed后怎样继续)、README及runbook。模型配置、正式Planner、Gold、Tool schema和旧实验目录不变；宿主400,000字符上限保持原值。

## 7. 已核验的真实记录

- 同一旧第3轮：413,642字符；新历史投影：107,838字符，上限360,000；8份历史尝试全部保留，当前反馈完全相同。零模型调用，报告在`runtime/evidence-test/experiments/harness-v1/advisor-memory-real-preview.json`。
- 新`frozen-exploration-v2`只读检查：`CHECK_PASSED`，10题、3批历史、7份候选、历史229次调用；第一轮建议输入108,069字符。不创建输出目录，未启动live。
- `frozen-exploration-v1`、旧搜索和旧迭代的比较结果与原文件逐字段相同。旧探索仍是STOPPED。四批实验共10,603个文件散列不变，摘要在`advisor-memory-readonly-checks.json`。
- 真实macOS隔离复测专项：1通过，使用Fake模型，报告`advisor-memory-os.xml`。
- 最终相关回归：74通过、3跳过，报告`advisor-memory-regression.xml`。覆盖有界历史、两种摘要篡改/缺失、80份候选全部计数、十轮冻结、五/十次复测、旧实验管理、短编号和原迭代协议；三项OS用例按默认条件跳过，其中复测专项已另行在真实OS执行通过。之前单独摘要专项6通过是开发中检查，不重复累计。
- 新增/修改模块语法检查和`git diff --check`通过。报告均位于`runtime/evidence-test/experiments/harness-v1/`根目录，不写进已封存实验内部。

摘要改变的是建议器阅读旧反馈的方式，不是评测题目、Planner输入或评分标准。该修复不证明模型业务策略已经改善；仍需另行live测试。
