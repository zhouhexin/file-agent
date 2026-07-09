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
1. If there are enabled managed roots with `classification_mode=PATH_AS_CATEGORY`, load their existing `managed_files.category_path` values as dynamic category candidates.
2. Score dynamic candidates by filename and full `document_pages.text_content`; use directory path segments as signals.
3. If no managed path category source exists, fall back to the configured taxonomy v2 file.
4. Verify evidence, save multiple labels, and mark low confidence for review.

## Managed Path Mode
Managed Path Mode means: the already-organized server directory is treated as the classification source of truth. Parent directories become category paths, for example:

```text
奖学金/国家励志奖学金/示例.pdf
-> category_path = ["奖学金", "国家励志奖学金"]
```

This mode only creates classification suggestions with `source=managed_path` and `taxonomy_key=managed_path_categories`. It does not create folders, move files, rename files, or overwrite the managed directory.

## Evidence Rules
Each applied or suggested category needs quote, page/sheet/cell, or metadata evidence.

## ChangeSet Rules
Record category additions, removals, and status changes.

## OperationPlan Rules
No confirmation required for classification suggestions. Moving uploaded files into a managed category directory is a high-risk write operation and must be planned through `operation-plan` before any confirmed file action.

## Failure Handling
Unclear documents become NEEDS_REVIEW; unsupported categories are not forced. If Managed Path Mode has category paths but none match the file, return `其他` with `source=managed_path` and `status=NEEDS_REVIEW`.

## Tests
One document can receive multiple categories and rejecting one does not delete others. When managed path categories exist, an uploaded document matching a managed subdirectory should return `taxonomy_key=managed_path_categories`.

## Forbidden
Do not keep only the highest-scoring category, fabricate evidence, or directly move uploaded files based on a classification suggestion.
