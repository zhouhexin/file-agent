# feedback-and-memory

## Trigger
User submits feedback, corrects the agent, or explicitly asks to remember/forget a preference.

## Inputs
Target type, target id, feedback type, comment, memory command.

## Outputs
Feedback record, explicit user preference, or OperationPlan for destructive memory action.

## Allowed Tools
`feedback-record`, `operation-plan-create`

## Open Source Backing
No direct open-source dependency. Feedback persistence and user preference memory are project-specific.

## Steps
Validate access, record feedback, store explicit non-destructive preference, or create confirmation plan for destructive memory changes.

## Evidence Rules
Feedback and preference are not objective file evidence.

## ChangeSet Rules
Record memory state changes when preferences are added or removed.

## OperationPlan Rules
Clearing many preferences requires confirmation.

## Failure Handling
Invalid targets return 403/404; ambiguous memory commands require clarification.

## Tests
User can submit feedback; clear-all memory requires OperationPlan confirmation.

## Forbidden
Do not automatically update production Skill, classification, or objective file facts from feedback.
