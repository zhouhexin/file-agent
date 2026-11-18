# Harness首轮路由策略实验最小开发方案

日期：2026-11-11。状态：方案先行写入，首轮路由最小版已按第1—7节实现，工程验收见第9节。依据为`agent.md`、自适应Planner实施方案及现有反馈驱动Harness方案B阶段。

## 1. 目标与本次交付边界

针对正文事实提问被分到`managed-file-list`的失败，增加独立离线路由实验入口。流程为真实失败记录导出、安全反馈、LLM提出一个预置路由策略、校验、同版控制与候选复测、独立评分、保留结果供下一轮读取。

第一版复用现有LangGraph `planning` 节点及其Legacy/Adaptive校验和降级链，执行到首轮声明式计划生成及Tool输入schema校验。评分包括首轮路由、明确附件范围保持、未知文件ID、危险Tool和原有正常题退步。报告标记`evaluation_level=INITIAL_ROUTING`。`CONTENT_ANSWER`表示计划进入证据回答或证据发现路线，报告同时保存实际入口Tool。

检索Tool执行、证据覆盖、最终答案正确性需要后续任务执行器扩展。首轮计划通过不能记为已检索成功或已回答成功。当前用户提交时为空的附件保持为空，参考答案中的F编号和预期Document ID不能作为规划输入补入。这样能复现这次无范围提问的路由问题。

本次新增离线入口，默认只检查，显式`--live`才调用配置的模型。部署设置、线上Planner提示词和现有A阶段协议继续由各自入口管理。

## 2. 输入、来源与人工标签

`routing-prepare`通过独立测试库只读事务按操作者给定的user ID和AgentRun ID导出。只接收已结束、首轮有规划结果的运行。原消息、实际attachments及持久化context_documents来自服务端记录。导出上下文仅保留ID、文件名、类型和状态，持久化摘要不进入这一版实验。导出策略版本标记为`routing-context-metadata-v1`，因此本次同版控制组重新执行，不把旧输出当成控制组。

源运行仅持久化Catalog身份和启用名称，完整Catalog使用当前已实现的ToolRegistry及Skill manifest构建，并固定完整快照和指纹。导出文件记录原Catalog身份和当前实验Catalog，明确当前代码下的复测身份。导出不把旧工具输出、答案、路径、用户身份或正文发给建议模型。

导出目录包含`inputs.json`和`gold-draft.json`。Gold只允许`CONTENT_ANSWER`、`FILE_LIST`、`OTHER`，每题人工填写预期类别、理由及`confirmed=true`。正常文件查找应有FILE_LIST反例。至少一条CONTENT_ANSWER和一条FILE_LIST才允许真实策略实验，以检测“全部改为问答”的退步。

`routing-freeze`验证输入/标签逐题对应和人工确认，新增不可覆盖的数据集文件并绑定散列。规划器仅接收输入，Gold由独立比较器读取。源资料、完整问题和实验请求仅写入Git忽略的私有测试目录，文件0600。

## 3. 唯一候选策略

控制策略ID为`baseline`。候选仅允许`content_question_priority_v1`，使用开发者固定提示附录，强调“列出文件正文中的对象、日期、金额并引用原文”属于正文任务，“列出/查找文件”属于文件列表，保留缺范围时先发现文件的能力。

附录由实验专用client在内存中附加到Legacy及Adaptive请求的system prompt，用户原句、附件、目录、模型及代码保持同版。模型只选择策略ID或NO_CHANGE，不能输出任意Prompt、代码、Tool实现、预算、路径或扩大权限。

新建议协议独立于A阶段，字段为decision、feedback_refs、strategy_id、hypothesis。建议模型看到来源失败的安全摘要，例如观察路由、入口Tool枚举、是否有明确附件、是否到过证据回答、运行是否失败，以及已有独立实验结论。它看不到问题、正文、人工预期路线、Gold理由和实际用户/文件ID。

程序对已确认来源标签进行独立核对，仅把`SOURCE_ROUTING_MISMATCH`或`SOURCE_RUN_FAILED`等固定失败类别加入反馈，用于说明哪些来源确有错误；预期类别及人工理由仍只由本机裁判读取。

历史实验必须通过独立比较器重新核验。相同数据、配置和执行代码下，完整NO_GAIN/REGRESSION的同一策略再次提出时拒绝。Fake历史仅供Fake实验使用。首版每次只评一个候选，NO_CHANGE、拒绝、建议失败立即保存并停止。

## 4. 复测与预算

每题原输入构造初始AgentGraphState，经小型LangGraph调用现有`planning`函数。运行依赖使用AgentRuntimeContext。ToolRegistry只提供真实元数据与schema，复测图没有Tool执行边。首轮涉及写操作或需要确认的计划记录为不允许的路由结果，不执行其handler。

每题每组可重复1—3轮。正常规划、Adaptive失败回退Legacy、参数修复等模型请求全部经统一计数client。真实请求复用现有ProcessTransport，总等待60秒、内部重试0。规划请求输出上限为3000 Token；建议器使用独立的`--advisor-max-output-tokens`，默认3000，允许3000/6000/8000，由操作者在CLI设置。每次试验最多3个规划请求，超出以固定错误停止本次试验。

2026-11-11根据首次真实截断追加这一最小调整。建议器额度写入RoutingAuthorization和逐请求STARTED/FINISHED记录，默认检查展示建议器与规划器各自额度，比较器按请求角色核验实际参数。旧authorization缺少该字段时按原3000读取。模型建议不能改变这一授权。

历史比较按保存的生成时代码指纹、配置散列、完整性清单和既定路由协议核验，不再把“生成时代码指纹必须等于当前源码”作为有效性条件，以支持增加额度参数后继续核验旧实验。指纹必须是合法64位散列，配置及产物绑定仍须通过。历史数据集、Gold、Profile或发送记录的绑定不一致仍返回INVALID_EXPERIMENT。

整轮预算包含建议1次和两个组的实际规划请求。最坏请求数为`1 + 2 × 样本数 × 重复次数 × 3`。CLI默认19请求、1800秒，允许操作者显式设定新的正整数授权上限，建议模型无法修改。预检查展示计划上界和授权是否足够，低于上界仍允许受预算控制的部分执行。全局预算耗尽立即停止，未执行项继续计入分母。

首版授权schema上限为1000请求、7200秒，每组重复1—3轮。最多继承8份历史、递归8层，超出明确报错。建议未通过或未完成建议调用时，比较标记`NOT_RUN`并保留实际终止状态。

发送前写请求STARTED记录，返回或异常写FINISHED记录。Token未知保留null，所有失败请求计次。中断保留已完成轮次及发送记录，强制结束缺最终清单时可只读检查为UNFINALIZED，输出目录不能续写覆盖。使用`--previous`读取历史只用于新一轮反馈，不自动补发旧调用。

输出也不能选在已有实验目录内部，避免新增子文件改变旧实验的完整性快照。

## 5. 独立比较

独立比较器从绑定数据集、逐轮规划输出和调用记录重算，验证实验输入、标签、策略、代码、模型端点散列、请求预算、文件完整性。忽略缓存comparison的自述结论。

类别规则固定在程序中。首步为evidence-answer，或具有证据发现intent且首步为hybrid-search，计CONTENT_ANSWER。managed-file-list或普通文件搜索计FILE_LIST，其他为OTHER。预期类别不参与分类逻辑。

文件范围核验分两部分。所有计划ID必须来自原附件或后端原上下文，未知ID判失败。原输入具有明确附件时，计划不得丢失或加入附件范围之外ID。无明确附件时仅能核验没有编造ID，目标文件是否找到标为未执行。

主指标为“路由匹配且计划范围校验通过次数/全部预定轮次”，失败和未执行按失败计。另报每类P/R/F1、逐题原正常退步、已知Token/未知用量数、耗时与停止原因。任一题控制得分下降或发生安全失败记REGRESSION，完整改善记IMPROVED，完整无改善记NO_GAIN，部分记INCONCLUSIVE，绑定或完整性失效记INVALID_EXPERIMENT。改善只保留为待复核实验候选。

## 6. 文件范围

新增`apps/api/app/modules/evidence_harness/routing/`包，包含严格协议`schemas.py`、来源导出与冻结`dataset.py`、实验策略与规划适配`runtime.py`、闭环与预算记录`service.py`、独立比较`comparator.py`。修改现有Harness CLI新增routing-prepare、routing-freeze、routing-cycle、routing-compare。增加确定性Fake测试及操作验收记录。更新README、runbook及原Harness方案进度链接。

现有planning日志可包含Tool输入。为保护离线任务原句，在`core/logging.py`的`log_context`增加仅当前上下文生效的`metadata_only`投影，保留事件、状态、固定错误码及耗时，路由实验调用时启用。普通请求沿用默认日志行为。

复用现有`digest`、`write_new_json`、私有路径校验、严格JSON读取、ProcessTransport、诊断协议、ToolRegistry、AgentCatalogService和LangGraph规划节点。A阶段的反馈/建议协议及执行器无需重构。

## 7. 验收条件

1. 真实历史读取为只读，原问题和附件保持可追溯，空附件不会被Gold补全，人工未确认标签拒绝运行。
2. 建议的请求载荷无问题、正文、身份和Gold，合法建议才有控制与候选，NO_CHANGE及非法/失败建议不复测。
3. Fake分别复现改善、无改善、全部改问答导致正常搜索退步、中断、预算耗尽、模型异常和无效输出。
4. 真实规划函数及schema校验被调用，候选附录仅存在实验client；保护明确附件、未知ID及写操作边界。
5. 比较器可独立重算，检测产物/Gold/输入/配置被改动，缓存结论不能影响历史事实。
6. 部分执行仍有完整计划分母，建议及所有回退请求计入预算，Token不完整不当0，目录不可覆盖，中断无自动续跑。
7. 相关路由和既有A阶段回归通过。开发验收采用Fake，不自动执行新的收费模型实验。

后续真实实验需用户先标注导出的路由Gold，再使用默认检查及显式live。开发交付时说明工程验收、真实未执行项和本次首轮评分边界。

## 8. 具体操作

### 8.1 从已有任务导出输入

路由实验可以直接读取已有失败AgentRun。先选正文路由失败题，例如R002，再选一条用户确实要求找文件的正常运行作反例。运行ID从原日志的`agent_run_id`读取，用户ID使用同一测试账号。下列引号中的文字均需换成实际值。

首次验收可先补一条SEARCH01，在前端输入“查找2024年的教工党支部换届通知，列出文件名即可。”，等待任务结束后，使用`rg --no-ignore -n -F 'agent.run.completed' runtime/evidence-test/logs/`查看本次`agent_run_id`，并按同一ID核对时间和`agent.planning.final_tool_plan`。预期是FILE_LIST。正文提问R002的已有失败运行可以直接复用。两题先跑一轮，默认请求授权足够覆盖最坏的13次上界。

在项目根执行：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py harness routing-prepare \
  --user-id "替换为测试账号UUID" \
  --run-ids "替换为R002的运行UUID" "替换为正常找文件运行UUID" \
  --case-ids R002 SEARCH01 \
  --output datasets/routing-real-v1
```

成功返回`AWAITING_ROUTING_GOLD`。实际文件位于`runtime/evidence-test/datasets/routing-real-v1/`，包括原问题及实际附件的`inputs.json`、待标注的`gold-draft.json`和来源清单。该步骤只读查询测试库，模型调用为0。

输入是当前Catalog、当前规划代码及原上下文元数据下的新实验，原始任务Catalog身份保存在`source_catalog_refs`。后续代码或Catalog改变需要重新检查实验身份。同版控制与候选都重新执行。

### 8.2 填写路由Gold并冻结

人工查看每题原问题，在`gold-draft.json`中填写预期类别、理由及`confirmed=true`。示意结构如下：

```json
{
  "cases": [
    {"case_id": "R002", "expected_route": "CONTENT_ANSWER", "reason": "要求说明通知正文中的范围和期限并引用原文", "confirmed": true},
    {"case_id": "SEARCH01", "expected_route": "FILE_LIST", "reason": "用户只要求找到文件及展示文件清单", "confirmed": true}
  ]
}
```

上例只在实际导出的问题符合这些理由时适用。`OTHER`可用于其他初始分支。首版live同时要求CONTENT_ANSWER和FILE_LIST样本，以便检测把正常找文件也改成问答的退步。

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py harness routing-freeze \
  --inputs datasets/routing-real-v1/inputs.json \
  --gold datasets/routing-real-v1/gold-draft.json \
  --output datasets/routing-real-v1/cases-v1.json
```

冻结成功返回`ROUTING_DATASET_FROZEN`。本次使用路由类别标签；原证据状态评测标签继续用于各自的数据集。

### 8.3 默认检查，再显式执行

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py harness routing-cycle \
  --dataset datasets/routing-real-v1/cases-v1.json \
  --output experiments/harness-v1/routing-live-v1
```

默认返回`CHECK_PASSED`、题数、请求上界和授权是否覆盖，模型调用为0。两题各一轮的最坏上界为13次，正常无回退时通常为5次。确认需要实际调用后，对同一条命令追加`--live`。

若导出8题各一轮，请求上界是49，可显式传`--max-requests 49 --max-active-seconds 3600`。真实时间以实际模型、回退及准备开销为准，达到授权即保存并停止。每组可用`--repetitions 3`检查稳定性，此时按第4节公式重新检查总预算。

模型读取安全失败反馈，选择`content_question_priority_v1`或NO_CHANGE。建议合法才运行同版两组。策略只影响此次实验的规划请求。结果包括路由类别、计划范围校验、逐题退步、调用数、Token已知小计及未知调用数、耗时和停止原因。

### 8.4 只读比较与下一轮

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py harness routing-compare \
  --experiment experiments/harness-v1/routing-live-v1
```

后续新一轮在`routing-cycle`中加`--previous experiments/harness-v1/routing-live-v1`，并改用新输出目录。它会继承前轮已有历史，独立复算结论。同条件下再次建议完整无改善的相同策略会被拒绝。历史实验按保存的生成时代码指纹及既定比较协议读取；依赖版本或协议参数不兼容、配置及文件绑定失败时，会明确返回INVALID_EXPERIMENT，需要核对原实验环境。

产物结构如下：

```text
routing-live-v1/
  experiment.json       # 输入、Gold、Catalog、代码、策略、预算与历史来源
  feedback.json         # 实际送给建议器的安全反馈
  proposal.json         # 合法建议或固定拒绝原因
  calls/                # 每次建议、规划和回退的STARTED/FINISHED
  control/rounds/       # 原策略逐题首轮计划
  candidate/rounds/     # 候选策略逐题首轮计划
  run-summary.json      # 实际调用及终止状态
  integrity.json
  comparison.json      # 独立重算结果，可重新检查
  manifest.json
```

达到预算或人工中断时，未执行轮次保留在评分分母中。正常不调整或建议被拒绝时比较为NOT_RUN。改进只针对`INITIAL_ROUTING`，`target_file_retrieval`和`answer_quality`均标记`NOT_EXECUTED`。

## 9. 工程验收记录

新增模块为`routing/schemas.py`、`dataset.py`、`runtime.py`、`service.py`、`comparator.py`及包初始化。新增`test_routing_harness.py`，修改CLI和上下文日志投影，并更新项目入口文档。

已验证的行为如下：

| 文档要求 | 验收 |
|---|---|
| 数据导出按本人运行约束、只读连接、未结束运行拒绝 | 导出器连接替身验证只读参数及SQL绑定，Gold保持草稿 |
| 原问题、实际空附件、Gold隔离 | 使用真实规划节点测试，模型请求未补入预期文件或Gold |
| 建议、候选、校验与独立评分串联 | Fake覆盖改善、无改善、正常题退步及越界ID |
| NO_CHANGE、非法字段、编造引用、优化器超时 | 仅一次建议请求，不进入规划复测 |
| 统一预算、内部回退与Shadow请求 | 请求逐次计数，Adaptive回退和Shadow也计入；每题3次封顶 |
| 预算停止、中断、未知用量与只读检查 | 保留发送记录、未执行分母及停止原因，输出目录拒绝覆盖 |
| 旧历史重算和继承 | 修改缓存比较结论无效，NO_CHANGE后仍能继承更早无改善记录 |
| 篡改输入、Gold、逐轮及调用记录 | 独立裁判判为INVALID_EXPERIMENT |
| 普通日志隐私 | 离线节点只保留事件/状态/固定错误码/耗时，普通调用仍按默认记录 |
| 正常入口 | CLI默认只检查，live分支通过无网络传输替身验证实际参数接线 |

最终相关回归548项通过，包含新增路由测试31项、原A阶段Harness、证据评估/回答、日志、Agent Runtime、Adaptive Planner与文件搜索规划。依赖有原有Swig/Jieba弃用提示。本机JUnit位于`runtime/evidence-test/experiments/harness-v1/routing-acceptance-v1/pytest-results-complete.xml`，工程交付记录为同目录`verification.json`。

开发验收全部采用确定性Fake或传输/连接替身。已只读核验原v1/v2反馈和已完成真实实验，原实验独立重算仍为NO_GAIN。首次真实路由测试的进展见第10节。

需要注意，现有Legacy Planner部分规则会在模型之后重新选择分支。测试专门保留了“模型选择正文问答，但后端规则仍返回文件搜索”的案例。该预置提示策略可能得到NO_GAIN，执行器会保留这一结果。若后续需要调整后端规则，应新增受控策略及相应回归后再实验。

## 10. 首次真实测试记录

2026-11-11，用户已完成真实任务导出、路由Gold确认及一次live运行。本机数据集为`datasets/routing-real-v1/cases-v1.json`，包含R002与SEARCH01，题干及人工预期分别对应正文回答与文件查找。

本机实验`experiments/harness-v1/routing-live-v1/`在建议阶段终止，状态OPTIMIZER_FAILED，比较为NOT_RUN。实际请求1次，耗时约39.4秒。诊断记录`calls/0001.finished.json`显示HTTP 200、MODEL_RESPONSE_PARSE、JSONDecodeError、最终content为2字符、reasoning_content为6194字符、finish_reason为length、output_truncated为true。本次建议请求输出上限3000 Token，模型输出截断使JSON无法解析。Token用量未知。

控制组和候选组均为planned=2、recorded=0，表中0值来自尚未执行的计划槽位，尚无实际路由对照分数。下一步先处理建议器输出截断，再使用新的实验目录复测。本记录保留为真实建议失败样本。

## 11. 独立建议器额度与本次复测

已增加`--advisor-max-output-tokens`，支持3000、6000、8000，默认3000。它只作用于ADVISOR请求。PLANNER请求继续按3000输出，单次等待60秒、总请求授权及Gold沿用各自设置。额度记录在`experiment.json`的`authorization.advisor_max_output_tokens`，每次发送前及完成后的记录保存实际传给模型的`max_output_tokens`。旧记录缺少这一授权字段时按3000核验。

已用当前真实两题执行下列默认检查，返回CHECK_PASSED、model_calls=0、建议器8000、规划器3000、请求上界13。旧routing-live-v1独立重算仍为NOT_RUN/OPTIMIZER_FAILED，文件散列核对一致。可以直接复用现有冻结数据集。

先检查：

```bash
python scripts/run_evidence_test.py harness routing-cycle \
  --dataset datasets/routing-real-v1/cases-v1.json \
  --previous experiments/harness-v1/routing-live-v1 \
  --output experiments/harness-v1/routing-live-v2 \
  --advisor-max-output-tokens 8000
```

检查通过后，真实复测命令为：

```bash
python scripts/run_evidence_test.py harness routing-cycle \
  --dataset datasets/routing-real-v1/cases-v1.json \
  --previous experiments/harness-v1/routing-live-v1 \
  --output experiments/harness-v1/routing-live-v2 \
  --advisor-max-output-tokens 8000 \
  --live
```

查看新结果：

```bash
python scripts/run_evidence_test.py harness routing-compare \
  --experiment experiments/harness-v1/routing-live-v2
```

所有命令在项目根运行，使用现有Python测试环境。真实运行发起新的模型请求，输出目录需为新目录。只读检查成功不会创建routing-live-v2。

本次修改`routing/schemas.py`、`service.py`、`comparator.py`、Harness CLI及测试。增加8项回归，验证3000/6000/8000实际请求额度、规划额度独立、非法额度、授权及角色额度不符的拒绝，以及旧授权与历史代码指纹兼容。真实复测留给操作者执行。

本次相关回归148项全部通过，其中路由测试39项。JUnit及核对记录位于`runtime/evidence-test/experiments/harness-v1/routing-advisor-budget-acceptance-v1/`。实际默认检查通过，真实模型调用为0，routing-live-v2尚未创建。

## 12. routing-live-v2真实复测核对

用户随后已完成v2真实复测。建议器使用8000 Token，约17.3秒成功返回候选策略并通过校验。控制及候选的4次规划请求都使用3000 Token，全部在约47—52秒返回HTTP 200、finish_reason=length、output_truncated=true，最终content均为2字符，reasoning_content为10723—11478字符，均在JSON解析阶段失败。

现有规划节点随后进入确定性规则降级，两组R002均为FILE_LIST而错误，SEARCH01均为FILE_LIST而正确。当前比较器输出NO_GAIN，表示实际观察到的首轮路由结果相同；本轮有效规划模型输出为0，尚不足以验证候选提示策略本身的效果。COMPLETED描述外层复测循环完成，不能替代逐次模型调用状态。

全程5次模型请求、约213.4秒。已知Token为建议器输入733、输出926；4次失败规划用量未知。后续需为规划请求提供独立输出额度并对两组使用相同配置，同时显式记录降级状态，完善提示策略可评估性及历史去重门槛，避免把全量降级误认成已充分验证的无效策略。本节为真实结果诊断，相关程序调整尚待实施。

## 13. routing-live-v3真实复测核对

用户随后完成v3。5次模型调用全部成功，用量完整，总输入103900 Token、输出4538 Token，整轮约50.9秒。建议器8000、规划器3000。两组均1/2匹配，R002错误、SEARCH01正确，比较为NO_GAIN。

逐轮私有产物确认，控制组及候选组的R002模型意图都已是`ANSWER_QUESTION_FROM_FILES`，同时声明`evidence_answer`能力，最终服务端计划却均为`SEARCH_FILES`、首步`hybrid-search`。两组SEARCH01模型意图和最终计划均为SEARCH_FILES。此次没有规划模型调用失败，属于可分析的首轮路由实验结果。

根因位于`agent/planner.py`的`build_plan_from_user_intent`。其前置文件搜索分支调用`_has_file_search_intent`，后者会把原句中同时出现“列出”和“通知/文件”等词判为文件搜索。在空附件条件下，该分支提前返回`_file_search_plan`，覆盖了已经由模型识别的正文回答目标。该请求需要的是保留“发现来源后继续回答”的EVIDENCE_DISCOVERY路线，当前最终意图变成了仅找文件的SEARCH_FILES。

本轮候选只给规划模型追加固定提示，无法改变后端分支优先级。正常找文件、明确附件、来源范围和高风险操作均须有回归。当前已实现策略仍为第3节的提示策略。

2026-11-11用户明确后续目标为Harness自主生成并验证代码补丁。扩展按[四阶段开发计划](README.md#harness自主改进四阶段开发计划)推进：机器可读根因反馈、候选补丁生成、隔离执行、自动验收与有限迭代。不再只靠开发者预写新的后端策略供模型选择；补丁仍需受控范围、独立评分和实验隔离。四阶段最小版已完成工程验收，操作见[自主补丁测试步骤](harness-self-improvement-usage.md)。旧入口和历史结论保留；新路由复测额外记录安全意图轨迹，真实模型补丁效果尚未测试。
