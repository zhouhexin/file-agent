# 180 个文件分类标注与评估实施方案

## 1. 目标

本方案用于把 `E:\test` 中现有 180 个真实文件建设成第一版可重复使用的分类基准集，回答以下问题：

1. 每个有效 taxonomy 分类是否至少有真实文件能够进入候选集合。
2. 当前规则分类的 Top-1 准确率和 Top-3 召回率是多少。
3. 当前大量 `NEEDS_REVIEW` 是候选召回不足、主分类排序错误、证据不足，还是自动落位阈值过严造成的。
4. 在不降低自动落位精度的前提下，全局置信度阈值和 Top-1/Top-2 间隔应如何初步调整。
5. 后续应优先完善 taxonomy 信号、全文特征、重排算法、LLM 受限判定，还是校准策略。

本轮只建设评测方法和标准答案，不直接用标注结果移动文件，也不直接修改生产分类阈值。

## 2. 当前数据与适用边界

`E:\test` 当前共有 180 个文件：

| 扩展名 | 数量 |
| --- | ---: |
| DOCX | 66 |
| PDF | 50 |
| DOC | 34 |
| XLSX | 17 |
| XLS | 12 |
| TXT | 1 |
| 合计 | 180 |

当前运行时 taxonomy 约有 58 个非根分类，180 个文件平均每类约 3 个样本。因此本批数据可以用于：

- 全分类链路连通性检查。
- 当前实现的基线评估。
- 全局阈值初步校准。
- 发现主要混淆分类和 taxonomy 信号缺口。
- 建立以后不可随意修改的回归集。

本批数据不能单独用于：

- 证明每个分类都达到 99% 精度。
- 训练可靠的逐分类概率校准器。
- 仅凭一次评估直接全量开放自动落位。

原因是单分类样本太少。即使 180 个样本全部自动分类正确，也不足以严谨证明真实错误率低于 1%；如果只有其中一部分被自动落位，统计不确定性会更大。第一版评估报告必须同时展示样本数和置信区间，不能只展示一个准确率百分比。

## 3. 总体流程

```text
冻结 taxonomy 和分类器版本
-> 建立 180 文件清单并计算 SHA-256
-> 精确去重和文件族分组
-> taxonomy 覆盖预检
-> 双人独立标注
-> 冲突仲裁并冻结 Gold Labels
-> 在隔离测试环境运行当前分类链路
-> 导出 Top-5 候选、证据、分数和拒绝原因
-> 计算召回、准确率、自动落位精度和覆盖率
-> 在开发集扫描阈值
-> 在独立保留集复核
-> 输出改进建议，继续保持 Shadow 或受控灰度
```

## 4. 评测资产设计

### 4.1 原始文件

- `E:\test` 作为只读原始语料目录。
- 不在文件路径或文件名中写入正确分类，防止分类器通过路径获得答案。
- 不覆盖、改名或修改原始文件。
- 测试运行产生的工作副本、解析结果和报告进入隔离测试存储，不写回 `E:\test`。

### 4.2 样本清单

实施阶段建立版本化 manifest。建议字段如下：

| 字段 | 含义 |
| --- | --- |
| `sample_id` | 与文件名无关的稳定测试 ID，例如 `CLS-0001` |
| `relative_path` | 相对 `E:\test` 的路径，仅供受控测试读取 |
| `filename` | 原始文件名 |
| `extension` | 文件扩展名 |
| `size_bytes` | 文件大小 |
| `sha256` | 内容哈希，用于精确去重和结果关联 |
| `family_id` | 同模板、同文档不同年份或近似副本的文件族 ID |
| `parse_expected` | 是否预期能够正常解析 |
| `taxonomy_key` | 标注时冻结的运行时 taxonomy key |
| `taxonomy_version` | 标注时冻结的运行时 taxonomy version |
| `dataset_split` | `CALIBRATION` 或 `HOLDOUT` |

manifest 只描述测试输入，不包含正确答案。正确答案必须保存在独立 Gold Labels 文件中，运行分类时不得把 Gold Labels 传给分类服务、Prompt、文件路径或数据库分类表。

### 4.3 Gold Labels

每个样本至少标注以下字段：

| 字段 | 必填条件 | 说明 |
| --- | --- | --- |
| `sample_id` | 始终 | 关联 manifest |
| `gold_primary_category_id` | 可分类时 | 唯一物理落位主分类，使用稳定 taxonomy ID |
| `gold_primary_category_path` | 可分类时 | 供人工核对，不作为长期外键 |
| `acceptable_secondary_category_ids` | 可选 | 真实存在但不决定物理目录的次级业务分类 |
| `should_abstain` | 始终 | 当前 taxonomy 是否不足以可靠确定主分类 |
| `abstain_reason` | `should_abstain=true` | 例如多主题并列、正文不足、taxonomy 缺失、解析失败 |
| `evidence_type` | 可分类时 | `text_quote`、`sheet_cell`、`metadata` 等 |
| `page_number` | PDF/Word 可定位时 | 证据页码 |
| `sheet_name` | Excel 时 | 工作表名称 |
| `cell_range` | Excel 时 | 单元格或区域 |
| `evidence_quote` | 非“其他”分类 | 支撑主分类的短原文，不保存整篇正文 |
| `filename_strength` | 始终 | `STRONG`、`WEAK`、`MISLEADING` |
| `ambiguity_level` | 始终 | `LOW`、`MEDIUM`、`HIGH` |
| `annotator_1` / `annotator_2` | 始终 | 标注者标识 |
| `adjudication_status` | 始终 | `AGREED`、`ADJUDICATED`、`UNRESOLVED` |
| `notes` | 可选 | 简短说明，不记录敏感全文 |

### 4.4 敏感数据处理

- 原始文件、抽取全文和包含敏感文件名的完整 manifest 默认不提交 Git。
- Git 中只保存标注规范、JSON Schema、脱敏统计和评估报告模板。
- 如需要把 Gold Labels 纳入版本控制，应先把 `relative_path` 和 `filename` 替换为 `sample_id + sha256`，并确认分类标签与证据短句不包含个人敏感信息。
- 评估日志不得写入正文、密码、JWT、API key 或完整模型 Prompt。

## 5. 标注实施步骤

### 5.1 冻结版本

开始标注前记录：

- taxonomy key 与 version。
- taxonomy 文件 SHA-256。
- classifier version。
- summary provider、classification mode、graph mode。
- Git commit。
- 评估批次 ID，例如 `classification-eval-180-v1`。

taxonomy 在标注期间发生修改时，必须创建新的评估版本，不能静默覆盖旧标签。

### 5.2 文件审计与去重

对 180 个文件执行只读审计：

1. 计算 SHA-256，标记完全重复文件。
2. 基于规范化文件名、文件大小、抽取文本哈希和模板特征建立 `family_id`。
3. 完全重复项只保留一个计分样本，其余记录为 `duplicate_of`，但仍可用于导入和幂等链路测试。
4. 同模板跨年份文件不删除，而是归入同一个文件族。
5. `CALIBRATION` 和 `HOLDOUT` 必须按文件族切分，禁止同一模板的不同副本出现在两边。

### 5.3 taxonomy 覆盖预检

先让一名业务标注者为每个文件选择可能的主分类，生成覆盖矩阵：

| category_id | 分类路径 | 样本数 | 格式分布 | 是否有弱文件名样本 | 是否有混淆样本 |
| --- | --- | ---: | --- | --- | --- |

要求：

- 每个有效分类至少 1 个样本，用于链路连通性验证。
- 缺失分类必须明确列入“待补样本”，不能用其他分类文件凑数。
- 只有 1–2 个样本的分类只能报告个案结果，不能发布逐分类阈值。
- 重要或容易混淆的分类后续应扩充到至少 10–20 个不同文件族。
- 如果 180 个文件不能覆盖全部有效分类，应补充文件后再宣称“全部分类均已测试”。

### 5.4 试标与规则统一

先选择 20 个样本进行试标，覆盖：

- 明确单主题文件。
- 泛化文件名文件。
- 多主题文件。
- PDF、Word、Excel。
- 文件名和正文冲突文件。
- 应拒识或归入“其他”的文件。

两名标注者独立完成后，集中讨论冲突并完善标注手册。重点统一：

1. 主分类表示“如果只能放入一个物理目录，哪个分类最能代表文件的核心业务目的”。
2. 文种不是业务主分类；“通知”“审批表”“总结”不能覆盖文件真实业务主题。
3. 文件同时涉及多个主题时，区分核心任务、支撑材料和偶发提及。
4. 正文证据优先于文件名；文件名与正文冲突时以正文为准。
5. 无法可靠确定唯一主分类时标记 `should_abstain=true`，不得强行选择。

试标完成后重新标注这 20 个样本，避免早期规则不一致污染正式结果。

### 5.5 正式双人标注

推荐两名熟悉学校文件业务的标注者对全部 180 个样本独立标注，互相不可见对方答案，也不可见系统预测结果。流程如下：

1. 标注者查看安全预览或抽取正文。
2. 从冻结 taxonomy 中选择一个主分类或选择“应拒识”。
3. 记录可接受的次级分类。
4. 记录至少一项可定位正文证据。
5. 标记文件名强度、歧义程度和解析问题。
6. 系统自动比较两份标签。

冲突包括：

- 主分类不同。
- 一方认为应拒识、一方认为可分类。
- 分类相同但证据无法支持。
- 使用了不存在或版本不一致的 category ID。

冲突由第三人或指定业务负责人仲裁。无法仲裁的样本标记 `UNRESOLVED`，只用于解析和候选观察，不进入准确率及阈值校准。

资源不足时可以由第二人复核全部高歧义样本和随机 20% 普通样本，但该模式只适合内部摸底；用于发布自动落位策略前仍应完成全量双人复核。

### 5.6 冻结标准答案

Gold Labels 冻结前执行以下校验：

- 180 个 `sample_id` 唯一且均能关联 manifest。
- 非重复样本都有确定标签或明确 `should_abstain`。
- 所有 category ID 存在于冻结 taxonomy。
- 非“其他”分类都有证据。
- Excel 证据包含 sheet 和 cell，能够定位时不得只写泛化关键词。
- 完全重复文件标签一致。
- 同一文件族的差异有业务解释。
- Gold Labels 计算版本哈希，并禁止评估过程中修改。

## 6. 基线运行方式

### 6.1 隔离要求

评估应使用专用测试 workspace、managed root、数据库批次和 `WORKING_COPY_STORAGE_ROOT`，复用现有测试数据重置与隔离方案。不能把评估文件导入生产工作副本，也不能在评估时执行确认后的移动、删除或覆盖操作。

推荐先运行 Shadow：

```env
AUTO_PRIMARY_CLASSIFICATION_ENABLED=true
AUTO_INITIAL_PLACEMENT_ENABLED=true
AUTO_CLASSIFICATION_SHADOW_MODE=true
```

Shadow 用于记录分类候选和假设落位决策，但不根据结果移动文件。若必须验证原目录导入后的真实物理落位，应另建一次性隔离工作副本，在基线评估完成后执行，并对照同一 Gold Labels；不得用物理目录作为标准答案来源。

### 6.2 统一入口

180 个样本必须通过与生产一致的链路运行：

```text
原目录扫描/导入
-> 文档解析或旧 Office 转换
-> document_pages
-> 本地抽取式摘要
-> taxonomy 候选召回
-> 当前配置的重排/判定
-> evidence_items
-> DocumentClassificationRun / Suggestions
-> AutoPlacementPolicy Shadow 决策
```

不得为评估单独编写一个与生产 matcher 不同的“简化分类器”。离线评估脚本应调用正式服务或读取正式持久化结果。

### 6.3 结果导出

每个样本导出：

- 解析状态和 extraction run ID。
- classifier/taxonomy/calibration/policy version。
- Top-5 category ID、路径、rank、confidence、rule score。
- Top-1/Top-2 ranking score 和 margin。
- title/content/negative signals。
- evidence items 及其可定位状态。
- summary/full-text agreement。
- graph mode、semantic status 和降级警告。
- 假设自动落位决策及全部 reason codes。
- 单文件处理耗时。

导出结果通过 `sample_id` 或 SHA-256 与 Gold Labels 合并，不能依赖数据库中可能变化的 document ID 作为长期样本标识。

## 7. 评估指标

### 7.1 数据与解析指标

- 原始样本数、去重后样本数、文件族数量。
- 各扩展名数量和解析成功率。
- OCR/旧 Office 转换成功率。
- 空正文率、缺失证据率。
- taxonomy 分类覆盖数和缺失分类数。

解析失败样本应单独报告，不能算作分类器的普通错误，也不能从总报告中静默删除。

### 7.2 候选召回指标

- Top-1 accuracy：系统第一候选等于 Gold 主分类的比例。
- Top-3 recall：Gold 主分类出现在前三候选的比例。
- Top-5 recall。
- `OTHER` 或无候选比例。
- 每个分类的样本数、Top-1、Top-3 和主要混淆方向。

诊断规则：

| 现象 | 优先改进方向 |
| --- | --- |
| Top-3 recall 低 | taxonomy aliases/signals、全文特征和候选召回 |
| Top-3 recall 高但 Top-1 低 | 排序、图谱/语义重排或受限 LLM 判定 |
| Top-1 高但自动落位少 | 置信度校准和门槛策略 |
| 分类正确但证据缺失 | 解析、页码/sheet/cell 定位和 evidence projector |
| 弱文件名组明显下降 | 增强正文、标题和结构化字段特征 |
| 摘要/全文频繁冲突 | 调整摘要权重，避免摘要成为唯一主召回文本 |

### 7.3 自动落位指标

对 `should_abstain=false` 的可分类样本计算：

```text
自动落位精度 = 自动落位且主分类正确数 / 自动落位总数
自动落位覆盖率 = 自动落位总数 / 可分类样本数
待复核率 = NEEDS_REVIEW 数 / 可分类样本数
错误自动落位率 = 自动落位但主分类错误数 / 可分类样本数
```

对 `should_abstain=true` 的样本计算：

```text
正确拒识率 = NEEDS_REVIEW 数 / 应拒识样本数
危险自动落位数 = 应拒识但被 AUTO_ORGANIZED 的样本数
```

报告必须同时展示精度和覆盖率。通过把所有文件都放进 `NEEDS_REVIEW` 可以得到表面上的零误放，但不代表分类功能可用。

### 7.4 分层指标

除全局指标外，必须按以下维度分别报告：

- category ID。
- 学校/学院等 taxonomy 根分支。
- 文件格式。
- `STRONG`、`WEAK`、`MISLEADING` 文件名组。
- 普通单主题、多主题、应拒识。
- 自然文件与模板文件族。
- 摘要/全文一致与不一致。

## 8. 数据切分和阈值初校

### 8.1 文件族级切分

按 `family_id` 把去重后的样本分为：

- `CALIBRATION`：约 120 个，用于分析规则、补 taxonomy 信号和选择全局阈值。
- `HOLDOUT`：约 60 个，只用于最终复核，不参与阈值选择。

切分时尽量保持 taxonomy 根分支、格式和文件名强度分布。样本很少的分类不强制同时出现在两组；这些分类只进行连通性测试，不发布单类指标。

### 8.2 阈值扫描

在 `CALIBRATION` 上离线扫描：

- Top-1 confidence：`0.50` 到 `0.95`，步长 `0.01`。
- Top-1/Top-2 margin：`0.00` 到 `0.40`，步长 `0.01`。
- 是否要求至少 1、2、3 个正文信号。
- 是否拒绝 summary/full-text conflict。
- 是否拒绝 filename-only 和 evidence missing。

每组参数输出自动落位精度、覆盖率、错误样本和各分类分布。选择策略为：

1. 所有硬安全条件继续满足。
2. 优先保证自动落位精度。
3. 在精度相近时选择覆盖率更高的组合。
4. 不允许通过排除困难分类来制造更高的全局表面精度。

候选参数确定后，只运行一次 `HOLDOUT`。如果 Holdout 明显下降，应回到 taxonomy、特征或样本扩充阶段，不能反复查看 Holdout 后继续调参，否则 Holdout 也会变成训练集。

### 8.3 第一版发布限制

180 个样本只支持发布“初步全局回退参数”，不支持可靠的逐分类阈值。建议满足以下条件后才考虑实际自动落位：

- Holdout 中没有发现明显系统性混淆。
- 所有错误自动落位经过逐条复核并形成回归样本。
- 应拒识样本没有危险自动落位。
- 每个自动落位结果都有可定位证据。
- 新参数先进入 Shadow，观察新增真实文件，而不是立即移动现有 `ACTIVE` 文件。

逐分类校准应在主要分类积累至少 20–30 个不同文件族后实施；高风险或易混淆分类需要更多样本。

## 9. 输出物

实施完成后至少生成：

1. 数据集说明和版本信息。
2. 输入 manifest 与 SHA-256 清单。
3. Gold Labels 及其 schema。
4. taxonomy 覆盖矩阵和缺失样本清单。
5. 双人标注一致率与冲突仲裁清单。
6. 系统原始 Top-5 结果导出。
7. 全局和逐分类基线报告。
8. 混淆矩阵与失败样本明细。
9. 阈值扫描报告。
10. 推荐配置、适用范围、风险和回滚方式。
11. 固定回归样本清单。

建议的实施文件位置为：

```text
docs/classification-evaluation/
├─ labeling-guide.md
├─ manifest.schema.json
├─ gold-label.schema.json
└─ report-template.md

scripts/
├─ build-classification-eval-manifest.py
├─ export-classification-eval-results.py
└─ evaluate-classification-results.py

storage/test-artifacts/classification-evaluation/<evaluation_run_id>/
├─ manifest.csv
├─ gold-labels.csv
├─ predictions.jsonl
├─ metrics.json
└─ report.md
```

其中 `storage/test-artifacts/` 只保存本地受控测试资产并加入忽略规则，不提交真实文件正文。

## 10. 工作量估算

| 阶段 | 预计工作量 |
| --- | --- |
| 文件审计、去重、manifest | 0.5 天 |
| 20 个试标和规则统一 | 0.5 天 |
| 180 个文件第一人标注 | 1–2 天 |
| 180 个文件第二人复核/独立标注 | 1–2 天 |
| 冲突仲裁 | 0.5–1 天 |
| 评估脚本与基线报告 | 1–2 天 |
| taxonomy/阈值初步调整与 Holdout 复核 | 1–2 天 |

若文件较长、扫描质量差或 Excel sheet 较多，人工标注时间应相应增加。

## 11. 验收标准

本次 180 文件标注评估完成的标准是：

1. 180 个文件全部进入 manifest，原件未被修改。
2. 完成 SHA-256 去重和文件族分组。
3. 明确列出全部有效分类的样本覆盖情况。
4. 每个非重复样本都有冻结主分类或明确拒识标签。
5. 非“其他”分类都有可定位证据。
6. 标注冲突全部解决或标记为 `UNRESOLVED` 并排除出计分集。
7. 系统预测和 Gold Labels 通过 sample ID/SHA-256 可稳定关联。
8. 报告包含 Top-1、Top-3、自动落位精度、覆盖率、待复核率、正确拒识率和危险自动落位数。
9. 报告同时提供逐分类、逐格式和文件名强弱分层结果。
10. 阈值只在 Calibration 上选择，Holdout 不参与调参。
11. 不因本次评估自动移动、覆盖或删除任何现有工作副本。
12. 对 180 样本能力边界作出明确声明，不把初步结果宣传为逐分类 99% 精度。

## 12. 推荐实施顺序

第一步先完成 manifest、去重和 taxonomy 覆盖矩阵。只有确认 180 个文件实际覆盖了哪些分类，才能安排业务标注；如果缺少分类，应先补样本。

第二步完成 20 个试标并冻结标注手册，再开始全量标注，避免180个文件标完后才发现主分类判定标准不一致。

第三步冻结 Gold Labels，然后使用当前 `rule_only + extractive summary + graph shadow` 配置跑一次不可修改的基线。

第四步根据基线诊断问题类型：先修候选召回和 taxonomy 信号，再调排序，最后才调自动落位门槛。不能通过单纯抬高系统显示的 confidence 掩盖分类错误。

第五步使用 Calibration 选择初步参数，用 Holdout 做一次复核；通过后仍先运行 Shadow，再决定是否启用真实自动落位。
