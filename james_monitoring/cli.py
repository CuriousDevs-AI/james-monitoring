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
    from .gateway import TelegramGateway
    cfg = _cfg(args)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    gw = TelegramGateway(cfg)
    gw.build()
    rt = _runtime(cfg, bus=gw)
    asyncio.run(gw.run(rt))


def cmd_chat(args) -> None:
    """Talk to a team member locally, without Telegram. /commands work too."""
    from .commands import run_command
    from .runtime import ConsoleBus, Event
    cfg = _cfg(args)
    who = cfg.member(args.member)
    if not who:
        sys.exit(f"unknown member `{args.member}`")
    rt = _runtime(cfg, bus=ConsoleBus())

    async def one(text: str) -> None:
        text = text.strip()
        if not text:
            return
        if text.startswith("/"):
            cmd, _, rest = text[1:].partition(" ")
            try:
                out = await run_command(rt, cmd.lower(), rest.strip(), who.id, private=True, background=False)
            except Exception as e:  # noqa: BLE001 - show any user error
                out = f"⚠️ {e}"
        else:
            out = await rt.dispatch(who.id, Event("dm", text, sender=rt.owner_id))
        print(f"\n{who.name}: {out}")
        await rt.drain()

    async def main() -> None:
        if args.message:
            await one(" ".join(args.message))
            return
        print(f"Chatting with {who.name} ({who.role}). Ctrl-D to exit. /help for commands.")
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
    print(asyncio.run(rt.run_work_session()) or "Nobody has open todo/doing tasks.")


def cmd_doctor(args) -> None:
    cfg = _cfg(args)
    ok = True

    def check(cond: bool, good: str, bad: str) -> None:
        nonlocal ok
        print(("  ✅ " + good) if cond else ("  ❌ " + bad))
        ok = ok and cond

    print(f"james-monitoring {__version__} — {cfg.path}")
    print("AI")
    if cfg.llm.provider in ("claude-code", "claude_code", "claude-cli", "subscription"):
        check(shutil.which("claude") is not None, "claude CLI installed (uses your subscription login)",
              "`claude` CLI not found — npm install -g @anthropic-ai/claude-code, then run `claude` and /login")
        print(f"  ·  model: {cfg.llm.model or 'CLI default'}")
    else:
        check(bool(cfg.llm.model), f"model: {cfg.llm.provider}/{cfg.llm.model}", "llm.model is empty")
        check(bool(cfg.llm.api_key) or cfg.llm.provider in ("fake",) or bool(cfg.llm.base_url),
              f"API key present ({cfg.llm.api_key_env})", f"{cfg.llm.api_key_env} not set in .env")
    if args.ping:
        from .llm import LLMError, make_llm
        try:
            r = make_llm(cfg.llm).complete("Reply with the single word: pong", [{"role": "user", "content": "ping"}])
            check("pong" in r.text.lower(), f"model replied ({r.total_tokens} tokens)", f"odd reply: {r.text[:80]}")
        except LLMError as e:
            check(False, "", str(e))
    print("Workspace")
    check(shutil.which("git") is not None, "git installed", "git not found")
    check((cfg.workspace_path / ".git").exists(), f"git repo at {cfg.workspace_path}", "workspace is not a git repo (run jm init)")
    check((cfg.workspace_path / "team/charter.md").exists(), "charter present", "team/charter.md missing")
    for m in cfg.team:
        check((cfg.workspace_path / f"team/{m.id}/persona.md").exists(), f"persona: {m.name}", f"persona missing for {m.id}")
    print("Telegram")
    check(cfg.owner_user_id != 0, f"owner user id {cfg.owner_user_id}", "owner.telegram_user_id not set (use /whoami)")
    check(cfg.group_chat_id != 0, f"group id {cfg.group_chat_id}", "telegram.group_chat_id not set (use /groupid)")
    for m in cfg.team:
        check(bool(m.bot_token), f"bot token: {m.name}", f"{m.bot_token_env} not set — {m.name} will be offline")
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

    s = sub.add_parser("run", help="start the Telegram team (all bots + the manager's schedule)")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("chat", help="talk to a member locally, no Telegram")
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

    s = sub.add_parser("doctor", help="check the setup")
    s.add_argument("--ping", action="store_true", help="also call the model and Telegram")
    s.set_defaults(fn=cmd_doctor)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
