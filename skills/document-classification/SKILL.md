# document-classification

## Trigger
Document content, metadata, chunks, or filename are available for classification.

## Inputs
Document id, metadata, chunks, taxonomy version, and optional managed-directory category source.

## Outputs
Document categories with relation role, confidence, status, evidence.

## Allowed Tools
`multi-label-classify`, `hybrid-search`, `document-lineage-read`

## Open Source Backing
Classification orchestration is project-specific. Evidence retrieval can use open-source-backed `hybrid-search` through LangChain, LlamaIndex, and pgvector adapters.

External Skill references:

- ComposioHQ `awesome-claude-skills` / `file-organizer` is used only as a conceptual reference for “analyze before organizing”; do not copy external Skill content into this repository.
- Anthropic `anthropics/skills` is used only as a conceptual reference for self-contained Skill instructions and tool boundaries; document edits or file moves must still go through this project's Tool and OperationPlan rules.

## Steps
1. 如果存在启用的 `PATH_AS_CATEGORY` 来源根，通过 `GlobalManagedCategoryCatalogService` 加载所有来源根共同形成的全局分类候选集。
2. 只允许 Profile 标识为 `CATEGORY` 的目录进入候选集；相同规范化分类路径跨根合并为同一个稳定分类 ID。
3. 路径、目录名、文件名、扩展名和元数据只能用于候选召回和弱信号，不能单独确认分类。
4. 分类前读取受控正文，使用 `document_pages.text_content`、OCR、PDF 页文本、Word 段落、Excel Sheet/单元格或压缩包子文件清单作为确认依据。
5. 对文件名、完整正文、目录弱信号和可用图谱上下文分别评分，保留所有达到门槛的多标签建议。
6. 配置了受管分类来源但目录为空时进入 `NEEDS_REVIEW`，不得静默混入或回退到另一套预置业务 taxonomy。
7. 校验证据并保存多条建议；低置信度或缺少可定位正文证据的结果进入待复核。

## Content Confirmation Rules
Filename-based classification is candidate recall, not final judgment. Generic names such as `通知`, `工作安排`, `审批表`, `会议纪要`, `日报表`, `制度汇编`, scanned PDFs, and archives must be opened through approved parsing tools before business category, document type, date, related unit, and rename suggestions are confirmed.

If filename/path signals conflict with body evidence, prefer the body evidence and include the conflict in the returned warning list. If body evidence cannot be produced for a non-obvious category, return `status=NEEDS_REVIEW` instead of forcing the filename-derived category.

## Managed Path Mode
`PATH_AS_CATEGORY` 表示经 Profile 审核的 `CATEGORY` 目录可以贡献全局分类候选，不表示目录中的文件已经被确认分类。例如：

```text
奖学金/国家励志奖学金/示例.pdf
-> category_path = ["奖学金", "国家励志奖学金"]
```

目录位置只生成 `LOCATED_IN` 和可选 `PATH_SUGGESTS` 弱关系。正文分类生成 `SUGGESTED_AS`；只有用户明确接受或更正反馈才能形成 `CONFIRMED_AS`。该模式不创建目录、不移动文件、不重命名文件，也不覆盖受管源文件。

## Evidence Rules
Each applied or suggested category needs quote, page/sheet/cell, or metadata evidence.

Content-confirmed categories should prefer `text_quote` evidence from page, paragraph, sheet, or cell locations. Metadata-only evidence is acceptable only for structural labels such as source department, file format, temporary file, archive, or unknown file; it is not enough for business-topic classification when the document content is available.

## ChangeSet Rules
Record category additions, removals, and status changes.

## OperationPlan Rules
No confirmation required for classification suggestions. Moving uploaded files into a managed category directory is a high-risk write operation and must be planned through `operation-plan` before any confirmed file action.

## Failure Handling
不明确的文档进入 `NEEDS_REVIEW`，不得强制匹配分类。全局受管分类目录存在但没有候选匹配时，返回 `其他`、`source=managed_global_catalog` 和 `status=NEEDS_REVIEW`；Neo4j 不可用时降级到 PostgreSQL 全局目录和正文分类。

## Tests
一个文档可以获得多个分类，拒绝其中一个不能删除其他建议。上传文件和受管文件必须共享 `taxonomy_key=managed_global_categories` 及同一目录版本；文件当前父目录不得自动形成确认分类。

## Forbidden
Do not keep only the highest-scoring category, fabricate evidence, or directly move uploaded files based on a classification suggestion.
