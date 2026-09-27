# Architecture

## Principles

1. **Git is the truth.** Tasks, memory, decisions and reports are markdown files in a git repo. Telegram is
   only the interface. If something isn't in git, it didn't happen.
2. **Models are replaceable workers.** Every prompt is built from the files, so a new model gets the same team.
3. **Group = everyone, DM = one person.** Instructions to one person never go in the group.
4. **Nothing gets lost in chat.** When the owner corrects someone, the correction is written to the task's
   Feedback and to that person's memory.
5. **The team never waits silently.** Every ask has a deadline and a default. 🔴 asks wait for you, and the
   daily report shows them.
6. **Reports come from code, not from a model.** Monitoring can't hallucinate.

## Modules

| Module | Role |
|---|---|
| `config.py` | `config.yaml` + `.env` → typed config |
| `workspace.py` | Git repo layout, commits, memory, logs, runtime state (`.jm/state.json`, not committed) |
| `mdoc.py` | Markdown with YAML front matter + `## Sections` |
| `tasks.py` | Task board and the rules (P0, WIP, done-means, reviewer, blocked-on) |
| `asks.py` | Permission requests with level, deadline, default, outcome |
| `prompts.py` | System prompt = charter + persona + memory + roster + context + action protocol |
| `runtime.py` | Dispatch, model call, action application, agent inbox with hop limit, budget, heartbeat, decisions |
| `executor.py` | Coding CLI in a git worktree on `jm/<task>`, merge after approval |
| `runtime.run_work_session` | Scheduled work: each member with todo/doing tasks moves the top one; digest to the owner |
| `monitor.py` | Deterministic report and checks (overdue, blocked too long, asks past deadline, failures) |
| `router.py` | Group routing: `@all`, `@name`, `@bot_username`, default James |
| `gateway.py` | Telegram: one bot per member, James reads the group, approval buttons, scheduled jobs |
| `commands.py` | Owner commands, shared by Telegram and `jm chat` |
| `llm/` | `anthropic`, `openai` (any OpenAI-compatible API), `fake` |
| `setup.py` | `jm init` wizard, `jm add-member`, persona import |

## The action protocol

Every model reply is one JSON object: `{"reply": "...", "actions": [...]}`. The runtime checks and applies
each action:

`create_task` · `update_task` · `write_file` (docs/ only) · `read_file` (docs, tasks, reports, team, asks, decisions; up to 2 read rounds) · `ask_permission` · `remember` · `message_agent` · `notify_owner` · `post_group` · `run_code`

Actions that break a rule are refused, and the reason is shown in the reply (⚠️). A bad action never crashes
the run. A reply that isn't JSON is sent as plain text.

## Why agents don't talk through Telegram

Telegram bots never receive messages sent by other bots. So agent-to-agent messages go through the runtime:
they're logged to `team/<id>/log.md`, visible with `/log`, and capped by `limits.max_agent_hops`.

## Data flow: "Sofia, change the navbar"

1. Sofia's bot receives your DM → `runtime.dispatch("sofia", Event("dm", ...))`.
2. The prompt is built from the charter, Sofia's persona and memory, her tasks and the projects → model call.
3. The model returns `create_task` + `run_code` → a task file is written and committed. The executor starts in the background.
4. The executor runs the coding CLI in `../.jm-worktrees/site-T-012` on branch `jm/T-012` and commits there.
5. A merge ask (🔴) is created → you get a card with the diffstat and ✅/❌ buttons.
6. You tap ✅ → `git merge --no-ff jm/T-012` on main (only if main is clean) → the task is done → Sofia is told
   and replies to you.
