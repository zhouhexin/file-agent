# 新增12题：实际检索证据采集步骤

日期：2026-11-10。适用已有14份原文件已解析并索引就绪的测试环境。本文提供操作命令，没有自动生成采集计划、重启服务或发起真实模型请求。

采集的意思是：在前端正常提问时，系统把**这一次实际送入评估器的问题、检索证据和同次核验版本事实**保存下来。不能把参考答案、最终回复中的引用列表或手工拼接正文当成该输入。

流程为：新采集计划 → API/analysis加载计划 → 前端限定文件并提问 → 检查采集快照 → 人工核对 → 确认Gold和生成数据集。原`frozen-real-v1`保留，本次只准备新增题。

## 1. 生成新采集计划

在项目根目录执行以下命令：

```bash
cd /Users/zhouhexin/PycharmProjects/file-agent
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py frozen prepare \
  --root datasets/frozen-real-expanded-v1 \
  --workbook datasets/real-workdocs-20261031/人工判定表.xlsx \
  --sources datasets/real-workdocs-20261031/文件清单.json \
  --case-ids R002 R005 R009 R013 R017 R018 R024 R025 R027 R029 R035 R040
```

该命令只读查询测试数据库，按文件内容SHA256唯一匹配活动、索引完成的工作副本，不调用LLM。成功显示`state: AWAITING_REAL_CAPTURE`，生成：

```text
runtime/evidence-test/datasets/frozen-real-expanded-v1/
  capture-plan.json
  annotations/gold-draft.json
```

此时只是有计划和人工标签草稿，还没有采集证据，也不代表新包Gold已经确认。新增12题的题干已与现有人工表核对一致；本次读取到测试env的`EVIDENCE_ANSWER_CACHE_ENABLED=false`，无需修改env。

若提示`CAPTURE_PLAN_ALREADY_EXISTS`，查看已有计划是否就是这一版，不反复prepare，也不覆盖旧文件。若提示`SOURCE_REQUIRES_ONE_ACTIVE_INDEXED_COPY:Fxx`，先核对对应文件是否唯一且就绪，不能猜测同名对象。

## 2. 让API和analysis加载同一份计划

在原来启动API和analysis的终端分别按`Ctrl+C`停止对应实例，确认退出后重启。不要在旧实例仍占用端口时再启动一套；不要停止或重建数据库。

终端一：

```bash
cd /Users/zhouhexin/PycharmProjects/file-agent
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py api \
  --capture-plan runtime/evidence-test/datasets/frozen-real-expanded-v1/capture-plan.json
```

终端二：

```bash
cd /Users/zhouhexin/PycharmProjects/file-agent
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py analysis \
  --capture-plan runtime/evidence-test/datasets/frozen-real-expanded-v1/capture-plan.json
```

原有lifecycle及前端可继续运行，无需为采集重启。`--capture-plan`仅作用于此次新启动进程，不改env。

注意路径规则：`frozen prepare`的`datasets/...`相对独立测试目录；API/analysis的`--capture-plan runtime/...`相对项目根目录。以上命令已分别写好，不要互换前缀。

## 3. 限定文件范围后逐题提问

### 2026-11-11 复测排查

新增目录的采集计划已生成。检查时 API 和一套 analysis 已加载新计划，另一套 analysis 仍加载旧 `frozen-real-v1/capture-plan.json`。在对应的旧 analysis 终端按 `Ctrl+C`，保留使用 `frozen-real-expanded-v1/capture-plan.json` 的进程。已有计划可以继续使用。

会话数据库中，本轮匹配到的6道新增开发题都以零附件提交。R002、R017、R027、R029进入列文件流程。R018进入证据回答时读取两份文件，计划只允许F04，因此记录 `CAPTURE_CASE_SCOPE_NOT_MATCHED`。R035走表格发现流程，未构造证据包。此次查询未找到与R005题干完全匹配的新运行。旧题R016的另一次请求也留下了跳过采集的日志。

本轮采集目录没有快照。此前的前端说明缺少一步验证，文件列表出现目标文件后，还需通过选择卡确认它进入本轮范围。普通聊天的“选择文件”是本机文件上传入口。直接粘贴题干时，服务端可能收到空范围。

### 先用明确范围复测R018

已从现有采集计划生成7份开发题的请求正文，位于 `runtime/evidence-test/datasets/frozen-real-expanded-v1/scoped-requests/`。每份均保留原题干与计划中的 `attachment_document_ids`。先用R018验证，看到对应的采集成功后再继续。这一接口仍通过聊天任务的路由和权限检查，R035也需要单独确认实际是否进入证据回答。

打开本机测试 API 的 `http://127.0.0.1:8000/docs`，使用现有测试账号在 `POST /api/auth/login` 登录。复制本机返回的 `access_token`，用于 `POST /api/conversations/{conversation_id}/evidence-answer` 的 `Authorization` 请求头，值为 `Bearer ` 加 token。

路径参数 `conversation_id` 使用当前测试会话 ID。本次数据库核对的会话为 `chat-2a69dd5c8b9a48af967808c9e5d4d5e`。请求正文粘贴 [R018.json](../../runtime/evidence-test/datasets/frozen-real-expanded-v1/scoped-requests/R018.json) 的完整内容，执行一次。接口使用真实问答与Shadow模型。

提交后按第4节查看该次运行。只有 `case_id=R018` 和 `status=COMPLETED` 同时出现，并且新目录有对应的 `package.json`，才继续提交其他请求。若仍走文件列表或其他流程，保留运行ID以定位路由。

### 使用聊天页的选择卡

打开本机[问题扩充清单](../../runtime/evidence-test/datasets/expansion-candidates-v1/问题扩充清单.md)，按题号查找完整题干和来源。按以下顺序操作：

1. 通过文件范围选择卡确认该题列出的文件。双文件题必须同时选中两份，移除上题多余选择。若当前聊天页没有范围选择卡，可使用上面的 API 请求正文明确传入文件 ID。
2. 只复制该题完整问题，保留“以下仅核对文件记载的当时信息”等前缀和原标点；不要加“R002：”、自行简写或粘贴答案参考。
3. 提交一次，等待这次任务结束，记录题号和运行ID。采集不是要求再上传文件。
4. 查看第4节的采集日志，确认对应题号已落盘，再做下一题。若评估超时，先检查是否已有快照，避免为了采集而重复发送付费问题。

先采新增开发集7题：

| 题号 | 前端限定文件 | 核对重点 |
|---|---|---|
| R002 | F02，2024年教工党支部换届通知 | 范围和两项期限 |
| R005 | F05，第1次理论学习中心组通知 | 时间、地点、人员和请假要求 |
| R017 | F02 | 候选差额比例、最终当选书记姓名缺失 |
| R018 | F04，系（所、中心）主任换届通知 | 实验中心人数占位符，不能猜 |
| R027 | **F02＋F03**，2024年和2025年两份换届通知 | 年份、对象及期限分开 |
| R029 | **F05＋F06**，第1次和第4次学习通知 | 不同场次不能只凭地点不同报冲突 |
| R035 | F25，党建工作台账XLSX | 目标85%以上和实际88.37%的口径 |

预留验收5题对应：R009→F11、R013→F18、R024→F19、R025→F21、R040→F24。计划已包含它们，但可以先不提问，等候选配置固定后再采集。若提前采集，仍须独立保存，不把预留逐题结果反复交给优化器。详细文件名与路径见[上传清单](../../runtime/evidence-test/datasets/expansion-candidates-v1/上传文件清单.md)。

这里只描述在普通前端逐题提问；真实问答和Shadow评估会调用已配置的模型并计费。采集本身不另起一轮Harness优化，也没有要求三轮重复采集。

## 4. 怎么确认真的采集到了

项目根目录执行：

```bash
rg --no-ignore -n -F 'evidence_evaluation.capture' runtime/evidence-test/logs/
```

需要看同一次运行的`agent_run_id`，重点看：

- `status=COMPLETED`，同时有正确`case_id`及`input_sha256`：该输入快照已保存。
- `SKIPPED`及`CAPTURE_CASE_SCOPE_NOT_MATCHED`：完整题干或文档ID范围与计划不匹配，不代表已经采集。先核对题干、文件及是否真正限定范围。
- `FAILED`：查看`capture_stage`、固定错误码和脱敏异常类型；检查计划是否加载、目标路径及文件权限。不要仅根据最终回答推定采集成功。
- 没有新采集事件：先确认API和analysis均以本次计划重启，再看本次是否实际进入证据评估；路由到找文件、未找到证据或缓存直接返回等情况不能冒充成功输入。

成功后，例：

```text
runtime/evidence-test/datasets/frozen-real-expanded-v1/
  captures/R002/<64位input_sha256>/
    package.json         # 实际问题与evidence_items，含证据正文和定位
    versions.json        # 同次核验的版本事实
    context.json         # 原问题及CURRENT/HISTORICAL/UNKNOWN
    manifest.json        # 输入散列、来源、采集配置等
    observations/        # 后续保存的评估结果、调用状态与安全诊断
```

输入快照在语义评估前保存，所以**评估超时不等于证据没采到**。即使评估失败，也应保留真实失败记录；采集COMPLETED同样不证明语义判断正确，仍需核对包内容。

同一输入可新增observation；不同证据/版本事实会产生不同散列。若同题出现多个目录，记录打算使用的具体散列，不能自动挑模型答对的那一个。

## 5. 采完后核对什么

先查看实际`package.json`中的`evidence_items`，逐项对照问题：支持事实是否真的在包内、原文定位是否可靠、是否混入其他文件。不要只看前端最终答案，也不要因为它漏答就直接标MISSING。

新增`annotations/gold-draft.json`只是从原文件人工表带来的草稿，需要按具体包复核：完整支持才标NORMAL；所问必需事实在包内缺失才标MISSING。原文件有、检索包没有时要保留检索漏召回记录。不能根据模型预测结果反向改Gold。

确认完成后，才执行`frozen confirm`绑定该题散列，再用`frozen build`生成可运行数据集；对应命令结构见[原固定输入文档第4—5节](frozen-real-input-usage.md#4-查看实际快照并确认金标)，将root替换为本次`datasets/frozen-real-expanded-v1`。不要对尚未人工核对的12题批量确认。

原3题继续保留在`frozen-real-v1`，不要求重新上传或重复提问。新旧输入的合并、开发/预留分集、基线准备和Harness可变题数适配在采集核对后另行处理；当前不能把新增数据直接传给仍限制3题/19请求的`harness cycle`。
