# confirmed-file-action

## Trigger
User confirms an OperationPlan.

## Inputs
OperationPlan id, confirmation text, user id.

## Outputs
Execution result and ChangeSet.

## Allowed Tools
`confirmed-file-action`, `change-report`

## Open Source Backing
No direct open-source dependency. File mutation is performed only by project-owned confirmed Tool implementations.

## Steps
Verify ownership and status, execute confirmed action, create ChangeSet, return receipt.

## Evidence Rules
Show before/after for every changed target.

## ChangeSet Rules
Every executed action must create ChangeItems.

## OperationPlan Rules
Only execute PLANNED or WAITING_CONFIRMATION plans with valid confirmation.

## Failure Handling
Failed item remains failed without rolling unrelated successes unless operation is atomic.

## Tests
Plan executes only after confirmation.

## Forbidden
Do not execute cancelled or already executed plans.
