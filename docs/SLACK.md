# Slack

Run the team from Slack, in sync with the web console and Telegram. About 3 minutes.

- **One Slack app for the whole team.** Every teammate posts under their own name and icon.
- **Socket Mode**: no public URL, no open ports. It works on a laptop or behind NAT, like Telegram polling.
- **The same rooms everywhere.** Anything said in Slack shows in the console and on Telegram, and the other way round.

| Slack channel | Console room | What it's for |
|---|---|---|
| `#jm-hq` | All hands | Announcements, `@all status`, the daily report |
| `#jm-p-<project>` | the project's room | That project's team. Teammates' hand-offs show here too |
| `#jm-dm-<person>` (private) | your 1:1 with that person | Instructions, corrections, approval cards |
| `#jm-backchannel` (private) | Team backchannel | Teammates talking to each other outside a project. Read-only |
| the app's DM | → the manager | "@riya …" reaches Riya, anything else goes to the manager |

## 1. Create the app from this manifest

Open <https://api.slack.com/apps?new_app=1>, choose **From a manifest**, pick your workspace, and paste:

```yaml
display_information:
  name: AI Team
  description: Your AI team (james-monitoring)
features:
  bot_user:
    display_name: ai-team
    always_online: true
  app_home:
    messages_tab_enabled: true
    messages_tab_read_only_enabled: false
oauth_config:
  scopes:
    bot:
      - chat:write
      - chat:write.customize
      - channels:history
      - channels:read
      - channels:join
      - channels:manage
      - groups:history
      - groups:read
      - groups:write
      - im:history
      - im:read
      - im:write
      - users:read
      - users:read.email
      - commands
  slash_commands:
    - command: /jm
      description: Run your AI team (status, board, assign, approve…)
      usage_hint: status | board | assign riya "title" P1 | approve ASK-3
      should_escape: false
settings:
  event_subscriptions:
    bot_events:
      - message.channels
      - message.groups
      - message.im
  interactivity:
    is_enabled: true
  socket_mode_enabled: true
```

Then:

1. **Install App** → *Install to Workspace*. Copy the **Bot User OAuth Token** (`xoxb-…`).
2. **Basic Information** → *App-Level Tokens* → *Generate*, with the scope `connections:write`. Copy it (`xapp-…`).

## 2. Connect it in the console

**Settings → Slack**:

1. Paste both tokens and click **Connect**. The tokens are saved to `.env` (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`), never in `config.yaml`.
2. **You**: type your Slack email and click **Find me**. Or click **Detect me** and send the app the code it
   shows. Or paste your member id (Slack → your profile → ⋮ → *Copy member ID*) and **Save & restart**. Only these
   members can command the team.
3. Click **Create / connect channels**. The channels above are created (existing ones with the same name are
   reused) and you're invited. The mapping is saved in `config.yaml` under `slack.channels`, where you can
   point a room at any channel you like.

Add a project or a person later? Click **Create / connect channels** again.

## Using it

- Write in a channel just as in the console: `@all give status` (or `@here`), `@riya can you…`, or no mention
  (the project lead or the manager answers).
- **Reply in a thread** to someone's message and it goes to that person.
- **Commands**: `/jm status`, `/jm board`, `/jm assign riya "Rate limits" P1`, `/jm accept T-012`,
  `/jm changes T-012 shorter headline`, `/jm approve ASK-3`, `/jm pause all`. Typing them with `!` works too (`!status`).
  You can also just write `approve ASK-3` or `reject ASK-3 too expensive`.
- **Approval cards** have ✅ Approve / ❌ Reject buttons. Deciding anywhere (Slack, Telegram or the console) settles it everywhere.

## config.yaml

```yaml
slack:
  bot_token_env: SLACK_BOT_TOKEN      # defaults
  app_token_env: SLACK_APP_TOKEN
  owner_user_ids: [U012AB3CD]
  channels:                            # room → channel id (the console fills this in)
    team: C0123HQ                      # #jm-hq
    p-api: C0456API                    # #jm-p-api
    riya: G0789RIYA
    backchannel: G0ABCBACK
sync:
  mirror_owner: true                   # your messages appear in every channel (false: only where you wrote them)
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Slack error" badge in the top bar | Settings → Slack shows the error. Usually a missing scope: add it in *OAuth & Permissions* and reinstall the app |
| Messages show the app's name, not the person's | Add `chat:write.customize` and reinstall |
| The team ignores you | Your member id isn't in `slack.owner_user_ids`. Use **Detect me** |
| Nothing arrives from Slack | Socket Mode must be on, and the app token needs `connections:write` |
