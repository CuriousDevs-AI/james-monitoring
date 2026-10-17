# Project resources and reviewed task planning

## Workflow

1. Create a project, write its goal/brief and assign teammates. Set the model in project Settings.
2. Open Resources. Add a brief, document, dataset, design, artifact, tool or link. Notes explain its purpose,
   approved scope and missing access. Upload a supported file if useful. Never upload credentials.
3. Use Task for manual creation, or AI task drafts to propose up to 12 concise tasks. The planner is the assigned
   lead, otherwise the first assigned non-private teammate. Explicit consent is required for the shown model.
4. Review each draft's title, owner, priority, scope, acceptance checks and deadline. Create prerequisite drafts
   first, then their dependents. The server validates owners and dependencies; it never accepts an unreviewed draft.
5. Created tasks use the existing board, scheduler, permission gates and configured GitHub task mirror. Creation
   from a draft does not immediately message an agent; a later work session may select the task.

Generating drafts is one call through the configured model adapter, not an agent dispatch. Returned actions are
never executed. It respects planner/project pause and the existing reported-token daily budget. There is no
automatic model fallback or automatic regeneration. Failed/invalid responses leave existing drafts intact and
create no tasks. Reported tokens count even when the output is invalid. Provider limits and CLI isolation still
apply; the application is not an OS sandbox.

## What the AI can read

- Project description and instructions: up to 8,000 characters each; planning request up to 2,000 characters.
- Assigned public teammates and their project roles; first 100 shared task summaries to help avoid duplicates.
- Included, non-archived resource notes and text-file excerpts: 20,000 characters total, explicitly truncated.
  Each text file contributes at most its first 20,000 bytes. These are untrusted references, not permissions.
- Ordinary project-context turns also include these resource excerpts, including task-linked DMs. Non-assigned
  agents do not automatically receive them; unrelated DMs have no project resource context.

Supported text: TXT, Markdown, CSV, TSV, JSON, YAML. PDF, DOCX, XLSX, PPTX, PNG, JPG/JPEG and WebP are stored as
opaque downloadable attachments, **not extracted or inspected by the model**. Add notes or an exported text/CSV
version for AI context. Each file is nonempty and at most 2 MB; each project has at most 40 resource records.
Archiving removes a resource from AI context, not history; archived records still count toward the limit.

Google Sheets/Docs, Claude, Figma, repository and other HTTP(S) links are reference URLs only: James neither
fetches them nor inherits their access. A Claude link is not Claude-model configuration. Use Settings for model
authentication and supported connections. External editing, scraping, sheet sync and outbound sending require
separate implemented connectors and permissions. Known credential-bearing query/fragment keys are refused,
but this is not a comprehensive secret scanner: do not attach private signed links or credentials in notes/files.

## Persistence and access

- Resource JSON records (including base64 file bytes) live at `projects/<id>/resources/<uuid>.json` in workspace
  git. Saves are atomic, revision-checked and committed. Archive/restore and detaching/replacing a file are
  recoverable from git history; they do not erase old content. Keep workspace repositories private as needed.
- Owner/admin may manage resources and generate/accept drafts. Console members/viewers can read/download
  resources; clients cannot. Project assignment chooses automatic AI context, not tenant/OS-level isolation.
- Latest uncreated plan lives at `.jm/project-plans/<id>.json`, excluded from git but included by private runtime
  snapshots. A new generation replaces uncreated drafts. Back up both workspace git and runtime state off-machine.
  GitHub Projects mirrors created tasks only; it is not a resource/attachment or runtime backup.
- The task's `source_key` is saved atomically with creation. Retrying acceptance after a lost response returns
  the same task. Resource, team, project instruction/status or model-routing changes invalidate remaining drafts.
  The generation request must match the context signature shown with model consent. Existing task edits do not
  invalidate a plan: humans must still check duplicate work and current scope during review.
- Audit entries include project/resource/draft identifiers, not uploaded bytes, notes or planning prompts.

## API

`GET /api/project/resources?id=site` lists metadata; adding `&resource_id=<uuid>` returns that record with file
data for download. `POST /api/project/resources` accepts `id`, `title`, `kind`, `notes`, `url`, `include` and
optional `filename`/base64 `data`. For edits provide `resource_id` and exact `revision`; `archived` controls
archive/restore and `remove_file: true` detaches a file. A replacement file wins if both are supplied.

`GET /api/project/plan?id=site` returns the plan plus planner/model/signature. To generate, POST
`/api/project/plan/generate` with `{id, instructions, consent: true, signature}`. To accept one draft, POST
`/api/project/plan/accept` with `{id, plan_id, draft_id, reviewed: true, task}`; `task` can contain edited
`title`, `owner`, `priority`, `description`, `done_means`, `due`, `depends_on` (earlier draft IDs).
Omitting `task` uses the saved draft. No endpoint bulk-executes a plan.

Synthetic HTTP, persistence, context, validation, dependency, retry and browser tests cover this workflow.
Live model suitability and actual cloud access still require deployment-specific acceptance.
