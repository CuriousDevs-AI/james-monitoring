# Owner controls: verification record

The Secrets and execution-recovery controls reuse the existing console components; no new frontend
dependency or external design-source upload is involved.

## Automated evidence

- The initial owner-controls increment passed 310 Python tests, including the Node form-test wrapper.
  The subsequent integration passes 338 Python tests.
- Eight dependency-free Node tests check JavaScript parsing, manual secret-status loading, duplicate-save
  prevention, clearing secret inputs on success/failure, non-disclosure of error details, and evidence,
  outcome and explicit-verification requirements for recovery submissions. Status refresh cannot race a save.
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
- The final inline verification checkboxes render correctly. Browser automation could not toggle/submit
  them reliably after an earlier native-dialog focus problem, so final browser end-to-end submission is
  **not signed off**. Isolated form tests and backend recovery API tests pass; they do not replace this check.

Before release, repeat action reconciliation and schedule acknowledgment in a fresh browser session, test
non-owner access, and exercise the selected credential backend with owner-provisioned test credentials.
Configured credentials are not evidence of a successful provider connection.
