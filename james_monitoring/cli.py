"""jm — command line for james-monitoring."""
from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config


def _cfg(args):
    try:
        return load_config(args.config)
    except ConfigError as e:
        sys.exit(f"error: {e}")


def _runtime(cfg, bus=None):
    from .llm import make_llm
    from .runtime import Runtime
    return Runtime(cfg, make_llm(cfg.llm), bus=bus)


def cmd_init(args) -> None:
    from .setup import wizard
    base = Path(args.dir).resolve()
    if (base / "config.yaml").exists() and not args.force:
        sys.exit(f"{base / 'config.yaml'} exists. Use `jm add-member` to grow the team, or --force to start over.")
    try:
        asyncio.run(wizard(base, api_base=args.telegram_api))
    except (KeyboardInterrupt, EOFError):
        sys.exit("\nSetup stopped. Everything confirmed so far is saved; run `jm init --force` to redo.")


def cmd_add_member(args) -> None:
    from .setup import add_member, add_member_interactive
    cfg = _cfg(args)
    if args.name and args.role:            # scripted, no Telegram checks
        projects = [p.strip() for p in (args.projects or "").split(",") if p.strip()]
        mid = add_member(cfg.path, name=args.name, role=args.role, projects=projects,
                         persona_file=args.persona or "", token=args.token or "")
        print(f"Added {mid}. Verify with `jm doctor --ping`, then restart `jm run`.")
        return
    try:
        asyncio.run(add_member_interactive(cfg.path))
    except (KeyboardInterrupt, EOFError):
        sys.exit("\nStopped; nothing was added.")


def cmd_remove_member(args) -> None:
    from .setup import remove_member
    cfg = _cfg(args)
    try:
        name = remove_member(cfg.path, args.member)
    except ValueError as e:
        sys.exit(f"error: {e}")
    print(f"Removed {name} from the team (their files stay in git). Restart `jm run`.")


def cmd_run(args) -> None:
    """The console (web) + team runtime + Telegram (if connected) + scheduler. Works in an empty folder too:
    the browser then shows the setup screen."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg_path = Path(args.config).expanduser().resolve()
    if args.no_web:
        from .gateway import TelegramGateway
        from .hub import Hub
        from .scheduler import Scheduler
        cfg = _cfg(args)
        gw = TelegramGateway(cfg)
        gw.build()
        hub = Hub()
        rt = _runtime(cfg, bus=hub)
        hub.attach(rt)

        async def main():
            if cfg.slack.configured:
                from .slack import SlackTransport
                try:
                    await SlackTransport(cfg).start(rt, hub)
                except Exception as e:  # noqa: BLE001
                    logging.getLogger("jm").error("Slack failed to start: %s", e)
            await gw.run(rt, scheduler=Scheduler(rt), hub=hub)
        asyncio.run(main())
        return
    from .server import serve
    serve(cfg_path.parent, host=args.host, port=args.port, open_browser=not args.no_browser,
          telegram=not args.no_telegram)


class _Print:
    """`jm chat` as a channel: prints what happens in the rooms (the chat is also saved for the console)."""
    name = "cli"

    def __init__(self, cfg):
        self.cfg = cfg

    async def deliver(self, room, msg, ask=None):
        m = self.cfg.member(msg.get("who", ""))
        where = "" if self.cfg.member(room) else f"[{room}] "
        print(f"\n{where}{m.name if m else msg.get('who')}: {msg.get('text')}" + ("  [/approve|/reject " + ask.id + "]"
                                                                               if ask else ""))


def cmd_chat(args) -> None:
    """Talk to a team member locally, without Telegram. /commands work too. Saved like any other chat."""
    from .hub import Hub
    cfg = _cfg(args)
    who = cfg.member(args.member)
    if not who and args.member not in ("team", "all") and not args.member.startswith("p-"):
        sys.exit(f"unknown member `{args.member}`")
    room = who.id if who else ("team" if args.member in ("team", "all") else args.member)
    hub = Hub()
    rt = _runtime(cfg, bus=hub)
    hub.attach(rt)
    hub.add(_Print(cfg))

    async def one(text: str) -> None:
        text = text.strip()
        if not text:
            return
        try:
            await hub.inbound(room, text, via="cli")
        except ValueError as e:
            print(f"⚠️ {e}")
        await rt.drain()

    async def main() -> None:
        if args.message:
            await one(" ".join(args.message))
            return
        print(f"Chatting with {who.name} ({who.role})." if who else f"Chatting in {room}.",
              "Ctrl-D to exit. /help for commands.")
        while True:
            try:
                line = await asyncio.to_thread(input, "\nyou> ")
            except EOFError:
                break
            await one(line)
    asyncio.run(main())


def cmd_status(args) -> None:
    from .runtime import ConsoleBus, Runtime
    from .llm.fake import FakeLLM
    rt = Runtime(_cfg(args), FakeLLM(), bus=ConsoleBus(quiet=True))
    print(rt.cmd_status())
    print()
    print(rt.tasks.board())


def cmd_report(args) -> None:
    from .monitor import build_report, write_report
    from .runtime import ConsoleBus, Runtime
    from .llm.fake import FakeLLM
    cfg = _cfg(args)
    rt = Runtime(cfg, FakeLLM(), bus=ConsoleBus(quiet=True))
    if args.write:
        rel, text = write_report(cfg, rt.ws, rt.tasks, rt.asks)
        print(text)
        print(f"(saved {rel})")
    else:
        print(build_report(cfg, rt.ws, rt.tasks, rt.asks))


def cmd_work(args) -> None:
    from .runtime import ConsoleBus
    rt = _runtime(_cfg(args), bus=ConsoleBus())
    async def main():
        digest = await rt.run_work_session()
        await rt.drain()                               # the CLI waits for coding runs / hand-offs to finish
        return digest
    print(asyncio.run(main()) or "Nobody has open todo/doing tasks.")


def cmd_ui(args) -> None:
    args.no_web = False
    cmd_run(args)


def cmd_connection(args) -> None:
    """Every AI model the team uses: installed? logged in? answering? — and log in right here in the terminal."""
    import subprocess
    from . import connections as cx
    cfg = _cfg(args)
    if args.action == "login":
        kind = cx._kind({"claude": "claude-code", "codex": "codex-cli"}.get(args.target or "", args.target or "")
                        or cfg.llm.provider)
        cmd = cx.LOGIN.get(kind) or cx.TERMINAL_LOGIN.get(kind)
        b = cx._bin(kind)
        if not cmd:
            sys.exit(f"{args.target or cfg.llm.provider}: no login — it uses an API key (set it in Settings or .env).")
        if not b:
            sys.exit(f"Install it first: {cx.INSTALL.get(kind, kind)}")
        print(f"→ {' '.join([Path(b).name, *cmd])}   (follow the prompts; your browser may open)\n")
        rc = subprocess.call([b, *cmd])
        print("\n✅ Done — check with: jm connection" if rc == 0 else f"\n❌ Login exited with {rc}")
        sys.exit(rc)
    groups = cx.providers_in_use(cfg)
    bad = 0
    print(f"AI models — {cfg.company}")
    for g in groups:
        c = cfg_c = g["config"]
        r = cx.check(cfg_c)
        who = ", ".join((cfg.member(x).name if cfg.member(x) else x) for x in g["people"]) or "nobody"
        line = f"  {'✅' if r['ok'] else '❌'} {r['label']} · {c.model or 'default'} — {r['detail']}   (used by {who})"
        if args.action == "test" and r["ok"]:
            t = cx.test(c)
            line += f"\n     test: {'answers in ' + str(t['seconds']) + 's' if t['ok'] else '❌ ' + t['error']}"
            r["ok"] = t["ok"]
        print(line)
        if not r["ok"]:
            bad += 1
            kind = r["provider"]
            fix = (f"jm connection login {kind}" if (kind in cx.LOGIN or kind in cx.TERMINAL_LOGIN) and r["installed"]
                   else r["fix"])
            print(f"     fix: {fix}")
    name, email = cfg.git_author
    print(f"Git identity\n  {'✅' if email != 'jm@localhost' else '⚠️ '} {name} <{email}>")
    print("Channels")
    print(f"  {'✅' if cfg.monitor.bot_token else '·'} Telegram {'connected' if cfg.monitor.bot_token else '(optional)'}")
    print(f"  {'✅' if cfg.slack.configured else '·'} Slack {'connected' if cfg.slack.configured else '(optional)'}")
    print(f"  {'✅' if cfg.github.enabled else '·'} GitHub {'board mirror on' if cfg.github.enabled else '(optional)'}")
    if args.action != "test":
        print("\nRun `jm connection test` to make one tiny call to each model.")
    sys.exit(1 if bad else 0)


def cmd_doctor(args) -> None:
    cfg = _cfg(args)
    ok = True

    def check(cond: bool, good: str, bad: str) -> None:
        nonlocal ok
        print(("  ✅ " + good) if cond else ("  ❌ " + bad))
        ok = ok and cond

    print(f"james-monitoring {__version__} — {cfg.path}")
    print("AI (every model the team uses)")
    from . import connections as cx
    for g in cx.providers_in_use(cfg):
        r = cx.check(g["config"])
        who = ", ".join((cfg.member(x).name if cfg.member(x) else x) for x in g["people"])
        check(r["ok"], f"{r['label']} · {g['config'].model or 'default'} — {r['detail']} ({who})",
              f"{r['label']} — {r['detail']}: {r['fix'] or 'see `jm connection`'} ({who})")
        if args.ping and r["ok"]:
            t = cx.test(g["config"])
            check(t["ok"], f"  answers ({t.get('seconds', 0)}s)", f"  test call failed: {t.get('error', '')}")
    print("Workspace")
    check(shutil.which("git") is not None, "git installed", "git not found")
    check((cfg.workspace_path / ".git").exists(), f"git repo at {cfg.workspace_path}", "workspace is not a git repo (run jm init)")
    check((cfg.workspace_path / "team/charter.md").exists(), "charter present", "team/charter.md missing")
    for m in cfg.team:
        check((cfg.workspace_path / f"team/{m.id}/persona.md").exists(), f"persona: {m.name}", f"persona missing for {m.id}")
    print("Telegram")
    if not any(m.bot_token for m in cfg.team):
        print("  ·  not connected (optional — everything works in the console; see Settings → Telegram)")
    else:
        check(cfg.owner_user_id != 0, f"owner user id {cfg.owner_user_id}", "owner.telegram_user_id not set (use /whoami)")
        check(cfg.group_chat_id != 0, f"group id {cfg.group_chat_id}", "telegram.group_chat_id not set (use /groupid)")
        for m in cfg.team:
            if m.bot_token or m.monitor:
                check(bool(m.bot_token), f"bot token: {m.name}", f"{m.bot_token_env} not set — {m.name}'s bot is offline")
            else:
                print(f"  ·  {m.name}: no own bot (reachable through {cfg.monitor.name}'s bot with “@{m.id} …”)")
    if args.ping:
        async def tg():
            from telegram import Bot
            for m in cfg.team:
                if not m.bot_token:
                    continue
                try:
                    kw = {"base_url": f"{cfg.telegram_api_base}/bot"} if cfg.telegram_api_base else {}
                    async with Bot(m.bot_token, **kw) as bot:
                        me = await bot.get_me()
                        check(True, f"{m.name} → @{me.username}", "")
                        if cfg.group_chat_id:
                            try:
                                cm = await bot.get_chat_member(cfg.group_chat_id, me.id)
                                inside = cm.status in ("member", "administrator", "creator")
                            except Exception:  # noqa: BLE001
                                inside = False
                            check(inside, f"{m.name} is in the group", f"@{me.username} is NOT in the group — add it")
                        if m.monitor:
                            check(bool(me.can_read_all_group_messages), f"{m.name} can read the group",
                                  f"@{me.username}: BotFather → /setprivacy → Disable")
                except Exception as e:  # noqa: BLE001
                    check(False, "", f"{m.name}: {e}")
        asyncio.run(tg())
    print("Projects")
    for pid, p in cfg.projects.items():
        if p.repo:
            check((Path(p.repo) / ".git").exists(), f"{pid}: {p.repo}", f"{pid}: {p.repo} is not a git repo")
        else:
            print(f"  ·  {pid}: no repo (code tasks disabled for it)")
    if cfg.executor_command:
        check(shutil.which(cfg.executor_command[0]) is not None, f"coding CLI: {cfg.executor_command[0]}",
              f"coding CLI `{cfg.executor_command[0]}` not on PATH")
    print("\nAll good." if ok else "\nFix the ❌ items, then run `jm doctor` again.")
    sys.exit(0 if ok else 1)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="jm", description="james-monitoring — run an AI team like a real team")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-c", "--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="set up a team: AI, workspace, manager bot, members (verified live)")
    s.add_argument("--dir", default=".")
    s.add_argument("--force", action="store_true")
    s.add_argument("--telegram-api", default="", help=argparse.SUPPRESS)   # self-hosted Bot API / tests
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("add-member", help="add a person: name → persona/SKILL.md → bot → group ✓ → DM ✓")
    s.add_argument("name", nargs="?", help="with --role: add without the interactive Telegram checks")
    s.add_argument("--role")
    s.add_argument("--projects", default="")
    s.add_argument("--persona", help="persona file, SKILL.md, or a folder containing one")
    s.add_argument("--token", help="Telegram bot token (saved to .env)")
    s.set_defaults(fn=cmd_add_member)

    s = sub.add_parser("remove-member", help="take someone off the team (files kept in git)")
    s.add_argument("member")
    s.set_defaults(fn=cmd_remove_member)

    s = sub.add_parser("run", help="start everything: web console + team + Telegram (if connected) + schedule")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--no-telegram", action="store_true", help="console only")
    s.add_argument("--no-web", action="store_true", help="Telegram + schedule only, no console")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("chat", help="talk to a member (or `team`, or a project room p-<id>) from the terminal")
    s.add_argument("member")
    s.add_argument("message", nargs="*")
    s.set_defaults(fn=cmd_chat)

    s = sub.add_parser("status", help="print everyone's status + board")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("report", help="print the status report")
    s.add_argument("--write", action="store_true", help="also save to reports/ and commit")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("work", help="run one work session now (everyone moves their top task)")
    s.set_defaults(fn=cmd_work)

    s = sub.add_parser("ui", help="same as `jm run` (the web console)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--no-telegram", action="store_true")
    s.set_defaults(fn=cmd_ui)

    s = sub.add_parser("connection", help="check every AI model (installed, logged in, answering) · login · test")
    s.add_argument("action", nargs="?", choices=["check", "test", "login"], default="check")
    s.add_argument("target", nargs="?", help="for login: claude-code | codex-cli | opencode")
    s.set_defaults(fn=cmd_connection)

    s = sub.add_parser("doctor", help="check the setup")
    s.add_argument("--ping", action="store_true", help="also call the model and Telegram")
    s.set_defaults(fn=cmd_doctor)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
