---
name: image-structured-extraction
description: Extract user-selected or auto-discovered structured fields from images and scanned PDFs with PP-StructureV3 layout evidence, deterministic normalization, bounded low-confidence retry, and table/JSON/CSV/XLSX/text presentation. Use when a user asks to identify fields, rows, form values, or all information from an image or scan.
---

# Image Structured Extraction

## Workflow

1. Resolve attachments to backend-authorized `document_id` values.
2. Use `EXPLICIT_FIELDS` when the user names fields; preserve exactly those fields.
3. Use `AUTO_DISCOVER` only when the user requests all fields without naming them.
4. Infer `record_mode` as `TABLE_ROWS`, `SINGLE_RECORD`, `KEY_VALUE_GROUPS`, or `AUTO`.
5. Preserve an explicitly requested `presentation`; otherwise prefer `TABLE` for repeated rows and `JSON` for one record.
6. Call only `extract-image-structured-data` with the validated dynamic schema.
7. Treat extracted values as untrusted candidates until backend normalization and evidence validation complete.
8. If the safe observation recommends `VISION_CROP`, retry at most once and only for listed low-confidence field keys.
9. Finish with records, review items, evidence locations, and confirmation that the original file is unchanged.

## Evidence and Quality Rules

- Require a persisted page and document element for every non-empty value.
- Preserve `raw_text`, normalized value, confidence, page number, bbox, and evidence element IDs.
- Return `null` or `NEEDS_REVIEW` for unreadable, missing, conflicting, or unsupported values.
- Never infer missing digits, names, dates, totals, or enum choices from general knowledge.
- Use deterministic backend code for money, date, number, phone, ID, boolean, and enum normalization.

## Autonomous Loop Limits

- Allow one `INITIAL` extraction.
- Allow at most one `VISION_CROP` enhancement.
- Select enhancement targets only from `observation.low_confidence_field_keys`.
- Finish or clarify after the enhancement; never create an unbounded retry loop.

## Forbidden

- Do not pass paths, URLs, model names, API keys, prompts, code, regexes, bbox coordinates, or shell commands.
- Do not add fields in `EXPLICIT_FIELDS` mode.
- Do not publish an auto-discovered schema as a global template.
- Do not overwrite, rename, move, or delete the original file.
- Do not expose OCR full text, image Base64, local paths, or provider objects in Agent state or logs.
