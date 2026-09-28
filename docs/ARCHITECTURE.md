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
| `slack.py` | Slack transport: one app (Socket Mode), everyone posts as themselves, channel ↔ room mapping, approval buttons |
| `commands.py` | Owner commands, shared by Telegram and `jm chat` |
| `llm/` | `claude-code` and `codex-cli` (subscriptions, via their CLIs), `anthropic`, `openai` (any OpenAI-compatible API), `fake`. Per person: `team[].llm` |
| `setup.py` | `jm init` wizard, `jm add-member`, persona import |

## The action protocol

Every model reply is one JSON object: `{"reply": "...", "actions": [...]}`. The runtime checks and applies
each action:

`create_task` · `update_task` · `write_file` (docs/ only) · `read_file` (docs, tasks, reports, team, asks, decisions; up to 2 read rounds) · `ask_permission` · `remember` · `message_agent` · `notify_owner` · `post_group` · `post_room` · `run_code`

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
