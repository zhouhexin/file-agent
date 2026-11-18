# 真实输入冻结：已实现功能与操作步骤

2026-11-05。对应[实施方案](frozen-real-input-plan.md)。本次交付代码和deterministic fake测试，没有自动启动服务、查询真实库、修改实际env或调用付费模型。历史日志无法补成准确的旧输入，真实快照仍需按下列步骤重新采集。

## 1. 新能力加在哪一步

```text
原有上传 → 解析/索引 → 检索 → EvidencePackage
                              ↓
                 _evaluate_shadow（原有两条接入路径）
                 授权版本读取一次 → 保存固定输入（可选）
                              ↓
                 原有评估 → 保存完整结果/逐调用记录（可选）
                              ↓
                      原有回答照常执行

单独复测：已确认的固定包 → evaluate_evidence.py → 逐轮报告
```

采集默认关闭。仅允许`file_agent_evidence_test`前缀的数据库、项目`runtime/evidence-test/datasets/`内的采集计划。按完整题干和文档范围匹配，权限仍由原`AuthorizedVersionReader`核验；不能由模型决定采集路径。采集失败不会阻断评估或答案。

版本只读取一次，实际Shadow评估与快照使用同一份核验结果。`VERIFIED`代表原读取器已完成访问范围、对象绑定、活动状态及当前版本索引检查；不能离线手改该值冒充权限验收。无法核验照实保存。

## 2. 准备三题的采集计划

所有命令从项目根目录执行。`frozen`与`evaluate`子命令在独立测试目录中运行，所以它们的`datasets/...`参数相对于`runtime/evidence-test/`。

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py frozen prepare \
  --root datasets/frozen-real-v1 \
  --workbook datasets/real-workdocs-20261031/人工判定表.xlsx \
  --sources datasets/real-workdocs-20261031/文件清单.json \
  --case-ids R001 R016 R034
```

这一动作只读连接独立测试库，按源文件SHA256唯一匹配已入库、活动且当前索引完成的工作副本。不存在、多个候选或未就绪会停止，不猜同名文件，也不自动上传/建索引。

成功后生成：

- `runtime/evidence-test/datasets/frozen-real-v1/capture-plan.json`：三道完整题干与真实文档ID集合。
- `runtime/evidence-test/datasets/frozen-real-v1/annotations/gold-draft.json`：从当前Excel读取的金标草稿，尚未绑定实际输入。

已存在的计划不会覆盖。要重新准备一版，使用新目录，例如`frozen-real-v2`。准备计划不表示已经采集到快照。

这批原始真实材料使用默认`--material-source real`。如果以后使用脱敏改写/人为构造矛盾的文件，另建采集计划并传`--material-source derived`，来源标签会进入快照和评测报告，不把派生材料冒充原始业务冲突。

## 3. 让测试API和问答worker显式启用采集

先自行停止旧测试API/analysis实例，再分别执行以下命令；lifecycle仍按原方式启动：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py api \
  --capture-plan runtime/evidence-test/datasets/frozen-real-v1/capture-plan.json
```

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py analysis \
  --capture-plan runtime/evidence-test/datasets/frozen-real-v1/capture-plan.json
```

`--capture-plan`在启动器解析，路径相对于项目根；其他冻结子命令的路径规则见第2节。该参数只作用于新启动进程，不修改实际env。前端继续使用原测试环境。

从当前Excel“问题”页完整复制R001/R016/R034题干，分别选中限定文件再提交一次。不上传参考答案。现有采集计划要求题干和文件范围匹配，不删除“当时信息”前缀，不手改文件名措辞来凑匹配。缓存命中不会重新评估或采集，独立测试配置应继续关闭答案缓存。

若已人工调整采集计划题干，提交时以该计划的完整`question`为准。2026-11-05的R034“明确列出题名和文号仍0命中”已加入[范围修复](../2026-11-05-explicit-multi-source-scope-fix.md)：需重启API和analysis加载代码；当前计划已与该次提问一致，无需继续扩充。该修复不保证后续模型调用不会超时，仍应分别核查范围、采集和评估状态。

查看采集状态：

```bash
rg --no-ignore -n 'evidence_evaluation.capture' runtime/evidence-test/logs/
```

- `COMPLETED`：含case_id及input_sha256，可以定位快照。
- `SKIPPED / CAPTURE_CASE_SCOPE_NOT_MATCHED`：题干或文件范围不匹配，未导出正文。检查计划和实际题干/选择范围。
- `FAILED`：检查安全错误码及`capture_stage`（计划读取/输入写入/结果写入）、脱敏异常类型；原评估和回答继续。日志不回显异常正文、本地路径或密钥。

## 4. 查看实际快照并确认金标

生成结构：

```text
frozen-real-v1/
  capture-plan.json
  captures/R001/<input_sha256>/
    package.json        # 实际完整问题、原文证据、定位、限制、scope及旧指纹
    versions.json       # 同次核验的版本事实
    context.json        # 原始问题与实际CURRENT/HISTORICAL/UNKNOWN
    manifest.json       # 完整输入散列、时间、采集来源、配置和代码/依赖散列
    observations/
      <唯一编号>.json   # 完整评估结果、requirements/缺失项/理由与逐调用响应或安全错误
  annotations/gold-draft.json
```

相同输入重复采集复用同一输入目录，但新增observation，不覆盖旧结果。新问题、新证据或新版本事实产生新输入散列。JSON文件权限0600，目录仅作为本机私有测试材料使用，不进入普通日志或Git。

核对package中的真实证据，而不只是最终回答引用。根据实际包修改`annotations/gold-draft.json`的对应条目：

- 正常：`group="NORMAL"`，`expected_issues=[]`。
- 缺项：`group="MISSING"`，`expected_issues=[{"type":"MISSING","evidence_ids":[]}]`，理由写清缺什么。
- 冲突：必须填写两个真实evidence_id；旧版本正例也必须填写真实证据ID。
- 多标签：`expected_issues`列全部标签，group选其中一个主组用于分类展示。

不要因为模型上一轮这么判断就改金标。R034若实际包缺了培育时长，不能沿用原文件层面的正常标签；应复核实际包并记录检索差异。R034“是否都提供申请预算”可回答未提供，不自动等于缺项。

确认R001的命令如下。将中文占位文字替换为采集日志中R001实际64位散列；它被引号包住，不是shell重定向：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py frozen confirm \
  --root datasets/frozen-real-v1 \
  --gold datasets/frozen-real-v1/annotations/gold-draft.json \
  --case-id R001 --input-sha256 "替换为R001的64位input_sha256" \
  --output datasets/frozen-real-v1/annotations/R001.gold-v1.json
```

R016、R034分别替换case-id、各自散列及输出文件名执行。`confirm`表示操作者已复核该具体输入；不会伪填独立复核者。已确认文件不覆盖，改标注时另存gold-v2。

## 5. 生成可运行的数据集

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py frozen build \
  --root datasets/frozen-real-v1 \
  --gold datasets/frozen-real-v1/annotations/R001.gold-v1.json \
         datasets/frozen-real-v1/annotations/R016.gold-v1.json \
         datasets/frozen-real-v1/annotations/R034.gold-v1.json \
  --output datasets/frozen-real-v1/cases-v1.json
```

该命令验证输入散列、版本键完整覆盖、金标绑定与引用白名单，生成带数据集/金标散列的JSON。它不调用模型、不查询数据库，也不把gold-draft当已确认数据。导出的`已标注样本-v1.json`或Excel不能直接替代该数据集。

## 6. 先规则，再三轮真实模型评测

规则检查不调用模型：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py evaluate \
  --dataset datasets/frozen-real-v1/cases-v1.json --mode rules --repetitions 1 \
  --output datasets/frozen-real-v1/runs/rules-01.json
```

真实评测会向独立env配置的模型发送已冻结的证据并计费，只对评估器调用，不走Planner、检索或最终答案生成：

已有3000 Token基线后，验证输出截断请使用[输出额度对照步骤](output-budget-comparison-steps.md)及独立入口，不改原env或旧报告。

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py evaluate \
  --dataset datasets/frozen-real-v1/cases-v1.json --mode live --repetitions 3 \
  --output datasets/frozen-real-v1/runs/live-v1-01.json
```

报告为每题保存三轮的完整结果和每次调用记录。重新执行使用新的输出文件名；不覆盖历史报告。模型配置变化会体现为runtime_config_sha256变化，并报告与采集配置是否一致。低温度不等于模型必然三轮相同。

## 7. 固定返回的无网络回放

`replay`要求有真实采集的逐调用记录，或明确标记的工程fake。为构建带记录的数据集，在第5节`build`命令额外传入`--observations`，逐题指定一个已采集的`captures/<题号>/<散列>/observations/<编号>.json`，另存`cases-replay-v1.json`。同一输入不能同时选多个observation。

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py evaluate \
  --dataset datasets/frozen-real-v1/cases-replay-v1.json --mode replay --repetitions 3 \
  --output datasets/frozen-real-v1/runs/replay-v1-01.json
```

每批以请求散列核对对应返回，调用序号或载荷不符会拒绝回放，不把一条固定回答重复套给不同批次。回放Token记为本轮零网络消耗，历史供应商用量在observation中保留，不拿replay高分冒充真实模型准确率。

失败记录回放的是已保存的安全错误类别和阶段，不能声称恢复了未记录的原始HTTP响应字节。未给observations的数据集仍可rules/live；不能直接replay。

## 8. 看哪些指标

- 每轮分别统计MISSING/CONFLICT/OUTDATED及派生NORMAL的P/R/F1；没有阳性样本时Recall为null（N/A）。
- `per_round`给逐轮指标与执行状态；`cases[].rounds[]`保存每题每轮完整结果、解释、限制、错误及调用记录。
- `matches_annotation`只有标签/引用匹配且整体COMPLETED时为true。`issue_signature_matches`只表示问题标签与引用集合相同，失败空结果不能冒充正常通过。
- `exact_issue_mismatches`保留旧版“问题/引用集合”口径；严格验收看`exact_complete_matches`、各轮执行状态与逐例结果。
- `repeated_stability_rate`可能包含重复失败；同时查看`completed_repeated_stability_rate`，三轮都失败不代表稳定识别。
- 用量不完整时`token_usage_complete=false`，不要把字段中的0解释为真实零消耗。
- 本地数据集散列用于完整性检查，不代替真实权限集成测试；静态快照不去查询后来发生变化的实时版本。

## 9. 变更文件与验证范围

新增：`frozen_inputs.py`（快照/完整性/录制回放）、`frozen_evidence.py`（prepare/confirm/build）、`test_frozen_evidence_inputs.py`、本操作文档。

修改：`core/config.py`、`evidence_answer/service.py`、`scripts/evaluate_evidence.py`、项目启动器`run_evidence_test.py`、环境模板、启动器测试及方案/测试文档入口。

不需要数据库迁移，不涉及前端改动，也不增加评估驱动的补检、拒答或文件修改。生产默认off和采集默认关闭保持不变。测试使用隔离文件、SQLite及deterministic fake；没有把测试桩冒充真实三题已完成采集。

验证结果：相关回归328项通过；命令行对既有64条合成fixture执行两轮回放，保存128条逐轮记录且无失败，并验证同名报告拒绝覆盖。上述均为工程验证，不代表真实模型准确率。Jieba依赖出现一条既有弃用提示，不影响测试通过。
