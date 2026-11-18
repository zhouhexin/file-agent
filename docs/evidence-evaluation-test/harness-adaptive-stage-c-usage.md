# C阶段：怎样扩大题库并重复验证同一候选

日期：2026-11-12。开发依据：[C开发方案](harness-adaptive-stage-c-plan.md)。

最新准备状态：用户已确认14题标签和4道保留题独立性，已冻结`datasets/adaptive-validation-v2/cases-v1.json`（开发10/保留4），第6节三轮默认检查为CHECK_PASSED，model_calls和advisor_calls均为0。**无需重提三题、重复prepare或再次freeze**，可直接执行第6节live命令。真实C三轮对照尚未启动。三题采集记录见第10节。

## 1. 这次测试什么

固定使用`adaptive-policy-v2`已经生成的那份补充说明，比较原版和候选在更多题、默认三轮中的表现。C不调用建议器、不生成第二份候选、不自动上线；它仍只测首轮规划。

锁定的提示身份是`8ed96ea818ddec6f820b485a0a81f7df139366f3d7778c09e430dce28f491752`。重新运行`adaptive-cycle`会生成新候选，不能拿来冒充同一候选的重复验证。

所有命令在项目根目录执行，使用原Python环境和`runtime/evidence-test/.env`。准备、冻结、默认检查和比较都不调用模型。只有`adaptive-validate --live`调用真实规划模型；不用改env或重启服务。

## 2. 已经帮你准备了哪些真实输入

已只读盘点本人测试库，按“相同完整题干的最新已结束运行”固定选择，不按模型是否答对挑选，导出9题：R001、R009、R016、R017、R018、R027、R029、R034、R035。

真实导出在`runtime/evidence-test/datasets/c-source-inventory-v1/routing-export/inputs.json`。它们实际提交时均为零附件、零会话文件范围，原样保留。不会拿参考文件编号补成附件，也不把这9题当作已测过明确附件的案例。

加上原R002、SEARCH01，已创建11题草稿：

```text
runtime/evidence-test/datasets/adaptive-validation-v1/
  dataset-draft.json        # 来源、Catalog和真实输入，勿编辑
  annotations-draft.json    # 只需编辑的小标签文件
  人工核对说明.md
  准备情况.md
```

已在小标签文件中提供分组、业务文件族和路由建议，全部`confirmed=false`，不代替人工判定。建议开发10题、保留R009一题；R002/SEARCH01/R017/R027的换届文件族必须留在开发组。

目前**还不能冻结并进行真实扩大验证**：保留组至少需要2题，且需同时有正文任务和文件清单题；现有保留组缺“只列文件”。还需你核对题干和标签，并确认保留题没有用于当前候选的优化。原证据状态NORMAL/MISSING不能替代这里的路由标签。

## 3. 补齐真实任务

最低可新增一题（建议新会话、无附件，保留原句）：

| 新题号 | 在前端实际提交的问题 | 建议分组及文件族 |
|---|---|---|
| SEARCH02 | 查找2025年第9次党委会（扩大）会议通知，列出文件名即可。 | HOLDOUT，party-committee-meeting |

这样是12题、开发10＋保留2。保留组只有一个文件族，覆盖仍较窄。更建议再新增以下两题，让保留组达到4题、两个文件族：

| 新题号 | 问题 | 建议分组及文件族 |
|---|---|---|
| R013 | 以下仅核对文件记载的当时信息。只根据2019年印发的《西安理工大学党建活动经费管理暂行办法》这一版本，说明一天仅一次就餐的人均上限、大巴与中巴每天租车上限、场地费上限。请引用原文，不补充所选文件以外的信息。 | HOLDOUT，party-activity-funding |
| SEARCH03 | 查找2019年印发的《西安理工大学党建活动经费管理暂行办法》，列出文件名即可。 | HOLDOUT，party-activity-funding |

以下命令保留作采集步骤说明；三道题现已由助手按用户授权实际执行并导出，运行ID见第10节。再次采集其他新题时，按实际ID一一对应导出（替换下面三处占位符）：

```bash
python scripts/run_evidence_test.py harness routing-prepare \
  --user-id 2a69dd5c-8b9a-48af-9678-08c9e5d4d5e5 \
  --run-ids SEARCH02的运行ID R013的运行ID SEARCH03的运行ID \
  --case-ids SEARCH02 R013 SEARCH03 \
  --output datasets/c-additional-routing-v1
```

这是只读导出，不额外调用模型。若只补SEARCH02，run-ids和case-ids都只传一项。不要传预期文件ID或改写导出的附件。

为了覆盖更多类型，还可增加真实“选中附件后回答正文”和“无上下文需澄清”任务，分别保留实际范围与原句。澄清题应标OTHER并指定CLARIFY；没有采集时，报告不会声称覆盖。支持的是范围已明确的首轮规划，不执行任何文件Tool。

## 4. 合并成新草稿

补齐后保留旧11题草稿，另开新目录：

```bash
python scripts/run_evidence_test.py harness adaptive-validation-prepare \
  --candidate experiments/harness-v1/adaptive-policy-v2 \
  --inputs datasets/c-source-inventory-v1/routing-export/inputs.json \
           datasets/c-additional-routing-v1/inputs.json \
  --output datasets/adaptive-validation-v2
```

补齐三题时应显示14题。prepare自动加入原两题，所以inputs中不用再传routing-real-v1/inputs.json。题号或源运行重复会被拒绝。

此操作不会生成候选或确认Gold。请打开新目录`annotations-draft.json`，可参考旧目录的11条建议逐项填入，保持题号对应。每题填写：

| 字段 | 怎么填 |
|---|---|
| `split` | DEVELOPMENT或HOLDOUT；原两题只能DEVELOPMENT |
| `family_ids` | 业务文件族列表，同主题年度版本或不同任务引用同材料应保持同族；两个分组不可共用族或实际文件ID |
| `expected_route` | 要正文内容填CONTENT_ANSWER；只列文件填FILE_LIST；澄清或普通对话填OTHER |
| `expected_decision_type` | 正常规划通常TOOL_PLAN；应先澄清填CLARIFY；OTHER必须填写 |
| `reason` | 为什么应走这条路线，不能照抄模型预测当理由 |
| `confirmed` | 核对无误后改为true |
| `holdout_independence_confirmed` | 仅在确认保留题没有参与当前候选优化后改为true；开发题可保留false |

程序能检查显式同族、同文件、同原句和原B题泄漏，但无法证明你从未用某份材料调过策略，隐式同族和历史暴露仍须人工核对。保留集如果被用于后续调参，需要转为开发集并补新保留题。

不要修改`dataset-draft.json`的inputs、源路径或候选身份，也不要把它或标签文件上传到文件检索系统。

## 5. 冻结

```bash
python scripts/run_evidence_test.py harness adaptive-validation-freeze \
  --draft datasets/adaptive-validation-v2/dataset-draft.json \
  --annotations datasets/adaptive-validation-v2/annotations-draft.json \
  --output datasets/adaptive-validation-v2/cases-v1.json
```

全部确认、来源和分组检查通过后显示`VALIDATION_DATASET_FROZEN`。输出文件必须不存在。标签未确认、原题被改、开发/保留共用文件族或实际文档、保留缺正文/文件列表等情况会停止，不自动替你补标签。

## 6. 先检查，再三轮live

**2026-11-13：目前14题已冻结，第一次`adaptive-validation-v1`因模型额度耗尽得到INCONCLUSIVE。你已更换测试模型，请直接使用本文第11节的`adaptive-validation-v2`命令；下面保留首次运行说明，不要覆盖旧实验。**

以下按推荐14题：正常无回退两组各42次，共84次；最坏每题3次，共252次。没有建议器请求。若只有12题，正常72次、最坏216次，应把max-requests改为216。通用公式为`2×题数×轮数×3`。

```bash
python scripts/run_evidence_test.py harness adaptive-validate \
  --dataset datasets/adaptive-validation-v2/cases-v1.json \
  --output experiments/harness-v1/adaptive-validation-v1 \
  --repetitions 3 \
  --max-requests 252 \
  --max-active-seconds 7200 \
  --planner-max-output-tokens 3000
```

检查应显示CHECK_PASSED、model_calls和advisor_calls均为0、锁定提示身份、两组题数和请求上界。此时不建实验目录。

7200秒是总时限，不承诺覆盖每次都60秒超时的最坏路径。`time_budget_covers_request_wait_only=false`会如实显示；时间或请求预算耗尽就停止，未执行题保留分母，不自动补跑。

确认检查后，使用相同命令增加`--live`：

```bash
python scripts/run_evidence_test.py harness adaptive-validate \
  --dataset datasets/adaptive-validation-v2/cases-v1.json \
  --output experiments/harness-v1/adaptive-validation-v1 \
  --repetitions 3 \
  --max-requests 252 \
  --max-active-seconds 7200 \
  --planner-max-output-tokens 3000 \
  --live
```

它只运行已锁定候选，逐轮交替两组先后，不再次请求策略建议。失败、回退和中断都记账；Ctrl+C先清理子进程并记录已开始的请求，不会恢复旧运行。再次运行必须换新实验目录。

## 7. 比较和读结果

```bash
python scripts/run_evidence_test.py harness adaptive-validation-compare \
  --experiment experiments/harness-v1/adaptive-validation-v1
```

只读重算，不调用模型、不改实验。`arms.control/candidate`分别含overall、DEVELOPMENT和HOLDOUT。关注逐题每轮rows、cases中的正确次数和稳定性、route_metrics、decision_type_accuracy、contract_recognition_rate、Token和延迟。

| 结论 | 含义 |
|---|---|
| VALIDATED | 本批完整有效比较中开发表现改善，保留全对且无配对退步、新契约退步或安全违规 |
| NO_GAIN | 完整运行，但扩大集未达到该门槛；不能解释为没有错误 |
| REGRESSION | 有真实有效配对由对变错、新契约退步，或候选Adaptive计划出现违规 |
| INCONCLUSIVE | 没跑完、接口失败、回退或预检使同模式比较条件不足 |
| INVALID_EXPERIMENT | 源候选、冻结数据、调用、Profile、顺序或源码被改，不能采信结果 |

路线F1与决策类型正确率分开报告：OTHER路线正确但该澄清却直接回复，仍算该题错误。规则回退的越界观察会保留，但不冒充候选已被有效验证；未知Token单列，不当零费用。稳定地答错也可能显示路线稳定，必须结合正确次数看。

`candidate_retained=true`仍不是上线，actual retrieval和answer quality始终NOT_EXECUTED。C不向建议器传保留标签，也不自动用这些分数继续优化。

## 8. 新文件和接入位置

新增`improvement/validation_dataset.py`、`improvement/adaptive_validation.py`、`test_adaptive_validation.py`及C方案/本文。对原B增加已知v2执行器兼容；修正模型请求代理复用真实Planning的受控附件元数据投影，仍固定原ID，不采信前端文件名。CLI增加prepare、freeze、validate和compare，README/runbook同步。

接在“B候选已确定 → 扩大真实输入并人工分集 → 锁定候选重复对照 → 独立验收”这一段，不改变线上提示、Gold评分分类器、业务Tool或文件原件。

## 9. 工程验收与当前状态

346项相关回归通过，其中C专项29项；普通回归按设计跳过9项OS用例，其中C专项另行1项通过，六题两轮24次Fake规划请求、零建议器调用，候选副本不能读取env、Gold或其他实验。不能声称本次全部真实扩大测试已经完成。结果文件位于实验根下`planning-stage-c-related.xml`和`planning-stage-c-os-acceptance.xml`。

最初已准备11题，后补采三题、人工确认并冻结为14题。第一次C三轮流程已结束，但大量接口调用因旧模型额度耗尽失败，因此INCONCLUSIVE。后续更换模型触发默认配置检查，尚未创建第二轮实验；接下来按第11节复测，不需重新运行B建议器、补题或改标签。

## 10. 三题已实际补齐

每题单独新会话、无显式附件提交，复用前端的正常后端消息服务，以现有测试用户运行；没有修改配置、身份认证或活动提示，每题只提交一次。

| 题号 | 实际AgentRun ID | 状态 |
|---|---|---|
| SEARCH02 | 9cb2ac2a-e7a8-4713-86f5-6c0f19005357 | COMPLETED |
| R013 | cb562f30-20c7-4eab-a92a-3702ebfdf325 | NEEDS_REVIEW |
| SEARCH03 | 98df31e9-2391-42c5-b35d-99be4e652e10 | COMPLETED |

实际附件和会话范围均为0，原样导出至`datasets/c-additional-routing-v1/inputs.json`；提交账本在`datasets/c-three-submissions-v1/submissions.json`。R013已结束且有完整规划，允许作为真实输入采集：日志显示Shadow评估连接失败`SEMANTIC_NETWORK_ERROR`，正文模型生成完成，4条结论引用校验通过，答案最终为PARTIAL，不能声称整题完全成功；没有为凑成功重提。

已将三题与原11题合并为`datasets/adaptive-validation-v2/dataset-draft.json`。新目录`annotations-draft.json`保留原11条建议，并补充三题建议，开发10/保留4、文件族分开。所有人工确认标记仍为false，不自动代表标签正确或保留资格已确认。

后续用户已核对并确认小标签文件，第5节冻结和第一次三轮流程均已完成。原冻结集继续使用，不需要再次执行prepare或freeze覆盖已有文件。

## 11. 旧模型额度耗尽，使用新模型重新对照

旧实验`adaptive-validation-v1`原样保留。当前测试env已经更换模型，默认检查会报`VALIDATION_MODEL_PROFILE_CHANGED`；新增的`--allow-model-profile-change`表示你明确选择在新模型上复测同一候选。两组都使用新模型，其他输入、候选和评分标准相同。详见[补充方案](harness-stage-c-model-change-plan.md)。不用改服务配置或重启。

第一步，在项目根目录执行零调用检查：

```bash
python scripts/run_evidence_test.py harness adaptive-validate \
  --dataset datasets/adaptive-validation-v2/cases-v1.json \
  --output experiments/harness-v1/adaptive-validation-v2 \
  --repetitions 3 \
  --max-requests 252 \
  --max-active-seconds 7200 \
  --planner-max-output-tokens 3000 \
  --allow-model-profile-change
```

应显示`CHECK_PASSED`、`comparison_scope=CROSS_MODEL`、`model_changed=true`、`model_calls=0`。`origin_profile`是原B配置的哈希，`profile`是本轮配置，接口没变时`endpoint_changed=false`。检查不证明新模型额度充足或网络连通，因为它不会发请求。

第二步，正式复测只增加`--live`：

```bash
python scripts/run_evidence_test.py harness adaptive-validate \
  --dataset datasets/adaptive-validation-v2/cases-v1.json \
  --output experiments/harness-v1/adaptive-validation-v2 \
  --repetitions 3 \
  --max-requests 252 \
  --max-active-seconds 7200 \
  --planner-max-output-tokens 3000 \
  --allow-model-profile-change \
  --live
```

第三步，只读比较：

```bash
python scripts/run_evidence_test.py harness adaptive-validation-compare \
  --experiment experiments/harness-v1/adaptive-validation-v2
```

新实验头部记录来源/实际配置及明确允许变更的选择；每次调用开始和结束也记录同一`profile_ref`，比较器检查它们一致。结果中的收益只指本次新模型内部的原版/候选差别，不与旧模型分数相减。报告仍区分失败、回退、预检和实际Adaptive；R035目前走确定性预检，这次不修改其评分，因此接口恢复后仍可能INCONCLUSIVE。

`adaptive-validation-v2`若已经存在，就用新的输出目录，不能删除旧实验重跑。默认不开此选项仍保留同模型保护。旧C-v1只对已知执行器身份按原同模型规则只读重放，不补写新格式字段，不修改旧目录完整性。

本次改动验收：361项相关测试通过，另有1项真实OS隔离专项通过（均Fake）；实际测试env零调用检查通过，旧C-v1只读重放结论仍INCONCLUSIVE。详细记录见[补充方案第6节](harness-stage-c-model-change-plan.md#6-实际验收结果)。开发没有运行新一轮live。

## 12. 改用本机已经登录的Codex

当前推荐按第13节把选择写入测试env；本节的显式命令行写法仍然有效，优先于env。

若遇到整批`REQUEST_REJECTED`，先按第14节做单次探针；已修复Codex非致命启动提示被误当成工具事件的问题，不要直接覆盖或重跑失败实验。

如果原模型服务额度不足，可以仅对C测试选择本机Codex CLI。方案见[Codex登录接入](harness-codex-login-plan.md)。已确认本机`codex login status`显示ChatGPT登录，现有默认模型名为`gpt-6.1-sol`；程序要求命令中明确写出模型，不自动读取或切换默认值。不需要把登录token或原API key写入配置。

在项目根目录，先执行检查：

```bash
python scripts/run_evidence_test.py harness adaptive-validate \
  --dataset datasets/adaptive-validation-v2/cases-v1.json \
  --output experiments/harness-v1/adaptive-validation-codex-v1 \
  --repetitions 3 \
  --max-requests 252 \
  --max-active-seconds 7200 \
  --planner-max-output-tokens 3000 \
  --provider codex-login \
  --codex-model gpt-6.1-sol \
  --allow-model-profile-change
```

应显示`CHECK_PASSED`、`provider=codex-login`、`model_calls=0`，Profile中有CLI版本、包装身份和`CODEX_INVOCATIONS`。只检查本机安装及登录，不证明模型网络连通或账号额度足够；不建实验目录。

确认后，增加live进行真实测试：

```bash
python scripts/run_evidence_test.py harness adaptive-validate \
  --dataset datasets/adaptive-validation-v2/cases-v1.json \
  --output experiments/harness-v1/adaptive-validation-codex-v1 \
  --repetitions 3 \
  --max-requests 252 \
  --max-active-seconds 7200 \
  --planner-max-output-tokens 3000 \
  --provider codex-login \
  --codex-model gpt-6.1-sol \
  --allow-model-profile-change \
  --live
```

完成后只读比较：

```bash
python scripts/run_evidence_test.py harness adaptive-validation-compare \
  --experiment experiments/harness-v1/adaptive-validation-codex-v1
```

大白话说明几个差别：

- 这次是“程序把规划问题交给已登录的Codex，再拿回JSON”，模型仍在云端运行。Codex不会接管文件处理，也不会自动替你改程序。
- 只有这次C测试走Codex，前端、正常聊天、后台处理和A/B建议器没有换模型；可用本节命令临时指定，也可按第13节配置测试env，服务不用重启。两组题目和候选保持固定。
- `max-requests=252`在这里表示最多252次Codex调用；CLI内部HTTP次数及重试不能完全观察，Profile明确标记，不与原API请求数当成同一种计量。宿主不重试，不承诺CLI内部零重试。每次仍最多60秒，总时长仍最多7200秒。
- CLI没有原API相同的输出Token硬限制。`planner-max-output-tokens=3000`保留为上游请求值，Profile明确记录`output_token_limit_enforced=false`，不能把它理解为最多消耗3000个Token。实际Token有返回就记录，没有就保留未知。
- CLI使用临时空目录并忽略用户配置，不载入项目文档；禁用工具能力，OS另外阻止读取/写入项目和启动其他程序。候选仍在原隔离环境里，不获得本机登录信息。
- Codex账号仍有自身额度。额度问题、超时、网络或格式异常都会保留失败记录；不自动改用原API，也不自动补跑或上线。R035预检及其他原评分边界仍保持。

尚未登录时由你在终端执行`codex login`完成浏览器认证；程序只检查登录状态，不代为提取认证。未配置env且不指定`--provider`时使用原API；已配置env则读取env。旧实验保持原样，新目录若已存在须换新的输出名称。

工程验收：378项相关测试、另行2项真实OS隔离测试通过（均Fake）。本机实际CLI参数、固定配置加载和登录检查，以及真实14题零调用检查均通过；未请求真实模型或创建这轮实验。具体记录见[接入方案第7节](harness-codex-login-plan.md#7-验收结果)。

## 13. 在env中切换Codex和原API

现在已把本机`runtime/evidence-test/.env`的C测试选择设为：

```dotenv
EVIDENCE_HARNESS_PROVIDER=codex-login
EVIDENCE_HARNESS_CODEX_MODEL=gpt-6.1-sol
EVIDENCE_HARNESS_ALLOW_MODEL_PROFILE_CHANGE=true
```

这三项只影响C阶段Harness。原`LLM_API_KEY`、`LLM_BASE_URL`、`LLM_CHAT_MODEL`仍保留；Codex登录token不写入env。允许模型变更表示明确接受本次与候选来源模型不同，实际Profile仍会记录和核对。

先执行检查，不用再带三个模型选择参数：

```bash
python scripts/run_evidence_test.py harness adaptive-validate \
  --dataset datasets/adaptive-validation-v2/cases-v1.json \
  --output experiments/harness-v1/adaptive-validation-codex-env-v1 \
  --repetitions 3 \
  --max-requests 252 \
  --max-active-seconds 7200 \
  --planner-max-output-tokens 3000
```

应显示`CHECK_PASSED`、`provider=codex-login`、`configuration_sources`中三项为`ENV`、`model_calls=0`。检查不请求真实模型，也不建实验目录。

正式复测增加live：

```bash
python scripts/run_evidence_test.py harness adaptive-validate \
  --dataset datasets/adaptive-validation-v2/cases-v1.json \
  --output experiments/harness-v1/adaptive-validation-codex-env-v1 \
  --repetitions 3 \
  --max-requests 252 \
  --max-active-seconds 7200 \
  --planner-max-output-tokens 3000 \
  --live
```

完成后比较：

```bash
python scripts/run_evidence_test.py harness adaptive-validation-compare \
  --experiment experiments/harness-v1/adaptive-validation-codex-env-v1
```

**切回原API：**把env第一项改成`EVIDENCE_HARNESS_PROVIDER=openai-compatible`。保留原来三项`LLM_*`配置，Codex模型一行也可保留，API模式会忽略它。再次实验应使用新输出目录，例如`adaptive-validation-api-env-v1`；每条命令启动时重新读取env，不需重启前端或服务。

**临时覆盖：**在命令后增加`--provider openai-compatible`即可临时用原API；增加`--codex-model <模型>`临时覆盖Codex模型；用`--no-allow-model-profile-change`明确关闭env中的允许变更。顺序是命令行优先、env其次、代码默认最后。配置拼错会拒绝，Codex失败不会自动转回API。显式指定API又传`--codex-model`属于矛盾参数，会被拒绝。

未设置这些新键时，默认仍是原API且不允许Profile变更。原模型参数、Token限制能力、Codex账号额度和R035预检等边界见第11—12节。方案与验收记录见[env配置方案](harness-provider-env-plan.md)。

## 14. Codex启动事件修复后，先单次探针

已完成的`adaptive-validation-codex-v1`流程虽然COMPLETED，但156次CLI调用全部REQUEST_REJECTED，两组都没有有效Adaptive结果。核对代码及OS断网探针发现，CLI会在开始推理前发送`item.type=error`的非致命启动提示，原适配器把它误当成工具调用。

现已分开处理：合法非致命提示只记脱敏摘要，真实工具事件仍拒绝；错误通知继续等真实终态，失败事件、非零退出或缺少完成仍失败。修复不改变Gold、原候选或工具权限，旧实验保持原样。最新连接修复见第15节，背景见[启动事件修复](harness-codex-startup-event-fix.md)。

不要先跑整批。先检查当前env和登录（零模型调用）：

```bash
python scripts/run_evidence_test.py harness codex-probe
```

再明确执行一次真实调用：

```bash
python scripts/run_evidence_test.py harness codex-probe --live
```

固定假问题只检查JSON协议和用量，不读真实文件/Gold，不创建实验目录，不自动重试。`requests_used=1`表示一次CLI调用，内部HTTP仍不能精确计数。成功应显示`stage=COMPLETED`；失败时提供这条命令输出的JSON即可，先不要启动三轮。

`codex_event_diagnostic`包括阶段、最后事件种类、条目种类、启动提示数、固定原因线索、是否收到完成及清理前退出码。`UNSTABLE_FEATURE_NOTICE`是CLI功能提示，`MODEL_CATALOG_ENTRY_MISSING`是本地目录线索，不能据此证明云端不支持模型；`TOOL_EVENT_REJECTED`才是实际受禁工具事件。错误原文、推理、路径和认证不入记录。

只有单次探针成功后，再用第13节的三轮命令并把输出改为`experiments/harness-v1/adaptive-validation-codex-v2`，随后比较同一新目录。原R035预检限制仍保持，探针成功本身不代表C阶段通过或模型策略有效。

## 15. WebSocket被重置时改用HTTPS

本机doctor已确认：HTTP端点可达，WebSocket握手被重置。现已在测试env新增：

```dotenv
EVIDENCE_HARNESS_CODEX_TRANSPORT=https
```

本次仅切换连接协议，保留当前模型、ChatGPT登录和原API参数。HTTPS模式使用官方支持的独立Provider定义（内置openai不可覆盖），设置`requires_openai_auth=true`和`supports_websockets=false`；由Codex自己按登录选择ChatGPT原生端点，不复制token，不修改用户全局Codex配置。

先执行零调用检查：

```bash
python scripts/run_evidence_test.py harness codex-probe
```

确认Profile含`transport=HTTPS_SSE`、`configuration_sources.codex_transport=ENV`。再执行一次真实调用：

```bash
python scripts/run_evidence_test.py harness codex-probe --live
```

HTTPS模式显式配置请求/流重试为0，但CLI内部HTTP次数仍不可完全计数；总等待仍为60秒。适配器不会再因第一个error通知就退出，而是等待真正的turn.failed或成功完成。没有完成、非法JSON、非零退出、超时和真实工具事件仍失败，`codex_event_diagnostic.error_event_count`记录错误通知数，不代表模型最终失败次数。

原API忽略这个Codex专用env项，功能保留。未设置时仍为原auto模式；可以用`--codex-transport auto`临时恢复CLI自动选择。HTTP可达检查不等于真实模型推理成功，先确认一次探针COMPLETED，再考虑新目录三轮测试。实现和验收见[HTTPS修复方案](harness-codex-https-fix.md)。
