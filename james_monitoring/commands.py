"""Owner commands, shared by Telegram and the local CLI."""
from __future__ import annotations

import asyncio

from .runtime import Event, Runtime

HELP = [
    ("status", "Everyone's status from the board"),
    ("board", "All tasks by state"),
    ("assign", 'Assign: /assign <who> "title" P1 due:10-03 project:<id>'),
    ("accept", "Accept a task in review: /accept T-001 [note]"),
    ("feedback", "Feedback on a task: /feedback T-001 text"),
    ("changes", "Send work back: /changes T-001 what to change"),
    ("cut", "Cut a task: /cut T-001 [reason]"),
    ("asks", "Pending permission requests"),
    ("approve", "Approve: /approve ASK-001 [note]"),
    ("reject", "Reject: /reject ASK-001 [note]"),
    ("pause", "Pause this person, or /pause all  (alias /stop)"),
    ("resume", "Resume this person, or /resume all  (alias /start all)"),
    ("log", "Activity log: /log <who>"),
    ("budget", "Token use today"),
    ("report", "Generate and post the status report now"),
    ("work", "Start a work session now: everyone moves their top task"),
    ("onboard", "Post team intros in the group"),
    ("link", "The console link (private chats only)"),
    ("whoami", "Show your Telegram user id"),
    ("groupid", "Show this chat's id"),
    ("help", "All commands"),
]


async def ack_assignment(rt: Runtime, who: str, line: str) -> None:
    reply = await rt.dispatch(who, Event("system", f"{rt.cfg.owner_name} assigned you: {line}. If the task has no "
                                                   f"'Done means', set 2–4 checks with update_task. Acknowledge in one "
                                                   f"line: what you'll do first and when.", sender="system"))
    await rt.bus.send_owner(who, reply)


async def run_command(rt: Runtime, cmd: str, args: str, member_id: str, private: bool,
                      background: bool = True) -> str:
    """Execute an owner command. Raises ValueError/TaskError/AskError for user errors."""
    cfg = rt.cfg
    is_monitor = member_id == cfg.monitor.id
    default_who = member_id if (private and not is_monitor) else "all"
    if cmd in ("help", "start") and not args:
        return "\n".join(f"/{c} — {d}" for c, d in HELP)
    if cmd == "status":
        return rt.cmd_status()
    if cmd == "board":                              # the personal assistant's tasks are private
        return rt.tasks.board(skip_owners={m.id for m in cfg.assistants})
    if cmd == "link":
        if not private:
            return "I'll only send the console link in a private chat — it opens the console as you."
        link = getattr(rt, "console_link", None)
        if not link:
            return "The console isn't running here (start it with `jm run`)."
        url = link()
        local = "localhost" in url
        return (f"🔗 Your console: {url}" + ("\nIt's only reachable from the machine running it — turn on the public "
                                              "link in Settings → Channels (or `jm run --public`) to open it anywhere."
                                              if local else "\nAnyone with this link is you — don't forward it."))
    if cmd == "assign":
        text, who = await asyncio.to_thread(rt.cmd_assign, args)      # git commit off the event loop
        coro = ack_assignment(rt, who, text)
        if background:
            rt._spawn(coro)
        else:
            await coro
        return text
    if cmd == "accept":
        return await rt.cmd_accept(args)
    if cmd == "feedback":
        return await rt.cmd_feedback(args)
    if cmd in ("changes", "rework"):
        return await rt.cmd_changes(args)
    if cmd == "cut":
        return await asyncio.to_thread(rt.cmd_cut, args)
    if cmd == "asks":
        return rt.cmd_asks()
    if cmd in ("approve", "reject"):
        parts = args.split(maxsplit=1)
        if not parts:
            raise ValueError(f"usage: /{cmd} ASK-001 [note]")
        return await rt.decide_ask(parts[0], "approved" if cmd == "approve" else "rejected",
                                   by=cfg.owner_name, note=parts[1] if len(parts) > 1 else "")
    if cmd in ("pause", "stop", "resume", "start"):
        pause = cmd in ("pause", "stop")
        who = (args or default_who).lstrip("@").lower()
        if who != "all":
            m = cfg.member(who)
            if not m:
                raise ValueError(f"unknown member `{who}`")
            who = m.id
        res = rt.set_paused(who, pause)
        if not pause:
            n = await rt.drain_queue()                 # messages that waited while they were paused
            if n:
                res += f" · {n} queued message{'s' if n != 1 else ''} being answered"
        if who == "all":
            await rt.bus.post_group(cfg.monitor.id, f"⏸ {cfg.owner_name}: all work is paused until further notice."
                                    if pause else f"▶️ {cfg.owner_name}: work is ON. Go.")
        return res
    if cmd == "log":
        return rt.cmd_log(args or member_id)
    if cmd == "budget":
        return rt.cmd_budget()
    if cmd == "report":
        rel = await rt.run_daily_report()
        return f"Report posted to the group and saved as {rel}"
    if cmd == "work":
        digest = await rt.run_work_session()
        return ("🛠 Work session done:\n" + digest) if digest else "Nobody has open todo/doing tasks."
    if cmd == "onboard":
        for mid, text in rt.onboarding_messages():
            await rt.bus.post_group(mid, text)
        return "Intros posted in the group."
    raise ValueError(f"unknown command /{cmd} — try /help")

