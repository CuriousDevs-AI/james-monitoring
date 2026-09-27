# james-monitoring

Run an AI team the way you'd run a real team: a Telegram HQ group, a 1:1 chat with every team member,
tasks and memory in git, permissions before anything risky, and **James**, a delivery manager who
monitors all of it and reports to you.

It works with any model. The team lives in plain markdown in a git repo, so switching from Claude to
GPT, Codex or a local Llama is a one-line config change. Nothing is lost when you switch.

```
You (Telegram)
 ├── HQ group ........ announcements · @all status · /pause all · big news · daily report
 ├── DM James ........ "what's the delivery status of Ojas?" · /board · /assign · /asks
 ├── DM Sofia ........ "change the navbar" → she works on a branch → asks you → you tap Approve → merged
 └── DM Alex, Daniel … instructions, corrections, permission requests
        │
        ▼
 james-monitoring (one process on your server)
 ├── router ......... @all · @name · DMs → the right agent
 ├── runtime ........ prompt = charter + persona + memory + board → model → actions
 ├── rules .......... 1 P0/person · WIP ≤ 2 · "Done means" before start · only reviewer marks done
 ├── permissions .... 🟢 do · 🟡 do & tell · 🔴 ask first (Approve/Reject buttons, deadline, default)
 ├── James .......... hourly checks · overdue/blocked/stale alerts · daily report · loop & budget guards
 └── model adapter .. anthropic | openai-compatible (OpenAI, Codex, Ollama, OpenRouter, vLLM)
        │
        ▼
 git workspace: team/charter.md · team/<id>/{persona,memory,log}.md · tasks/ · asks/ · reports/
```

## What it does

| You do | What happens |
|---|---|
| DM James: *"what's the delivery status of Ojas?"* | James answers from the board, recent commits and pending asks. He doesn't answer from memory. |
| In the group: `@all give status` | Every member's bot replies with one line (status · task · blocker). This comes straight from the board: no model call, instant and free. |
| In the group: `@all start working on the new plan` | Every member reads it and replies in the group. |
| DM Sofia: *"make the navbar sticky"* | Sofia creates a task, runs a coding agent on branch `jm/T-012`, and sends you a 🔴 **Merge?** card. You tap ✅ and it's merged into main. |
| DM Sofia: *"T-012 not like this — use brand blue"* | The correction is saved to T-012's Feedback **and** to Sofia's memory, so she won't make that mistake again. |
| Alex needs ₹45k for a Jetson | Alex sends you a 🔴 card with the cost, the reason and James's recommendation. A 🔴 request never auto-approves. |
| `/pause all` in the group | Everyone stops. `/resume all` starts them again. |
| Every day at 18:54 | James posts the status report in the group and commits it to `reports/`. |

## Quick start

```bash
git clone https://github.com/CuriousDevs-AI/james-monitoring.git
cd james-monitoring
python -m venv .venv && . .venv/bin/activate
pip install -e ".[all]"

mkdir -p data && cd data
jm init            # wizard: AI → company → team → git workspace → Telegram → monitoring
jm doctor --ping   # checks model, bots, repo
jm run             # the team is live
```

Then, in Telegram:
1. Press **Start** on every bot, so each one is allowed to DM you.
2. Add all the bots to your HQ group.
3. Type `/onboard` in the group. Everyone introduces themselves.

Telegram step by step (BotFather, privacy mode, ids): [docs/SETUP.md](docs/SETUP.md).

### Try it without Telegram

```bash
jm chat james "what's blocked?"
jm chat sofia                       # interactive; /commands work too
jm chat james '/assign sofia "Hero section" P1 due:10-03'
jm status && jm report
```

## Commands

| Command | Where | Does |
|---|---|---|
| `/status` | anywhere | Everyone's status, taken from the board |
| `/board` | anywhere | All tasks by state |
| `/assign sofia "title" P1 due:10-03 project:site` | James DM / group | Creates the task. Sofia acknowledges in your DM. |
| `/accept T-012 [note]` | anywhere | Reviewer accepts, and the task is done |
| `/feedback T-012 text` | anywhere | Saved on the task and in the owner's memory |
| `/cut T-012 [reason]` | anywhere | Task cut |
| `/asks`, `/approve ASK-3 [note]`, `/reject ASK-3 [note]` | anywhere | Permission requests (buttons also work) |
| `/pause`, `/resume` | a member's DM | That member only |
| `/pause all`, `/resume all` (`/stop all`, `/start all`) | group / James DM | Everyone |
| `/log alex` · `/budget` · `/report` · `/onboard` | anywhere | Activity · token use · report now · intros |
| `/whoami` · `/groupid` | anywhere | IDs you need during setup |

## Rules the system enforces

- **Priorities:** at most one P0 per person. Only the founder can override.
- **Work in progress:** at most 2 tasks in `doing` per person. A task can't start without "Done means" (acceptance checks).
- **Done means reviewed:** agents move work to `review`. Only the reviewer (you, by default) marks it `done`.
- **Blocked needs a reason:** `blocked` must name the person and the exact thing needed.
- **Code:** changes always happen on a `jm/<task>` branch. main changes only after you approve the merge.
- **🔴 asks** (money, public, production, deleting, legal, new vendors) never auto-decide. 🟡 asks can have a default that applies at the deadline.
- **Loops:** agent-to-agent conversations stop after `max_agent_hops` and escalate to you.
- **Budget:** a daily token budget per agent. You get a warning at 80%, and the agent pauses at 100%.
- **Honest reports:** James's reports and checks are built from the files by code. No model is involved, so they can't invent progress.

## Switching models (so the team survives any vendor)

```yaml
llm: { provider: anthropic, model: <claude model id>, api_key_env: ANTHROPIC_API_KEY }
llm: { provider: openai,    model: <gpt/codex model id>, api_key_env: OPENAI_API_KEY }
llm: { provider: openai,    model: llama3.1, base_url: http://localhost:11434/v1, api_key_env: "" }   # Ollama
```

The coding hands are just as swappable: set `executor.command` to Claude Code, Codex CLI, Aider, or any
CLI that edits files in a folder.

## Personas

Each member's persona is `team/<id>/persona.md`: plain markdown that you can edit any time. `jm init` and
`jm add-member --persona path/to/SKILL.md` can import existing Claude skill files. The front matter is
stripped automatically.

## Deploy (always on)

- **Docker:** `cd deploy && mkdir data && cp ../examples/config.yaml data/ && docker compose up -d`
- **systemd:** see [`deploy/james-monitoring.service`](deploy/james-monitoring.service)

One small VPS is enough. The process uses long polling, so you don't need a domain or open ports.

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

More detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## License

MIT
