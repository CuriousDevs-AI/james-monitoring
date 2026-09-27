"""Owner commands, shared by Telegram and the local CLI."""
from __future__ import annotations

from .runtime import Event, Runtime

HELP = [
    ("status", "Everyone's status from the board"),
    ("board", "All tasks by state"),
    ("assign", 'Assign: /assign <who> "title" P1 due:10-03 project:<id>'),
    ("accept", "Accept a task in review: /accept T-001 [note]"),
    ("feedback", "Feedback on a task: /feedback T-001 text"),
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
    ("whoami", "Show your Telegram user id"),
    ("groupid", "Show this chat's id"),
    ("help", "All commands"),
]


async def ack_assignment(rt: Runtime, who: str, line: str) -> None:
    reply = await rt.dispatch(who, Event("system", f"{rt.cfg.owner_name} assigned you: {line}. Acknowledge in one "
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
    if cmd == "board":
        return rt.tasks.board()
    if cmd == "assign":
        text, who = rt.cmd_assign(args)
        coro = ack_assignment(rt, who, text)
        if background:
            rt._spawn(coro)
        else:
            await coro
        return text
    if cmd == "accept":
        return rt.cmd_accept(args)
    if cmd == "feedback":
        return rt.cmd_feedback(args)
    if cmd == "cut":
        return rt.cmd_cut(args)
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

