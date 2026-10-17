# Runtime backup and offline recovery

GitHub Projects is a task-board mirror, not a company backup. Shared files/history, company configuration,
private runtime state and provider credentials have different recovery paths.

## What the daily snapshot contains

The daily report snapshots workspace `.jm/` into `.jm/backups/YYYY-MM-DD.zip`. This includes local chats,
conversation state, scheduler receipts and other runtime metadata. Backups, model scratch/sandbox folders,
locks, symlinks and `.env`/`.env.*` files are excluded. Keychain values are never read by this operation.
Shared tasks/approval files outside `.jm/`, config.yaml, code repositories and their Git histories are **not**
in this ZIP. Credentials accidentally pasted into chat or arbitrary files may be present: treat it as sensitive.

The backup folder is mode 0700; new ZIPs are mode 0600. Files are written to a temporary private ZIP, flushed
and atomically replaced before date-named retention removes older backups (14 by default). A failed ZIP
write leaves the previous valid archive intact. A live backup is a best-effort per-file snapshot, not a
transaction across config/tasks/chats/approvals. Stop all writers before taking a recovery checkpoint.

After stopping them, `jm -c /private/company/config.yaml backup-runtime --confirm-stopped` takes an offline
runtime snapshot without starting services. The flag is your confirmation, not automatic process detection.
Keep the matching config/shared-file checkpoint separately. `--keep` defaults to 14 date-named snapshots.

ZIP compression is **not encryption**. Keep backups on protected storage; encrypt before off-host transfer.
Nothing here uploads backups to GitHub, Telegram or another service. Old backups may have legacy permissions;
review those separately. This does not protect against arbitrary code running as the same OS user.

## Safe offline extraction

Work on copies, on a stopped test installation first. The destination must not exist, even as an empty folder:

```sh
jm restore-runtime /secure/copy/2026-10-17.zip /secure/recovered-runtime --acknowledge-private-data
```

This validates entry paths, types, duplicates, CRCs, file/directory conflicts and size limits before creating
the destination. It refuses links, path traversal and excluded entries, creates private files and never starts
services or loads credentials. Limits are 10,000 files and 250 MB uncompressed; large installations need a
separately reviewed restore path. CRCs detect damage, not malicious tampering: use a trusted protected archive.
If a disk error interrupts extraction, partial output remains with `RESTORE-INCOMPLETE`; do not use it. Pick a
new destination for another attempt. No existing destination is overwritten or recursively deleted.

## Recovery drill and release gate

1. Stop every James process/CLI writer for that company. Keep the original installation and archive intact.
2. Recover the matching shared workspace revision and code repositories from their private Git backups,
   plus the corresponding config.yaml from protected storage. Check for unpushed commits/uncommitted work.
3. Extract the matching runtime archive into a fresh private directory with the command above. Keep scheduler
   receipts and action approvals from the same checkpoint. Review newer external effects: restoring an older
   snapshot can forget sends, approvals or work performed after that checkpoint.
4. In a separate test company, review and update absolute paths, repo destinations, identity and timezone.
   Keep credentials absent, outbound channels disconnected and scheduling disabled during inspection.
   Move the recovered runtime directory into that new workspace as `.jm` only after checking its contents.
5. Re-provision credentials separately into the selected store; do not assume copied references contain keys.
   Third-party CLI login sessions must be re-established separately. Review/reissue console access links.
6. Reconcile interrupted/uncertain actions and scheduled runs against destination evidence before enabling
   future work. Verify tasks, private chats, approvals, dependencies and history in the test company first.
7. Only then choose the active installation and reconnect approved test destinations. Do not run the original
   and restored company against the same live accounts at the same time.

Automated tests cover extraction/permissions, failed-write preservation, path attacks, retention and local
round trips using synthetic data. A full cross-machine/off-host recovery drill and encrypted backup-target
configuration remain release requirements; no real company data or credentials have been restored here.
