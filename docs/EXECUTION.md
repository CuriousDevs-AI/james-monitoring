# Approval execution and recovery

Human approval and successful execution are different facts. An approved request is not proof that a
message reached its destination or a background coding run finished.

## Covered in this increment

For approved requests of kind `action`, `merge` and `github_change`, James persists an execution attempt
before running the side effect. The attempt includes the approver, timestamp and hash of the request. Changes between
preflight and the durable write are refused. Action requests show the proposed action, destination and full
content in Details; requests exceeding the chat preview require console approval.

The execution record is stored in the ask's Markdown front matter, alongside its existing decision and
outcome. Atomic file replacement flushes both file contents and the containing directory on POSIX hosts.
A nonblocking local OS file lock prevents two runtimes/processes deciding the same request concurrently.
File locks require a POSIX host and a local filesystem with correct lock/fsync semantics; do not deploy this
on a shared/network filesystem or claim distributed exactly-once delivery.

- `started`: intent was persisted, but completion was not recorded. It may still be running, or the process
  may have stopped at any point. Never infer that no side effect happened.
- `succeeded`: the action handler returned successfully. For `run_code`, this means the background run was
  started, not that code passed tests, was merged, or was deployed.
- `uncertain`: execution raised or was cancelled. Partial/external effects may already have happened.
- `reconciled`: the owner checked external evidence and recorded the outcome. Reconciliation itself never
  executes the action, retries it, sends messages, or invokes an agent.

Any recorded attempt prevents re-approving/rejecting that same request through the normal decision route.
Interrupted and uncertain task-linked work is held in scheduling, dispatch and action checks until reconciled.
Unrelated one-off work remains available. Do not create a replacement request merely to bypass this hold.

GitHub proposals are checked against the current connection and task before the attempt is recorded.
Merge dependency checks also run first. Once a merge/proposal handler starts, an exception is conservatively
uncertain even if it may have failed before changing anything. There is no longer an instruction to blindly
approve the same failed merge again. Inspect the local repository/remote PR or task state and reconcile.
Cancelling a merge waits for its bounded git worker to exit before releasing the decision lock; cancelling
an asyncio task cannot stop an already-running thread. This may delay shutdown until command timeouts expire.
Reconciliation records evidence only: it does not repair task state, merge code or replay follow-up messages.

## Owner recovery API

The owner console exposes this flow under Approvals → Execution recovery. It shows interrupted/uncertain
attempts separately from normal approval buttons, requires a verified outcome, evidence and an explicit
verification checkbox, and never offers a retry button. Unsaved evidence is protected from automatic refresh.

`GET /api/executions` lists all unresolved attempts (not just the latest 50 approvals). The normal asks API
includes execution metadata; the owner's notifications flag unresolved attempts. Neither endpoint executes
anything. Both recovery endpoints require the authenticated owner, not an admin or an agent.

After checking the destination, use `POST /api/ask/reconcile`:

```json
{"id":"ASK-001","outcome":"completed","evidence":"Destination receipt confirms the reviewed message was delivered."}
```

Alternatively, use `not_completed` only after verifying the effect did not happen. Evidence is required.
The request is closed as approved/completed or rejected/not-completed, with an audit entry. A live decision
held by another process cannot be reconciled. A later new request still requires a new human review.
Do not paste credentials or private provider diagnostics into the evidence field.

## Limits still requiring work

Approved actions, merges and inbound GitHub proposals are journaled. Rejection-triggered PR closure,
green/yellow actions and background coding results are not yet covered by this execution record. Scheduled
runs have their own receipts (see SCHEDULER.md). A successful approval handler does not prove completion of
any follow-up tasks it spawned. Do not treat their request IDs or runtime state as a transactional ledger.
Merge requests still identify a branch/PR, not an immutable reviewed commit; head-change validation remains
a release requirement. The execution hash detects request-file changes during preflight, not a changed remote
PR head or a stale approval page opened before another edit.

There is no automatic retry, external provider reconciliation, outbox delivery, or cross-machine idempotency.
The same operation intentionally created under another approval ID is a separate request, not automatically
deduplicated. A provider receipt must be checked by the owner when an outcome is ambiguous. Files/history
must be backed up; restoring an older copy can lose knowledge of newer effects. Same-user arbitrary code
can still access these files and is outside this trust boundary. A complete credential sandbox is separate.
