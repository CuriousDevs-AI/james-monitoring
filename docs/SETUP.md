# Setup, step by step

About 20 minutes the first time.

## 1. Server

Any Linux box with Python 3.10+ and git: a ₹500–1,000/month VPS, a spare machine, or your laptop for testing.

```bash
git clone https://github.com/CuriousDevs-AI/james-monitoring.git /opt/james-monitoring
cd /opt/james-monitoring && python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[all]"
mkdir data && cd data
```

## 2. Model key

Get an API key for the model you want to use: Anthropic, OpenAI, OpenRouter, or none if you run Ollama.
The wizard saves it to `data/.env` with permissions set to 600. It is never written to config.yaml.

## 3. Telegram bots: one per team member

In Telegram, open **@BotFather**:

1. `/newbot` → name `James (CuriousDevs)` → username e.g. `curiousdevs_james_bot` → copy the token.
2. Repeat for every member: Alex, Ethan, Daniel, Sofia, and so on.
3. **James only:** `/setprivacy` → choose James's bot → **Disable**.
   James is the one bot that reads the group. The others only post there and read their own DMs.
4. Optional: `/setuserpic` to give each bot a face, and `/setdescription` to add its role.

Why one bot per person: it feels like messaging a real teammate, and every reply shows who sent it.

## 4. HQ group

1. Create a Telegram group named **CuriousDevs HQ**.
2. Add all the bots.
3. Optional: make James an admin. He can then pin announcements.

## 5. Run the wizard

```bash
jm init
```

It asks for the following, in order:

1. The AI provider, model and key.
2. The company, founder and goals.
3. The team: how many members, then each one's name, role, projects and persona file. You can import `SKILL.md` files.
4. The git workspace: a new repo, or a clone of an existing one. Optionally, each project's repo, which is needed for code tasks.
5. The bot tokens.
6. The report time, the budget, and the coding CLI (Claude Code, Codex, or none).

Leave your user id and group id at 0 for now. You get them in the next step.

## 6. Get your ids

```bash
jm run
```

- DM any bot `/whoami` to get your user id. Put it in `config.yaml` → `owner.telegram_user_id`.
- In the HQ group, send `/groupid` to James to get the group id. Put it in `telegram.group_chat_id`.
- Restart `jm run`.

Until your user id is set, the bots answer only `/whoami` and `/groupid`, so nobody else can control your team.

## 7. Go live

1. Press **Start** on every bot. Telegram doesn't let a bot DM you until you've done this.
2. `jm doctor --ping` should show all ✅.
3. In the group: `/onboard`. Everyone introduces themselves.
4. In James's DM: `/assign alex "Runtime survey: build vs reuse" P0 due:10-03 project:ojas`.

## 8. Keep it always on

```bash
sudo cp ../deploy/james-monitoring.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now james-monitoring
journalctl -u james-monitoring -f
```

Or use Docker: see `deploy/docker-compose.yml`.

## 9. Back up the workspace

Set `workspace.push: true` and add a private GitHub remote to the workspace repo. After that, every task,
memory, decision and report is pushed as soon as it changes.

## Adding someone later

```bash
jm add-member Riya --role "QA lead" --projects janus --persona ~/skills/riya/SKILL.md --token 123:ABC
```

Add the new bot to the group, press Start on it, then restart.
