# Workforce operating-system delivery checklist

James is a general-purpose, self-hosted workforce tool. CuriousDevs marketing and lead-generation teams are
example deployments, not hard-coded product behaviour. A passing fake integration test is not proof that a
live account, a free provider, or a production deployment works.

## Release acceptance

- [x] Configurable company, projects, personas/skills and per-person/project model assignment.
- [x] Console, Telegram/Slack transports, human approval and GitHub task-board integration exist.
- [x] Private-assistant task/file boundaries and inbound GitHub proposal review have regression tests.
- [x] Dependencies block work until accepted completion; one-off requests can remain separate from daily work.
- [x] Opt-in native credential storage, owner-only metadata/save API and unavailable-store handling.
- [ ] Owner-facing Secrets Settings screen; no reveal, explicit status checks, restart guidance.
- [ ] Durable approval execution attempts and human reconciliation after uncertain outcomes; no blind replay.
- [ ] Recovery drill from another directory/machine with configuration, tasks, private runtime state and
      separately re-provisioned credentials. GitHub Projects alone is not a full backup.
- [ ] Scheduled-run history distinguishes scheduled, running, completed, interrupted and failed work.
- [ ] Explicit, opt-in model fallback policy with budget/privacy boundaries; never silently send private
      context to a different provider or treat a free tier as guaranteed capacity.
- [ ] Per-agent/project connector grants enforced by the executor, not merely by a persona prompt.
- [ ] Data-source and outbound-connector contracts: collect -> validate -> draft -> approve exact content and
      recipient -> send -> record result. Provider/account access and business scope must be supplied.
- [ ] Safe self-improvement loop: proposed change, isolated tests, reviewable PR; no autonomous policy edits,
      credential expansion, self-approval or production deployment.
- [ ] Live acceptance with the owner's test accounts and explicit approved destinations.

## Evidence required for the example workflow

Create a generic project with two workers. The researcher produces sourced candidate records; the validator
checks scope and duplication, writes a draft, and requests human approval. A rejected draft is never sent.
An approved send refers to immutable reviewed content and destination. A timeout after sending is marked
uncertain and reconciled against the provider, not retried blindly. Restarting never loses the outcome.
An independent DM asking for two candidates does not change the recurring job. Both workers' reports are
visible in the chosen project room while private assistant data remains excluded.

Use synthetic sources and outbound sinks in automated tests. Do not enable real scraping, email or LinkedIn
sending merely because a connector is installed. Respect source terms, account permissions, recipient scope
and applicable outreach rules. Credentials are provisioned locally, never committed in examples or PRs.

## Review and delivery rules

- Small, tested PRs; existing changes are not overwritten. Source commits use the configured human identity.
- No automatic merging, production deployments, purchases, credential migration or live outreach as part
  of a code-review checkpoint.
- Record known limits in the relevant feature document. Do not tick an item until its acceptance behaviour
  actually exists and has been exercised.
- Integration credentials, deployment platform, outreach scope and approved test destinations are external
  prerequisites. Missing access does not prevent local implementation, but it does prevent live sign-off.
