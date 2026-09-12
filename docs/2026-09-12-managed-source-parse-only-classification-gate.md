# 受管目录“仅解析、后分类”开关开发说明

## 目标

允许部署者在受管目录首次接入阶段只执行扫描、正文解析、OCR、元数据提取和全文索引；不生成分类建议、不创建分类刷新任务，也不因分类缺失触发工作副本物化、改名或落位。

分类规则调整完成后，重新启用开关并复用已经持久化的 `DocumentPage`、`DocumentExtractionRun` 和索引结果，执行既有 `REFRESH_MANAGED_SOURCE_CLASSIFICATION`，不得重新读取或转换原文件。

## 配置契约

新增环境变量：

```env
MANAGED_SOURCE_CLASSIFICATION_ENABLED=true
```

- 默认值为 `true`，升级后所有既有部署和入口的行为保持不变。
- 值为 `false` 时，仅影响受管源目录的 `ANALYZE_MANAGED_FILE_REVISION` 与 `REFRESH_MANAGED_SOURCE_CLASSIFICATION`。
- 不影响普通聊天上传、WorkBuddy 批量导入、手工分类、搜索、读取、OCR、解析器或分类规则配置。

解析期部署建议同时设置：

```env
MANAGED_SOURCE_ANALYSIS_ENABLED=true
MANAGED_SOURCE_CLASSIFICATION_ENABLED=false
MATERIALIZE_ALL_MANAGED_FILES=false
MATERIALIZE_RELEVANT_FILES_AFTER_RESPONSE=false
```

其中后两个开关维持“不创建工作副本”的运维边界；分类总开关自身也必须防止因分类新鲜度缺失而创建物化或分类刷新任务。

## 行为边界

### 开关关闭

`ANALYZE_MANAGED_FILE_REVISION` 仍会：

- 创建或复用受控分析 Document / DocumentVersion；
- 写入提取运行、页面、表格结构、摘要和检索投影；
- 建立全文索引；
- 更新受管源修订为 `READY`。

它不会：

- 调用 `ClassificationRuntimeFactory`；
- 推断或创建用途材料包；
- 写入分类运行、分类建议或源分类投影；
- 投递 `REFRESH_MANAGED_SOURCE_CLASSIFICATION`；
- 因分类不新鲜投递 `MATERIALIZE_WORKING_COPY`。

已在队列中的分类刷新任务被 Worker 以结构化“已跳过”结果结束，不执行分类器。这样切换开关后不会因旧任务继续产生分类数据。

### 开关重新开启

对 `READY` 且正文解析仍有效的源修订，既有扫描/协调逻辑会投递
`REFRESH_MANAGED_SOURCE_CLASSIFICATION`。该任务走现有 `refresh_classification`，复用持久化正文，不重新解析原件。

## 改动范围与验证

仅修改配置加载、受管源分析服务、受管目录 Worker 和测试；不改变数据库 schema、MCP、API 或分类规则。

测试必须覆盖：

1. 默认值保持启用；
2. 关闭时普通文本仍成功解析和索引，且分类器/用途包推断不会被调用；
3. 关闭时不写入分类建议；
4. 已排队的分类刷新任务关闭时不执行；
5. 重新开启后分类刷新复用已有正文。
