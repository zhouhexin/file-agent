# file-search

## Trigger
User asks to find files, materials, prior context, or related evidence.

## Inputs
Query, conversation id, attachment ids, explicit user preferences.

## Outputs
Ranked search results, source locations, recommendation reasons.

## Allowed Tools
`hybrid-search`, `document-lineage-read`

## Open Source Backing
Uses open-source-backed `hybrid-search` through LangChain retrievers, LlamaIndex QueryEngineTool, and pgvector adapters.

## Steps
Search current attachments, current conversation, explicit preferences, and workspace fallback.

## Evidence Rules
Results must include source document and location when available.

## ChangeSet Rules
No ChangeSet unless a downstream user action changes state.

## OperationPlan Rules
No confirmation required for search.

## Failure Handling
Return no-result explanation and suggested next query.

## Tests
Current conversation results outrank workspace fallback when relevant.

## Forbidden
Do not let user preference override objective high relevance.
