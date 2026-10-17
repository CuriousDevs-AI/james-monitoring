# Scheduled execution receipts

Schedule markers mean **triggered**, not completed. Each attempted occurrence now has a receipt in
`<workspace>/.jm/schedule-runs/`, written before its handler starts. These private files are not committed
to Git or mirrored to GitHub Projects. Include them in an encrypted, access-controlled workspace backup.

## States and guarantees

- `scheduled`: claimed on disk, handler not yet entered.
- `running`: handler entered; no completion has been recorded.
- `completed`: handler returned, including its work-session digest callback where configured. This is **not**
  proof that all subtasks succeeded or an external message was delivered: some runtime handlers report
  problems in their result instead of raising. Check task/approval/provider evidence separately.
- `failed`: handler raised an exception; only its class name is recorded, never raw provider diagnostics.
- `interrupted`: cancellation, or an orphaned scheduled/running receipt found after its process released
  the job-family OS lock. Partial external effects may already have happened.
- `skipped`: a due work-session slot was consumed while the team was paused; it is not backfilled on resume.

One local POSIX file lock covers each job family. All work-session slots share a family, so long sessions
cannot overlap. GitHub sync, reports, checks, briefs and reminders use their own families. Background jobs
keep their lock until their handler finishes. Another scheduler cannot steal a live run based on a timeout.
On the next tick, orphaned receipts become interrupted. Corrupt receipts stop scheduling; they are never
silently reset or recovered from an older copy that could forget an external effect.

Failed/interrupted runs hold **future occurrences of that family** until the owner acknowledges them. Other
families continue. There is no automatic retry and no resend button. The original occurrence cannot run
again even after acknowledgment. On a company's first startup, already-passed slots are still intentionally
not backfilled; these historical skipped slots predate the receipt lifecycle.

## Owner API

`GET /api/schedule/runs` returns the 200 most recent receipts plus every unacknowledged failed/interrupted
receipt in `needs_review`. Receipt evidence is owner-only. Health reports the unresolved count.

After inspecting destinations and any task/approval records, the owner can submit:

```json
{"id":"<64-character receipt id>","evidence":"Checked the destination, corrected partial effects; future occurrences are safe."}
```

to `POST /api/schedule/acknowledge`. Evidence is required. This records who checked what, does not change
the original failure into success, does not call an agent and does not retry that occurrence. The next due
occurrence may start. Never include secrets in evidence. UI controls are not part of this backend increment.

## Deployment and limits

Use a local POSIX filesystem with functioning locks and fsync. Shared/network filesystems, multiple hosts,
Windows and distributed exactly-once execution are not supported. Do not run two schedulers against copies
of the same workspace. Restore receipts together with runtime state; an older backup may forget effects.
Receipts are retained, not automatically pruned: plan disk capacity and secure retention for long deployments.

This covers scheduler-owned jobs only, not manual work-session commands or agent-created background tasks.
It is not an external delivery outbox, provider-reconciliation system or a recovery backup implementation.
No live Telegram, GitHub, model or outreach account was used to validate these synthetic tests.
