# Architecture

Nothing is specific to one company: names, roles, personas, projects, the manager's name, timezone and
model all come from `config.yaml` and the workspace.

## Principles

1. **Git is the truth.** Tasks, memory, decisions and reports are markdown files in a git repo. Telegram is
   only the interface. If something isn't in git, it didn't happen.
2. **Models are replaceable workers.** Every prompt is built from the files, so a new model gets the same team.
3. **Rooms, not channels.** All hands = everyone, a project room = its team, a 1:1 = one person. A room is the
   same in the console, Telegram and Slack; the transport is only how you reach it.
4. **Nothing gets lost in chat.** When the owner corrects someone, the correction is written to the task's
   Feedback and to that person's memory.
5. **The team never waits silently.** Every ask has a deadline and a default. 🔴 asks wait for you, and the
   daily report shows them.
6. **Reports come from code, not from a model.** Monitoring can't hallucinate.
7. **Shared context.** An agent's prompt includes the recent messages of the room it's answering in (everyone's,
   from every channel) and a glance at its other rooms, so a project's people know what was said there.

## Modules

| Module | Role |
|---|---|
| `config.py` | `config.yaml` + `.env` → typed config (times, permissions and per-person models are validated) |
| `fileio.py` | Safe writes: atomic replace, one lock per file across threads and processes, validated config with `.bak`, `.env` add-or-replace |
| `workspace.py` | Git repo layout, commits, memory, logs, runtime state (`.jm/state.json`, not committed) |
| `mdoc.py` | Markdown with YAML front matter + `## Sections` |
| `tasks.py` | Task board and the rules (P0, WIP, done-means, reviewer, blocked-on) |
| `asks.py` | Permission requests with level, deadline, default, outcome |
| `prompts.py` | System prompt = charter + persona + memory + roster + context + action protocol |
| `chat.py` | Rooms (`team`, `p-<project>`, `<member>`, `backchannel`): one JSONL record per room in `.jm/chat/` |
| `hub.py` | Every message goes through here: records it, mirrors it to every channel, routes it to the right people. Also the runtime's Bus |
| `runtime.py` | Dispatch, model call, action application, agent inbox with hop limit, budget, heartbeat, decisions |
| `executor.py` | Coding CLI in a git worktree on `jm/<task>`, merge after approval |
| `runtime.run_work_session` | Scheduled work: each member with todo/doing tasks moves the top one; digest to the owner |
| `monitor.py` | Deterministic report and checks (overdue, blocked too long, asks past deadline, failures) |
| `router.py` | Who answers: `@all`, `@name`, `@bot_username`; project rooms default to the lead, All hands to the manager |
| `gateway.py` | Telegram transport: one bot per member, the manager reads groups, approval buttons |
| `github.py` | Task board mirrored into a GitHub Project (issues + fields) both ways through the rules, and PRs for code tasks — via `gh`, as the owner |
| `slack.py` | Slack transport: one app (Socket Mode), everyone posts as themselves, channel ↔ room mapping, approval buttons |
| `commands.py` | Owner commands, shared by Telegram and `jm chat` |
| `llm/` | `claude-code` and `codex-cli` (subscriptions, via their CLIs), `anthropic`, `openai` (any OpenAI-compatible API), `fake`. Per person: `team[].llm` |
| `setup.py` | `jm init` wizard, `jm add-member`, persona import |
| `users.py` | Sign-in links and roles (owner, admin, member, viewer, client); only SHA-256 hashes of the keys are stored |
| `audit.py` | The audit log in the team repo (`audit/YYYY-MM.jsonl`): console actions per person, decisions from every channel, agent actions, model failures, stuck turns. Secrets never written |
| `search.py` | Full-text search over every room, task, document and memory entry — only what the viewer may see |
| `assistant.py` | Personal assistant mode: reminders (`remind` action), the morning brief |
| `studio.py` | Agent Studio: role templates, skills, the persona form (and back) |

## The action protocol

Every model reply is one JSON object: `{"reply": "...", "actions": [...]}`. The runtime checks and applies
each action:

`create_task` · `update_task` · `write_file` (docs/ only) · `read_file` (docs, tasks, reports, team, asks, decisions; up to 2 read rounds) · `ask_permission` · `remember` · `message_agent` · `notify_owner` · `post_group` · `post_room` · `run_code` · `remind` (the personal assistant only)

Each action is normalised (wrong types fixed where possible) and applied on its own: one bad action is reported
(⚠️) and the rest still run. If any failed, the agent gets **one repair round** with the exact results. Results are
kept in its history, so it never believes something happened that didn't. A reply that isn't JSON, or was cut off at
the output limit, gets one retry that says what went wrong. Long documents go *after* the JSON in
`<<<FILE docs/…>>> … <<<END>>>` blocks, so they can't break it.

History is kept per person **per room**: approval follow-ups, work-session notes and your DMs are one thread.

Before an action runs, its permission level is checked (`permissions` in config, overridable per person):
🟢 runs; 🟡 runs and the owner gets an FYI; 🔴 becomes an ask of kind `action` holding the action itself. When the
owner approves (console, Telegram or Slack), the runtime runs that action for the agent. Rejected actions never run.

`message_agent` hand-offs (and the reply) are recorded in the project's room, or in the backchannel when no
project is involved, as `internal` messages. The owner can see how the team coordinates.

## Why agents don't talk through Telegram

Telegram bots never receive messages sent by other bots. So agent-to-agent messages go through the runtime:
they're logged to `team/<id>/log.md`, visible with `/log`, and capped by `limits.max_agent_hops`.

## Reliability

- **config.yaml**: every change is read-modify-validate-write under one lock, with a `.bak`. A reload builds the new
  runtime first and swaps it in one step. The old one's work in flight continues, sharing the per-person locks.
- **git**: one lock per repo (threads and processes), no prompts (`GIT_TERMINAL_PROMPT=0`, SSH BatchMode), no
  signing, timeouts, and pushes in the background. A failed commit is recorded and alerted; it never breaks a reply.
- **Merges** run before an approval counts. A failed merge is rolled back (`merge --abort`) and the request stays open.
- **Agents read files** only under docs/tasks/reports/team/asks/decisions, checked on the resolved path. Nothing under
  `.git`, `.jm` or any `.env` is readable.
- **Telegram**: edits never re-trigger work, messages are handled once, 429s are waited out, supergroup upgrades are
  followed (and saved), and a bot that fails to start never leaves others polling.
- `.jm/` (chats, conversation memory, state) is zipped daily into `.jm/backups/` (last 14).
- **Model CLIs are sandboxed.** Each call runs in an empty folder with a minimal environment: no API keys, bot
  tokens or other secrets. Timeouts kill the whole process tree.
  - `claude`: no tools, no MCP servers, no user settings or hooks.
  - `codex`: shell, browser, apps, MCP and user config are off, so it can't read files.
  - `opencode`: empty isolated config (no global MCP servers or plugins); paid models get all tools off and every
    permission denied. Free models are opt-in (`allow_free`) and use a throwaway home.
- **One turn is bounded** (7 minutes of model calls), so nobody's lock is held for long. Pause means paused for
  everything, including system follow-ups, which are queued.
- **Only the founder's own words become pinned corrections** in memory. A teammate's message, a document or a
  GitHub comment can't make itself binding.
- **Temporary model failures are retried** (rate limit, overload, network: after 3s, then 10s, within the turn's
  time). Login, key and model errors aren't retried; they're shown with the fix.
- **A hung turn is stopped** by the hub's watchdog (turn limit + 2 minutes). The person says so in the room, the
  lock is released, and it shows in Activity & health.
- **Activity & health** (console) checks the engine, scheduler, git, disk, models, channels, replies in progress,
  the queue and overdue approvals.

## People, roles and privacy

The founder opens the console with the key `jm run` prints. Everyone else gets a personal link from
Settings → Sign-in links; each has a role:

| Role | Can |
|---|---|
| admin | everything except making sign-in links (settings, people, models, approvals) |
| member | read; talk in All hands and project rooms; create and move tasks (not accept or cut) |
| viewer | read only |
| client | only the client portal: their projects' progress and a client-safe report |

Every API route declares the permission it needs (`GET_PERMS` / `POST_PERMS` in `server.py`); anything else is
refused. Messages from signed-in people are recorded as them, and agents are told they're a colleague, not the
founder. Only the founder runs commands or decides approvals in chat.

Private by design: the founder's 1:1 rooms (admins can see them), and everything of the **personal assistant**
(`assistant: true`): its room, its tasks, its reminders. It's left out of @all, the roster, the company report and
teammates' `message_agent`.

**Departments** (`departments:`, `team[].department`) group people with a head; `@engineering` reaches a whole
department in All hands; the report has a section per department. **Clients** (`clients:`, `projects.<id>.client`)
get a report built from the files (only their projects, no internal names, no chat) and, if you want, a portal
login.

## Project settings

A project can have its own model, rules and instructions, for everyone on it or per person. These apply when
someone works on that project: in its room, on its tasks and in work sessions.

```yaml
projects:
  website:
    llm: {provider: opencode, model: zai/glm-4.6}      # everyone on this project
    permissions: {write_file: red}                      # the project's rules
    instructions: "Client is Globex. British spelling."
    agents:
      riya: {role: Tech reviewer, instructions: "Review every PR within a day.",
             llm: {provider: claude-code}, permissions: {write_file: green}}
```

The most specific setting wins: this person on this project, then the project, then the person's own, then the
company's. The project's Settings tab shows who ends up with what, and where each value comes from. Project models
are checked by the AI setup check and health, and sessions reset when the model changes.

## Threads

A reply keeps `reply_to` (the message it answers), `thread` (the thread's first message) and a short quote. Whoever
you reply to answers (unless you @mention someone), inside the thread, and the agent sees the quote. Telegram
swipe-replies bring their quote in; Telegram and Slack show a `↩ quote` line on replies.

## One conversation, every channel

```
console ──┐                          ┌── console (reads .jm/chat)
Telegram ─┼─► hub.receive(room) ─────┼── Telegram (skips what it sent)
Slack ────┘   hub.handle → route ───►└── Slack
                     │
                     ▼ room_targets → runtime.dispatch(member, Event(room=…)) → hub.post(room, reply)
```

`sync.mirror_owner` (default on) also shows the owner's own messages in the other channels ("🗨 Pankaj (via console): …").

## Data flow: "Riya, add rate limiting to the API"

1. Riya's bot receives your DM → `runtime.dispatch("riya", Event("dm", ...))`.
2. The prompt is built from the charter, Riya's persona and memory, her tasks and the projects → model call.
3. The model returns `create_task` + `run_code` → a task file is written and committed. The executor starts in the background.
4. The executor runs the coding CLI in `../.jm-worktrees/api-T-012` on branch `jm/T-012` and commits there.
5. A merge ask (🔴) is created → you get a card with the diffstat and ✅/❌ buttons.
6. You tap ✅ → `git merge --no-ff jm/T-012` on main (only if main is clean) → the task is done → Riya is told
   and replies to you.
