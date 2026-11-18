# 证据评估器测试入口

扩展方案见[从legacy单条规则到Adaptive自主规划优化](harness-adaptive-autonomy-next-plan.md)。A阶段见[基线步骤](harness-adaptive-stage-a-usage.md)；B阶段见[开发方案](harness-adaptive-stage-b-plan.md)和[操作步骤](harness-adaptive-stage-b-usage.md)，已有真实v2首轮规划从1/2到2/2的改善。C工具已开发，见[C方案](harness-adaptive-stage-c-plan.md)和[C步骤](harness-adaptive-stage-c-usage.md)；14题已确认冻结，首次C因旧模型额度耗尽得到INCONCLUSIVE。更换模型时按[补充方案](harness-stage-c-model-change-plan.md)显式允许两组共用新模型复测，默认同模型检查保留。默认检查零调用，显式live调用模型，不切换线上模式；D完整执行尚未开发。

C现支持[本机Codex登录代理](harness-codex-login-plan.md)，完整命令见[操作第12节](harness-adaptive-stage-c-usage.md#12-改用本机已经登录的codex)。仅改变本次实验的模型调用，明确区分CLI调用预算和不支持的Token硬限制，旧实验只读保留。

C的选择也可写入测试env，见[env配置方案](harness-provider-env-plan.md)和[操作第13节](harness-adaptive-stage-c-usage.md#13-在env中切换codex和原api)。保留原API，CLI优先于env；本机测试env现已选择Codex，不影响普通聊天或后台服务。

Codex整批REQUEST_REJECTED已定位为非致命启动提示被误判工具，见[修复方案](harness-codex-startup-event-fix.md)。下一步先按[第14节](harness-adaptive-stage-c-usage.md#14-codex启动事件修复后先单次探针)执行`codex-probe --live`单次固定假问题，成功后才用新目录复测；不根据旧回退分数判断候选有效。

## Harness自主改进四阶段开发计划

以下四阶段最小开发及工程验收已完成，操作见[大白话测试步骤](harness-self-improvement-usage.md)。已经接通机器反馈、模型自主生成受限代码条件、隔离执行和独立验收；原预置提示策略入口继续保留。本次真实模型调用为0，真实模型优化效果待live测试。

| 开发顺序 | 简单开发文档 | 完成后获得的能力 |
|---|---|---|
| 1 | [机器可读根因反馈](harness-self-improvement-phase1-feedback-plan.md) | 把模型失败、规则降级及后端覆盖意图整理成可读反馈 |
| 2 | [候选补丁生成](harness-self-improvement-phase2-patch-plan.md) | LLM依据反馈和受控源码上下文生成真实代码补丁 |
| 3 | [隔离执行](harness-self-improvement-phase3-isolation-plan.md) | 在受限制的独立环境应用补丁并执行固定检查 |
| 4 | [自动验收与有限迭代](harness-self-improvement-phase4-acceptance-plan.md) | 独立评分，反馈下一轮，在总预算内自动试错 |

依次验收后进入下一阶段。首批以R002和SEARCH01验证首轮路由闭环，不代表检索或最终答案质量已经验收；通过的补丁只保留为候选，正式启用另行审批。

工程验收：176项相关回归通过，另4项真实系统隔离测试通过；两轮Fake实验先得到NO_GAIN，再读取反馈形成IMPROVED。不能将假模型工程结果宣称为真实模型已自主优化成功。

## 现有能力与测试入口

已有任务被错误分到列文件时，可使用[首轮路由Harness](routing-harness-minimal-plan.md)导出实际问题和上下文，人工确认路由标签后进行建议与受控规划对照。现有失败AgentRun可直接作为来源。

反馈驱动的离线实验已完成[第四步：完整流程与历史反馈](harness-step4-acceptance.md)。可用`harness cycle`一次串起建议、校验、复测与独立评分，默认只检查；真实运行需显式`--live`，每轮只尝试一个候选，已验证无效的同条件方案会被拒绝。

扩大样本时见[3题扩充到15题的准备方案](harness-expanded-input-plan.md)：已补选12题真实材料候选，完整题干及来源保存在本机私有清单。新增证据尚未冻结，Harness当前仍限定3题，不能直接套用19次预算运行15题。

文件准备完成后，按[新增12题实际检索证据采集步骤](expanded-real-input-capture-steps.md)生成新计划、重启API/analysis并在前端逐题提问；原三题快照保持不变。

**当前请用[独立新库与24项精简env](standalone.md)**：实际配置在`runtime/evidence-test/.env`，不继承原.env，配套管理员建库SQL和独立启动命令。以下覆盖配置方式保留为旧用法。

**本次验收使用你自行上传的真实文件，按“上传→解析→全文索引→检索→评估”完整执行。**
请直接阅读[真实文件测试步骤](real-files.md)，并使用[evaluation-test.env.example](evaluation-test.env.example)。
真实流程按Jieba正常分词验收，需要先补齐该依赖；不要求Embedding或Neo4j。
下面的合成证据包/示例TXT仅保留供开发回归参考，不作为这次真实文件验收输入。

准备人工验收集时，使用[60条样本构建与计分方案](manual-60-plan.md)和[Excel空白模板](manual-60-template.xlsx)。
模板预留四组各15条、三轮测试记录及P/R/F1、Token和延迟统计；金标与模型结果仍需实际填写。
OUTDATED正例需要真实版本链及旧证据回放，不能仅上传两个独立文件来代替。

已另准备[工作文档真实选材与40题人工判定包](workdocs-question-pack-20261031.md)：从指定硬盘选用24份原文件，附参考回答和原文位置，最终标签留给人工填写。

当前阶段先完成[真实评估输入冻结与可重复评测](frozen-real-input-plan.md)：保存实际证据包、同次版本事实与时间意图，直接评测评估器；明确现有脚本能力和仍需开发的受控导出入口，暂不把规划性能或决策策略优化作为主线。

该能力现已实现，执行步骤见[真实快照采集与独立评测操作文档](frozen-real-input-usage.md)。真实快照需重新运行题目采集，尚未自动生成。

这份测试包用于新增的MISSING / CONFLICT / OUTDATED评估器。
你不必先做Embedding。当前检索使用CPU全文索引，评估器本身只消费已找到的原文证据。

## 1. 先分清两种测试

| 方式 | 是否需要上传文件/建索引 | 是否需要Jieba或Embedding | 测什么 |
|---|---|---|---|
| 已有64条证据包，replay/rules | 不需要，问题、证据和版本快照已在项目JSON里 | 都不需要 | 工程链路和技术版本规则 |
| 已有64条证据包，live | 不需要上传，直接把已有证据送给模型 | 都不需要 | 真实LLM的缺项/冲突判断；版本仍用冻结快照 |
| 聊天上传下面的TXT，再提问 | 需要上传、解析、建立全文索引 | 不需要Embedding；Jieba缺失时有备用分词 | 从文件处理、检索到评估器的完整流程 |

Jieba缺失时，`ChineseLexicalTokenizer` 会改用原有确定性分词：英文/数字词项、连续中文及中文二元词。
因此不是“完全不能检索”，但与Jieba分词的匹配效果可能不同。
正式比较检索质量时应补齐Jieba，并确保索引与查询使用相同分词实现；现有索引不能因为切换分词自动视为一致。
本测试包不安装依赖，也不会自动重建任何既有文件的索引。

## 2. 使用测试配置

配置文件：[evaluation-test.env.example](evaluation-test.env.example)。
它只覆盖评估器、全文检索和图谱开关，不含数据库密码或模型密钥，**不要拿它替换根目录`.env`**。
数据库地址、LLM_API_KEY、LLM_BASE_URL、LLM_CHAT_MODEL继续从你已有的根`.env`读取。

在仓库根目录、新终端中执行：

```bash
set -a
source docs/evidence-evaluation-test/evaluation-test.env.example
set +a
```

这一步只影响该终端及其新启动的子进程，不修改`.env`，也不会改变已经启动的API/worker。
真实模型测试会调用你配置的LLM并计费；这里提供命令，没有替你执行真实调用。

## 3. 开发回归参考：合成证据包

项目已包含64条合成案例：`apps/api/app/tests/fixtures/evidence_evaluation_cases.json`。
先运行不调用模型的回放：

```bash
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python \
  -m app.scripts.evaluate_evidence --mode replay --repetitions 1 \
  --output /tmp/evidence-replay.json
```

准备好模型连接后，你可以自行运行真实模型评估：

```bash
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python \
  -m app.scripts.evaluate_evidence --mode live --repetitions 1 \
  --output /tmp/evidence-live.json
```

live不需要正在运行的API、文件worker或可连接的数据库；当前配置加载仍要求有格式正确的DATABASE_URL。
它只使用现有证据和冻结版本快照，并连接LLM，不把预期标签发送给模型。
真实缺项/冲突分数不能用replay结果替代；合成标签尚需业务人员独立复核。

## 4. 开发回归参考：示例TXT（本次真实文件验收不使用）

材料在[samples目录](samples/)，共5份UTF-8 TXT。
文件内容都是虚构的测试数据；只上传指定组合，不要把5份一起当作同一个问题范围。

需要原来的API、上传/归档/物化worker及正文分析worker工作，并能连接现有数据库。
在各进程启动终端先加载第2节配置，再按原来的启动方式启动；已经启动的进程需要你自行重启才能读到新环境。
API的已有启动命令（从仓库根执行）：

```bash
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python \
  -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

worker仍使用[runbook](../runbook.md)中的既有启动方式。本测试不需要Neo4j、GRAPH worker或Embedding模型。
不要为测试清空数据库、重置文件或改写真实原件。

每个案例新开一个会话，只上传该案例材料，同时发送明确任务文字：

```text
请读取并整理这些测试文件，用于接下来的内容问答。
```

等待文件处理和索引完成后提问。如果系统提示相似文件/重复内容，核对清楚后保留本案例需要的独立材料。
如果出现文件选择卡，明确选择该案例需要的文件，尤其是冲突案例的两份记录。

| 案例 | 上传文件 | 随后提问 | 预期评估 |
|---|---|---|---|
| 证据足够 | 01-完整材料.txt | 当前材料规定的最终批准预算和申报截止日期分别是什么？ | 无MISSING/CONFLICT/OUTDATED |
| 缺一年 | 02-部分年度材料.txt | 根据这份文件，青禾项目2023年和2024年的科研经费分别是多少？ | MISSING：缺2024年经费 |
| 同口径矛盾 | 03-晨光预算记录甲.txt、04-晨光预算记录乙.txt | 当前这两份材料对晨光项目的最终批准预算是否一致？分别是多少？ | CONFLICT：同年同范围最终批准预算500万与450万 |
| 不同口径反例 | 05-不同口径材料.txt | 当前材料中的申请预算和最终批准预算分别是多少？ | 不应因500万与450万而报CONFLICT |

这些是预期标签，不是保证真实模型一定答对。即使检测到问题，shadow也只记录，不改原答案。

## 5. 去哪里看结果

聊天页面不会新增评估卡片。完整流程测试查看 `LOG_DIR` 下当天JSONL日志中的：

```text
event = evidence_evaluation.shadow
issue_types = MISSING / CONFLICT / OUTDATED 的集合
status / version_status / semantic_status
error_codes / limitation_codes
```

`EVALUATION_FAILED`不是“没有问题”，需要看错误码；`NOT_EVALUATED`表示跳过；
`PARTIAL`可能是时间意图不明、版本无法核验或证据跨批等，不等于全部证据齐备。
如果根本没有证据包，原回答会提前返回NO_EVIDENCE，本次评估器不会被调用；先确认材料已入库、正文索引完成及文件范围正确。
测试配置关闭答案缓存，便于每次新生成时留下一条评估记录。

## 6. OUTDATED不能靠“旧日期TXT”来测试

OUTDATED要证明：同一工作副本/源文件真实产生了新内容版本，当前版本索引已完成，
而送入评估器的证据仍绑定旧版本，同时问题明确询问当前状态。
把两份不同日期的TXT上传成两个独立文件，不构成这种版本替换关系。
正常回答链优先读取当前版本，因此也不保证在聊天中人为制造旧证据命中。

先用已有的冻结快照或隔离ORM测试验证：

```bash
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python \
  -m app.scripts.evaluate_evidence --mode rules --repetitions 1 \
  --output /tmp/evidence-versions.json
```

集成测试 `test_working_copy_versions_are_read_only_and_historical_safe` 与
`test_source_revision_uses_its_own_current_version_path` 会在隔离库中构造版本替换，
并测试历史查询、当前索引未就绪等反例，不修改现有文件和真实数据库。

## 7. 测试结束

这份覆盖配置没有改根`.env`。关闭测试终端，在未加载覆盖配置的新终端中按原方式重启相关进程，即恢复原配置。
此次仅新增配置模板、测试TXT和说明，没有替你启用评估、安装Jieba、下载模型、启动进程或上传测试文件。
