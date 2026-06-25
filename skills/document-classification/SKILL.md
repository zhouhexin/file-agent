# document-classification

## Trigger
Document content, metadata, chunks, or filename are available for classification.

## Inputs
Document id, metadata, chunks, taxonomy version.

## Outputs
Document categories with relation role, confidence, status, evidence.

## Allowed Tools
`multi-label-classify`, `hybrid-search`, `document-lineage-read`

## Open Source Backing
Classification orchestration is project-specific. Evidence retrieval can use open-source-backed `hybrid-search` through LangChain, LlamaIndex, and pgvector adapters.

## Steps
Score category candidates independently, verify evidence, save multiple labels, mark low confidence for review.

## Evidence Rules
Each applied or suggested category needs quote, page/sheet/cell, or metadata evidence.

## ChangeSet Rules
Record category additions, removals, and status changes.

## OperationPlan Rules
No confirmation required for classification suggestions or auto-applied labels.

## Failure Handling
Unclear documents become NEEDS_REVIEW; unsupported categories are not forced.

## Tests
One document can receive multiple categories and rejecting one does not delete others.

## Forbidden
Do not keep only the highest-scoring category or fabricate evidence.
