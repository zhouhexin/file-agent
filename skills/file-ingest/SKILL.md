# file-ingest

## Trigger
User uploads files or asks the agent to read, ingest, parse, or prepare files for later work.

## Inputs
Conversation id, user id, document ids or upload ids, requested outputs.

## Outputs
Document versions, artifacts, pages/tables, chunks, embeddings, metadata, read profile, read quality, initial ChangeSet.

## Allowed Tools
`document-register-upload`, `security-scan`, `document-convert`, `table-extract`, `artifact-write`, `metadata-extract`, `chunk-build`, `embedding-generate`

## Open Source Backing
Uses open-source-backed Tool Adapters: `document-convert` via Unstructured, Haystack, Docling, LlamaIndex, or LangChain; `table-extract` via Haystack or openpyxl; `chunk-build` via LangChain or LlamaIndex.

## Steps
1. Register files and run scan.
2. Profile the file type, page/sheet count, character count, OCR need, and OCR usage.
3. Convert documents with open-source adapters, extract tables, and write `document_pages`.
4. Attach unified `read_quality` values: `GOOD`, `PARTIAL`, `OCR_NEEDED`, or `FAILED`.
5. Extract metadata, chunk and embed, then record ChangeSet.

## Evidence Rules
Preserve page, sheet, cell range, or character span whenever available.

## ChangeSet Rules
Record extracted text, artifacts, metadata, chunks, and original-file unchanged status.

## OperationPlan Rules
No confirmation required unless user requests high-risk file mutation.

## Failure Handling
Unsupported, encrypted, malformed, or low-quality files become NEEDS_REVIEW or FAILED with reasons. Text-empty PDFs with OCR disabled should be marked `OCR_NEEDED`, not `GOOD`.

## Tests
Supported file creates version, artifact, chunks, embeddings, read profile, read quality, and ChangeSet.

## Forbidden
Do not overwrite originals or execute macros/scripts.
