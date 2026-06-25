# change-report

## Trigger
One or more Tool calls completed and user needs a receipt.

## Inputs
AgentRun id, Tool invocation ids, ChangeSet items.

## Outputs
Per-file receipt and ChangeSet summary.

## Allowed Tools
`change-report`

## Open Source Backing
No direct open-source dependency. This Skill is project-specific audit and receipt logic.

## Steps
Aggregate changes, group by file, summarize success/failure/needs_review, state original file status.

## Evidence Rules
Include evidence for classifications and answer references where applicable.

## ChangeSet Rules
Creates or finalizes ChangeSet summary.

## OperationPlan Rules
Link pending OperationPlans when present.

## Failure Handling
Partial failures must be shown separately.

## Tests
Receipt states original file unchanged.

## Forbidden
Do not collapse batch results into only aggregate counts.
