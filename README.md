# james-monitoring

Run a team of AI agents the way you'd run a real team. Any founder, any startup, any number of people.

- **One conversation, every channel.** The web console, Telegram and Slack show the same rooms: All hands, a room
  per project, and a 1:1 with each person. Start a thread on your laptop, continue it on your phone.
- **A team that shares context.** Everyone on a project reads that project's room, including what teammates said
  to each other. They don't ask for what someone already said.
- **A manager (James by default, any name you like)** who watches the board, chases blockers and reports to you daily.
- **You stay in the loop.** Risky things arrive as Approve/Reject cards, and you choose what else needs approval,
  per action and per person. Nothing approves itself.
- **Everything lives in git:** personas, tasks, memory, deliverables, decisions, reports.
- **Any model, per person:** a **Claude subscription** (`claude` CLI), a **ChatGPT/Codex subscription** (`codex` CLI),
  the Claude or OpenAI APIs, or a local model through Ollama. Riya can run on Codex while Omar runs on Claude.

```
You — web console · Telegram · Slack (all in sync)
 ├── All hands ....... announcements · @all give status · !pause all · the daily report
 ├── project rooms ... #api: @all = the project's team · no mention = the lead · teammates' hand-offs visible
 ├── 1:1 per person .. instructions · corrections · ✅/❌ approval cards
 └── backchannel ..... teammates talking to each other outside a project (read-only)
        │
        ▼
 james-monitoring (one process on any server)
 ├── hub ............ one record per room → mirrored to every channel; routes @all · @name · rooms → people
 ├── runtime ........ prompt = charter + persona + memory + board + the room's recent chat → model → actions
 ├── rules .......... 1 P0/person · WIP ≤ 2 · "Done means" before start · only the reviewer marks done
 ├── permissions .... per action and per person: 🟢 do · 🟡 do & tell · 🔴 ask first (runs only after you approve)
 ├── work sessions .. scheduled: everyone moves their top task and saves real output to docs/
 ├── manager ........ hourly checks · alerts · daily report (built from files, not by a model)
 └── models ......... per person: claude-code · codex-cli (subscriptions) · anthropic · OpenAI-compatible · Ollama
        │
        ▼
 git workspace: team/charter.md · team/<id>/{persona,memory,log}.md · tasks/ · asks/ · docs/ · decisions/ · reports/
```

## Start in 2 minutes

```bash
git clone https://github.com/CuriousDevs-AI/james-monitoring.git
cd james-monitoring && python -m venv .venv && . .venv/bin/activate && pip install -e ".[all]"
mkdir -p ../my-company && cd ../my-company
jm run          # opens the console in your browser
```

**Everything happens in the console**, on your own machine at `localhost`, protected by a key in the link:

| Page | What you do there |
|---|---|
| **Setup** | First run only: company, you, timezone, goals, AI model (a Claude subscription works), team repo, manager's name |
| **Overview** | Open / review / blocked / overdue counts, **Needs you** (approvals, reviews, decisions), project progress, critical path, people, token use, recent activity |
| **Projects** | The heart of it. Each project has a brief, a lead, **its own team** (a person can be on several projects), and four tabs: **Overview** (progress, what needs attention, who is on it), **Room** (the project's chat: `@all` reaches only that project's people, no mention goes to the lead), **Board** (that project's kanban) and **Team** (add, remove, make lead). Your projects are listed in the sidebar |
| **Chat** | Project rooms, **All hands**, a private chat with each person, and the **Team backchannel** (read-only). Approve 🔴 requests right in the chat. Telegram and Slack conversations are the same rooms, with a badge showing where each message came from |
| **All tasks** | The company-wide kanban (To do · Doing · Blocked · Review · Done), filter by project or person, drag to change status, open a task to accept, give feedback, change owner, priority, due date |
| **People** | Drop in all the `.skill` / `SKILL.md` files at once; names and roles fill in. Telegram bots are optional, with live ✓ checks. Edit roles and personas, **pick each person's AI model**, set what they may do on their own, see memory and activity, pause people |
| **Approvals** | Every permission request and its history |
| **Reports** | Daily reports, and your open-decisions list (it appears in every report) |
| **Settings** | Company, default AI model, **permissions**, sync, schedule (report time, work sessions), budget, coding tool, Telegram and **Slack** connections |

**Telegram and Slack are optional.** Connect either or both in Settings to run the team from your phone or your
workspace. Telegram is a group plus one bot per person; Slack is one app where everyone posts under their own name
([docs/SLACK.md](docs/SLACK.md)). All channels stay in sync with the console: your messages, their replies and approvals.

Terminal alternatives still exist: `jm init` (setup wizard), `jm add-member`, `jm chat <who|team|p-project>`, `jm status`,
`jm report`, `jm work`, `jm doctor --ping`, and `jm run --no-web` (Telegram + Slack + schedule, no console).
`jm chat` conversations are saved like any other, so they show up in the console too.

## Day to day

| You do | What happens |
|---|---|
| DM the manager: *"what's the delivery status of the API?"* | The answer comes from the board, recent commits and open decisions. The manager doesn't answer from memory. |
| In the group: `@all give status` | Each member's bot replies with one line. This comes straight from the board: instant, and no model call. |
| In the group: `@riya @omar sync on the login flow` | Those two reply in the group. |
| DM Riya: *"add rate limiting"* | Riya creates a task, codes it on branch `jm/T-012`, and sends you a 🔴 **Merge?** card. You tap ✅ and it's merged. |
| DM Riya: *"T-012 not like this — per-user limits"* | The correction is saved on the task **and** in Riya's memory. |
| Someone needs to spend money | You get a 🔴 card with the cost, the reason and the manager's recommendation. It never auto-approves. |
| Work sessions (e.g. 10:00 and 15:00, Mon–Sat) | Everyone with open work moves it forward and writes the real output to `docs/`. You get a one-line-per-person digest. |
| Daily report time | The report is posted in the group and committed to `reports/`, including your open decisions (`decisions/OPEN.md`). |
| `/pause all` · `/resume all` | Everyone stops or starts. |

### Try it without Telegram

```bash
jm chat james "what's blocked?"
jm chat riya                       # interactive; /commands work too
jm status && jm report && jm work
```

## Commands

| Command | Does |
|---|---|
| `/status` · `/board` | Everyone's status · all tasks by state |
| `/assign <who> "title" P1 due:10-03 project:<id>` | Creates the task. That person acknowledges in your DM. |
| `/accept T-012 [note]` · `/feedback T-012 text` · `/cut T-012` | Review, feedback (saved to the task and to memory), cut |
| `/asks` · `/approve ASK-3` · `/reject ASK-3` | Permission requests (the buttons do the same) |
| `/pause`, `/resume` (in a member's DM) · `/pause all`, `/resume all` | One person · everyone |
| `/work` · `/report` · `/onboard` · `/log <who>` · `/budget` | Work session now · report now · intros · activity · token use |
| `/whoami` · `/groupid` | Your id · this chat's id |

## Rules the system enforces

- **Priorities:** at most one P0 per person. Only the owner can override.
- **Work in progress:** at most 2 tasks in `doing` per person, and a task can't start without "Done means".
- **Done means reviewed:** agents move work to `review`. Only the reviewer (you, by default) marks it `done`.
  **Request changes** (or feedback on work in review) sends it back to `doing`; they're told at once and rework it first.
- **Blocked needs a reason:** `blocked` must name the person and the exact thing needed. That person is told at
  once, unblocking others comes first in their next work session, and a task blocked on `T-012` goes back to
  `todo` when T-012 is done.
- **Own tasks only:** agents change only their own tasks. On a teammate's task they can add a log note or message them.
  Only you can cut a task.
- **Answers come back:** when one agent asks another, the reply goes back to whoever asked, and what they do with it
  is said where the conversation started.
- **Nothing is lost:** messages to a paused or over-budget person wait in a queue and are answered when they can work again.
  Your corrections are pinned in their memory and never trimmed.
- **Code:** changes happen only on `jm/<task>` branches. main changes only after you approve.
- **🔴 asks** (money, public, production, deleting, legal, new vendors) never auto-decide.
- **Permissions you set:** in Settings (and per person), each action (create tasks, save documents, message
  teammates, post in All hands or a project room, run the coding agent) is 🟢 *just do it*, 🟡 *do it and tell
  me*, or 🔴 *ask me first*. A 🔴 action becomes an Approve/Reject card and runs only after you approve it.
- **Loops:** agent-to-agent conversations stop after `max_agent_hops` and escalate to you.
- **The manager manages:** James does a round after every work session, and the hourly checks nudge whoever
  owns late, stale or long-blocked work. You get alerts for reviews waiting too long, idle people and failing models.
- **Budget:** a daily token budget per person. You get a warning at 80%, and they pause at 100%.
- **Honest reports:** reports and checks are generated by code from the files, so they can't invent progress.
- **Access:** only your Telegram id (plus any `extra_user_ids`) can command the team.

## Switching models

```yaml
llm: { provider: claude-code, model: sonnet }        # your Claude subscription via the `claude` CLI, no API key
llm: { provider: codex-cli }                         # your ChatGPT subscription via the `codex` CLI, no API key
llm: { provider: anthropic, model: <claude model id>, api_key_env: ANTHROPIC_API_KEY }
llm: { provider: openai,    model: <gpt model id>, api_key_env: OPENAI_API_KEY }
llm: { provider: openai,    model: llama3.1, base_url: http://localhost:11434/v1, api_key_env: "" }   # Ollama
```

That's the company default. Anyone can have their own, in their profile or in `config.yaml`:

```yaml
team:
  - { id: riya, name: Riya, role: Backend lead, llm: { provider: codex-cli } }
  - { id: omar, name: Omar, role: Designer,     llm: { provider: claude-code, model: opus } }
```

The coding tool is just as swappable (`executor.command`): Claude Code, Codex CLI, Aider, anything that edits files.

## Deploy (always on)

- **Docker:** see `deploy/docker-compose.yml`. Mount a folder holding `config.yaml`, `.env` and the workspace.
- **systemd:** see `deploy/james-monitoring.service`.

Telegram uses long polling and Slack uses Socket Mode, so you don't need a domain or open ports. A small VPS is enough.

Full guide: [docs/SETUP.md](docs/SETUP.md) · Internals: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Development

```bash
pip install -e ".[dev]" && pytest -q
```

The tests include a fake Telegram Bot API server. They drive the real python-telegram-bot code and the full
`jm init` flow end to end.

## License

MIT
