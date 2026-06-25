# chat-intake

## Trigger
Every user message in a conversation.

## Inputs
User message, conversation id, attachment document ids, recent conversation context.

## Outputs
Intent, slots, attachment context, candidate skills.

## Allowed Tools
`job-status-read`, `document-lineage-read`

## Open Source Backing
No direct open-source Skill dependency. Runtime orchestration is expected to run inside the project's LangGraph Agent Runtime.

## Steps
Identify intent, extract slots, resolve attachment references, propose candidate skills.

## Evidence Rules
Do not make factual claims from file content.

## ChangeSet Rules
No ChangeSet unless a downstream Tool changes analysis state.

## OperationPlan Rules
Mark high-risk user intent for `operation-plan`.

## Failure Handling
Return clarification need when intent or target documents are ambiguous.

## Tests
Intent extraction, attachment resolution, high-risk intent detection.

## Forbidden
Do not execute Tool side effects directly.
