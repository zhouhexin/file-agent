# operation-plan

## Trigger
User requests rename, move, copy, delete, export, external send, or memory clearing.

## Inputs
Requested operation, target documents, proposed before/after values.

## Outputs
PLANNED OperationPlan.

## Allowed Tools
`operation-plan-create`

## Open Source Backing
No direct open-source dependency. This Skill is project-specific risk planning and confirmation gating.

## Steps
Validate targets, build before/after plan, assign risk, require confirmation.

## Evidence Rules
Filename or metadata suggestions should include source evidence when possible.

## ChangeSet Rules
No execution ChangeSet until confirmed.

## OperationPlan Rules
All high-risk operations remain PLANNED before confirmation.

## Failure Handling
Ambiguous targets require clarification.

## Tests
Rename request creates plan and does not execute.

## Forbidden
Do not execute high-risk operations directly.
