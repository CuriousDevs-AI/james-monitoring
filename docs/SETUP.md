# Setup guide

About 15 minutes for a team of 3–5 people. You need: a machine that stays on (a small VPS, or your laptop to
try it out), Python 3.10+, git, a Telegram account, and a model: **a Claude Pro/Max subscription**
(install the CLI with `npm install -g @anthropic-ai/claude-code`, run `claude` once and `/login`), an API key, or Ollama.

## 1. Install

```bash
git clone https://github.com/CuriousDevs-AI/james-monitoring.git /opt/james-monitoring
cd /opt/james-monitoring && python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[all]"
mkdir -p /opt/my-team && cd /opt/my-team
```

## 2. Have these ready

- **Personas:** one markdown file per person describing who they are and what they own. A Claude `SKILL.md`,
  or a folder that contains one, works as-is: its front matter is stripped. If you have none, the wizard generates a starter persona.
- **Bots:** you create one Telegram bot per person with **@BotFather** (`/newbot`). The wizard asks for each
  token at the right moment, so you can create them as you go.

## 3. Set up (browser)

The easiest way: in an empty folder run `jm run`. The browser opens the setup screen; after it, add people on the
**Team** page and (optionally) connect Telegram in **Settings**. The terminal wizard below does the same.

### Or: the terminal wizard

```bash
jm init
```

| Step | You do | The wizard verifies |
|---|---|---|
| 1 Basics | company, your name, timezone, goals | |
| 2 AI | `claude-code` (subscription) or an API provider, model, key | the `claude` CLI is installed |
| 3 Workspace | folder, or a git URL to clone | creates or clones the repo |
| 4 Manager | name (default James), role, persona, **bot token** | the token works |
| | press **Start** on the manager bot | **detects your Telegram id** (you confirm) |
| | create a group, add the manager bot, send a message | **detects the group** (you confirm) |
| | BotFather → `/setprivacy` → Disable, for the manager bot | the manager can read the group |
| 5 Each member | name → role → **persona/SKILL.md path** → projects → **bot token** | the token works (a bad one is re-asked) |
| | add the member's bot to the group | **bot is in the group** ✓ |
| | press **Start** on the member's bot | **DM works** ✓: you get "✅ <name> here" |
| | | the member posts "👋 joined the team" in the group |
| 6 Monitoring | report time, work sessions, budget, coding tool | |

If something isn't detected in time, you can keep waiting or skip it. `jm doctor --ping` shows what's still missing.
Everything confirmed is saved as you go, so an interrupted setup loses nothing.

## 4. Go live

```bash
jm doctor --ping     # model, tokens, each bot in the group, privacy mode, workspace
jm run
```

In the group, type `/onboard`: the manager explains how the team works and everyone introduces themselves.
Then assign the first work, e.g. `/assign riya "Rate limiting for the API" P1 due:+3d project:api`.

## 5. Keep it running

```bash
sudo cp /opt/james-monitoring/deploy/james-monitoring.service /etc/systemd/system/
# edit User / paths in it, then:
sudo systemctl daemon-reload && sudo systemctl enable --now james-monitoring
journalctl -u james-monitoring -f
```

Or use Docker: `deploy/docker-compose.yml`.

## 6. Back up and share the workspace

Give the workspace repo a private remote and set `workspace.push: true`. Every task, memory, deliverable,
decision and report is then pushed as it happens. `git pull` it anywhere to read along.

## Changing the team later

The easiest way is the **Team** page in the console (`jm run`).
Drop in persona files (several at once), paste bot tokens, and watch the ✓ checks. The whole skill package is
stored in `team/<id>/skill/`, so the original files can be deleted afterwards.

```bash
jm add-member                 # verified flow for one new person
jm remove-member <id>         # take someone off; their files stay in git history
```

Restart `jm run` afterwards. Edit any persona directly in `team/<id>/persona.md`; changes apply on the next message.

## Your open decisions

Put anything waiting on you in `decisions/OPEN.md` as a numbered list. Every report shows it under
"Needs <you>" until you remove the line (or strike it through with `~~…~~`).
