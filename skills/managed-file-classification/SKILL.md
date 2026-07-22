# managed-file-classification

## Trigger
用户要求对服务器受管目录、子目录或筛选结果中的文件执行分类或重新分类。

典型表达：

```text
对党办下文件进行分类。
重新按正文分类党办/2026下所有 PDF。
对 downloads 下文件名包含“科学发展观”的文件分类。
```

## Inputs
受管目录逻辑范围与分类控制参数：`root_key`、`path_prefix`、`extension`、`filename_contains`、`recursive`、`force_reprocess`。真实文件集合必须由后端受管目录服务解析，LLM 不得生成服务器绝对路径或猜测文件 ID。

## Outputs
同步逐文件分类结果，或大批量异步 Job：

```json
{
  "intent": "CLASSIFY_MANAGED_FILES",
  "selected_skills": ["managed-file-classification"],
  "steps": [
    {
      "skill": "managed-file-classification",
      "tool_name": "classify-managed-files",
      "input": {
        "root_key": "downloads",
        "path_prefix": "党办/2026",
        "extension": "pdf",
        "recursive": true,
        "force_reprocess": false
      }
    }
  ]
}
```

每个文件结果必须包含解析状态、多个分类建议、置信度、正文证据、复用状态、警告和错误。异步结果必须包含可查询的 `job_id`，完成后回写原 AgentRun。

## Allowed Tools
`classify-managed-files`, `job-status-read`, `feedback-record`

## Open Source Backing
业务编排与分类边界为项目自研。正文解析间接使用 Docling、python-docx、PyMuPDF、openpyxl、LibreOffice 和 OCR adapter；其中 `.xls` 先隔离转换为临时 `.xlsx`，不使用 xlrd。图谱候选增强间接使用 Neo4j 与 `neo4j-graphrag-python`。开源组件只作为受控 Tool/Service adapter，不得直接操作受管源文件。

## Steps
1. Planner 判断 `CLASSIFY_MANAGED_FILES`，在普通上传附件分类分支之前解析受管目录范围。
2. 后端校验 `root_key`、逻辑 `path_prefix`、扩展名和文件名条件，并排除隐藏项与 `MISSING` 文件。
3. `GlobalManagedCategoryCatalogService` 从所有启用的 `PATH_AS_CATEGORY` 来源根加载一套全局分类候选目录。
4. Profile 只允许 `CATEGORY` 目录进入候选集；当前文件位置只提供 `LOCATED_IN` 和 `PATH_SUGGESTS` 弱信号。
5. 为每个文件创建或复用只读快照，从 `document_pages.text_content` 完整正文、OCR 文本或 Sheet 内容分类。
6. 同步阈值内逐文件执行；超过阈值创建 `CLASSIFY_MANAGED_FILES` Job，由 Worker 分批处理并隔离单文件失败。
7. 保存分类运行和多条建议，生成逐文件 ChangeSet，并把轻量结果写回 AgentRun。
8. 用户明确接受、拒绝或更正后记录反馈；只有明确接受或更正才能形成确认分类关系。

## Evidence Rules
目录、文件名、扩展名和元数据只能作为候选或弱信号。非“其他”业务分类必须有可定位的正文、OCR、页码、工作表或单元格证据；没有正文证据时必须进入 `NEEDS_REVIEW`。一个文件允许保留多个不同分类，不得只保留最高分。

## ChangeSet Rules
同步和异步批次都必须逐文件记录：

- `MANAGED_FILE_SNAPSHOT_CREATED` / `MANAGED_FILE_SNAPSHOT_REUSED`
- `TEXT_EXTRACTED` / `TEXT_REUSED`
- `CATEGORY_SUGGESTED` / `CATEGORY_SUGGESTION_REUSED`
- `DOCUMENT_PROCESSING_FAILED`

ChangeSet 只表示分析和分类建议，不表示源文件被移动、复制、重命名或覆盖。

## OperationPlan Rules
生成分类建议不需要确认。本 Skill 禁止执行物理整理；后续移动、复制、重命名或删除必须创建独立 OperationPlan 并由用户确认。

## Failure Handling
单个文件解析或分类失败不得回滚同批次其他文件。分类来源已配置但全局目录为空时返回 `NEEDS_REVIEW`，不得静默切换到项目预置业务 taxonomy。Neo4j 关闭或故障时降级为 PostgreSQL 全局目录和正文分类，不能导致基础任务失败。

## Tests
必须覆盖：

- “对党办下文件进行分类”稳定生成 `CLASSIFY_MANAGED_FILES`。
- 上传文件与受管文件使用相同的全局分类目录版本。
- 不同来源根中的相同分类路径合并为同一稳定 ID。
- 一个文件可持久化和展示多个分类。
- 文件父目录不生成 `CONFIRMED_AS`。
- 未变化文件复用快照、正文与分类结果。
- 异步批次单文件失败隔离，并最终回写 AgentRun 和 ChangeSet。
- 隐藏文件、隐藏目录和 `MISSING` 文件不进入任务。

## Forbidden
不得把当前父目录直接当作确认分类；不得按当前受管根裁剪全局候选；不得让 LLM 生成绝对路径或文件 ID；不得只使用文件名完成业务分类；不得修改受管源文件；不得把普通建议投影为 `CONFIRMED_AS`；不得在分类目录为空时静默混用另一套业务 taxonomy。
