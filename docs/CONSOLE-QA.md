# Owner controls: verification record

The Secrets and execution-recovery controls reuse the existing console components; no new frontend
dependency or external design-source upload is involved.

## Automated evidence

- The initial owner-controls increment passed 310 Python tests, including the Node form-test wrapper.
  The project-resource integration passes 369 Python tests.
- Twelve dependency-free Node tests check JavaScript parsing, manual secret-status loading, duplicate-save
  prevention, clearing secret inputs on success/failure, non-disclosure of error details, and evidence,
  outcome and explicit-verification requirements for recovery submissions. Status refresh cannot race a save.
- Project form tests cover retained resource notes/draft descriptions, mandatory model consent, context-signature
  submission, explicit human task review, and non-retryable chat footers for uncertain attempts.
- These isolated DOM stubs test behavior, not browser layout or actual credential storage.

## Local browser evidence

An isolated, paused fixture uses fake models, no messaging transports, synthetic requests and a no-op
credential endpoint. No real credentials were entered or saved.

- Secrets initially shows unchecked status and disabled inputs; explicit status loading displays
  configured/missing/unavailable references. A synthetic save clears the password and shows restart guidance.
- Keyboard Settings navigation works and leaving Secrets clears an unsaved password.
- Recovery renders uncertain actions separately from pending approvals, alongside interrupted scheduled
  runs. The attempted action has no replay/approval button.
- Desktop layout was visually inspected. At the narrow browser test size, measured content width equaled
  viewport width (433 CSS pixels); no horizontal overflow was observed.
- A fresh browser session completed action reconciliation and scheduled-run acknowledgment after explicitly
  checking the verification checkbox. Backend verification confirmed no replay. The earlier browser-submission
  blocker is resolved; this remains synthetic local evidence, not a real provider-delivery test.

## Project workflow browser checks — October 17, 2026

- Added resource notes through the console, archived/restored the entry, and verified revision increments.
- Uploaded a synthetic TXT attachment through the file picker and saved a Sheets reference URL without fetching it.
  The stored resource and file download control remained after restarting the fixture server.
- Missing model consent blocked generation. Two drafts appeared without creating board tasks.
- Missing review confirmation blocked task creation. Reviewed drafts created tasks; the second was reassigned
  to the validator and retained its dependency. Manual task creation also worked.
- Browser QA caught blank textarea prefills in draft review and resource edit. Fixed to use textarea content,
  added regression checks, and verified descriptions, acceptance checks and notes survive edit/reload.
- Resource changes marked saved plans stale. Previously created tasks remained linked from their draft cards.
- Project tabs use native buttons with existing styling. Desktop inspected; at the narrow test width,
  content width equaled the measured viewport width (433 CSS pixels). No horizontal overflow was observed.
- Overview routes uncertain requests to Execution recovery, not Approve/Reject. A synthetic recovery submission
  cleared the hold without invoking an agent. The tested page reported no browser errors.
- Separately, one real Claude CLI planning call used a fictional project: two valid drafts, zero created tasks,
  zero approval requests and 1,474 reported billable tokens. No external messages or cloud documents were accessed.

Before release, test the deployment's non-owner accounts and exercise the selected credential backend with
owner-provisioned test credentials. HTTP regression tests cover the resource/plan role gates, including clients.
Configured credentials are not evidence of a successful provider connection.
