# james-monitoring

Run a team of AI agents the way you'd run a real team. Any founder, any startup, any number of people.

- **A Telegram HQ group** for things everyone needs: announcements, `@all status`, `/pause all`, big news.
- **A 1:1 chat with each team member.** You give instructions and corrections there, and they ask your permission there.
- **A manager (James by default, any name you like)** who watches the board, chases blockers and reports to you daily.
- **Everything lives in git:** personas, tasks, memory, deliverables, decisions, reports.
- **Any model:** Claude, GPT/Codex, or a local model through Ollama. Switching is one line of config, and the team stays the same.

```
You (Telegram)
 ├── HQ group ........ /onboard · @all give status · @riya … · /pause all · daily report
 ├── DM the manager .. "what's blocked?" · "delivery status of the API?" · /board · /assign · /asks
 └── DM each member .. instructions · corrections · ✅/❌ permission cards
        │
        ▼
 james-monitoring (one process on any server)
 ├── router ......... @all · @name · DMs → the right person
 ├── runtime ........ prompt = charter + persona + memory + board → model → actions
 ├── rules .......... 1 P0/person · WIP ≤ 2 · "Done means" before start · only the reviewer marks done
 ├── permissions .... 🟢 do · 🟡 do & tell · 🔴 ask first (buttons, deadline, default)
 ├── work sessions .. scheduled: everyone moves their top task and saves real output to docs/
 ├── manager ........ hourly checks · alerts · daily report (built from files, not by a model)
 └── model adapter .. anthropic | any OpenAI-compatible API
        │
        ▼
 git workspace: team/charter.md · team/<id>/{persona,memory,log}.md · tasks/ · asks/ · docs/ · decisions/ · reports/
```

## Set up a team in about 15 minutes

```bash
git clone https://github.com/CuriousDevs-AI/james-monitoring.git
cd james-monitoring && python -m venv .venv && . .venv/bin/activate && pip install -e ".[all]"
mkdir -p ../my-team && cd ../my-team
jm init
```

`jm init` walks you through it and **checks every Telegram link live**. Nothing is assumed:

1. **Basics:** company, your name, timezone (detected automatically), goals.
2. **AI:** provider, model, key.
3. **Workspace:** a new git repo, or clone an existing one.
4. **Manager bot:** paste its token. The wizard then:
   - **detects you** when you press Start on the bot,
   - **detects your team group** when you add the bot to it,
   - reminds you to switch off privacy mode, so the manager can read the group.
5. **Each team member, one at a time:**
   `name` → `role` → **persona / SKILL.md path** (a file or a folder) → projects → bot token →
   waits until **the bot is in the group ✓** → waits until **you've pressed Start on it ✓** (you get a hello DM) →
   the member introduces themselves in the group.
6. **Monitoring:** daily report time, work sessions, token budget, and the coding tool (Claude Code / Codex / none).

Then:

```bash
jm doctor --ping    # model, every bot, group membership, privacy mode
jm run              # the team is live
```

Growing or shrinking the team later:

```bash
jm add-member                   # the same verified flow, for one person
jm remove-member riya           # off the team; their files stay in git history
```

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
- **Blocked needs a reason:** `blocked` must name the person and the exact thing needed.
- **Code:** changes happen only on `jm/<task>` branches. main changes only after you approve.
- **🔴 asks** (money, public, production, deleting, legal, new vendors) never auto-decide.
- **Loops:** agent-to-agent conversations stop after `max_agent_hops` and escalate to you.
- **Budget:** a daily token budget per person. You get a warning at 80%, and they pause at 100%.
- **Honest reports:** reports and checks are generated by code from the files, so they can't invent progress.
- **Access:** only your Telegram id (plus any `extra_user_ids`) can command the team.

## Switching models

```yaml
llm: { provider: anthropic, model: <claude model id>, api_key_env: ANTHROPIC_API_KEY }
llm: { provider: openai,    model: <gpt/codex model id>, api_key_env: OPENAI_API_KEY }
llm: { provider: openai,    model: llama3.1, base_url: http://localhost:11434/v1, api_key_env: "" }   # Ollama
```

The coding tool is just as swappable (`executor.command`): Claude Code, Codex CLI, Aider, anything that edits files.

## Deploy (always on)

- **Docker:** see `deploy/docker-compose.yml`. Mount a folder holding `config.yaml`, `.env` and the workspace.
- **systemd:** see `deploy/james-monitoring.service`.

It uses long polling, so you don't need a domain or open ports. A small VPS is enough.

Full guide: [docs/SETUP.md](docs/SETUP.md) · Internals: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Development

```bash
pip install -e ".[dev]" && pytest -q
```

The tests include a fake Telegram Bot API server. They drive the real python-telegram-bot code and the full
`jm init` flow end to end.

## License

MIT
