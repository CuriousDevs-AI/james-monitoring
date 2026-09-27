"""The manager's monitoring: deterministic, from the files — no model call, so it can't invent progress."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .asks import AskStore
from .config import Config
from .tasks import TaskStore
from .util import now, today
from .workspace import Workspace


@dataclass
class Alert:
    key: str          # de-duplication key (sent once per day)
    text: str
    incident: bool = False


def team_status_lines(cfg: Config, tasks: TaskStore) -> list[tuple[str, str, str]]:
    rows = []
    for m in cfg.team:
        st, line = tasks.person_status(m.id)
        rows.append((m.name, st, line))
    return rows


def open_decisions(ws: Workspace) -> list[str]:
    """Numbered or bulleted lines in decisions/OPEN.md — the owner's decision queue."""
    import re
    out = []
    for line in ws.read("decisions/OPEN.md").splitlines():
        m = re.match(r"\s*(?:\d+[.)]|[-*])\s+(.+)", line)
        if m and not m.group(1).startswith("~~"):
            out.append(m.group(1).strip())
    return out


def build_report(cfg: Config, ws: Workspace, tasks: TaskStore, asks: AskStore) -> str:
    tz = cfg.timezone
    d = today(tz)
    all_tasks = tasks.all()
    open_tasks = [t for t in all_tasks if t.is_open]
    rows = team_status_lines(cfg, tasks)
    counts = {k: sum(1 for r in rows if r[1] == k) for k in ["on track", "at risk", "blocked", "not started"]}
    stale_cut = d - timedelta(days=cfg.stale_days)
    stale = [t for t in open_tasks if (t.date_field("updated") or d) < stale_cut]

    state = ws.state()
    last = state.get("last_report_date")
    since = datetime.fromisoformat(last).date() if last else d - timedelta(days=1)
    shipped = [t for t in all_tasks if t.status == "done" and (t.date_field("done_on") or d) >= since]
    in_review = [t for t in all_tasks if t.status == "review"]
    blocked = [t for t in open_tasks if t.status == "blocked"]
    overdue = [t for t in open_tasks if t.overdue(tz) and t.status != "blocked"]
    pend = asks.pending()
    owner_blocks = [t for t in blocked if cfg.owner_name.lower() in t.blocked_on.lower()
                    or cfg.owner_key in t.blocked_on.lower()]
    p0 = sorted([t for t in open_tasks if t.priority == "P0"], key=lambda t: (t.due(tz) or d + timedelta(days=999)))

    if pend or owner_blocks or open_decisions(ws):
        n_dec = len(pend) + len(open_decisions(ws))
        headline = (f"{n_dec} decision(s) and {len(owner_blocks)} block(s) waiting on {cfg.owner_name} — "
                    f"that is the bottleneck.")
    elif blocked:
        headline = f"{len(blocked)} task(s) blocked; biggest: {blocked[0].line(tz)}"
    elif overdue:
        headline = f"{len(overdue)} task(s) overdue; biggest: {overdue[0].line(tz)}"
    elif not all_tasks:
        headline = "No tasks on the board yet — nothing is being tracked."
    else:
        headline = f"{len(open_tasks)} open task(s), none blocked or overdue."

    L: list[str] = [f"# Status report — {d.isoformat()}", "", "## Headline", headline, "",
                    "## Snapshot",
                    f"{counts['on track']} on track · {counts['at risk']} at risk · {counts['blocked']} blocked · "
                    f"{counts['not started']} not started · {len(stale)} stale task(s)", ""]
    L.append(f"## Needs {cfg.owner_name}")
    items = [f"- {a.id} ({a.requester}): {a.summary} — since {str(a.doc.meta.get('created', ''))[:10]}"
             for a in pend]
    items += [f"- Unblock {t.id} ({t.owner}): {t.blocked_on}" for t in owner_blocks]
    items += [f"- Review {t.id} ({t.owner}): {t.title}" for t in in_review]
    items += [f"- Decide: {d}" for d in open_decisions(ws)]
    L += items or ["- nothing"]
    L += ["", "## Blocked / at risk"]
    L += [f"- {t.line(tz)}" for t in blocked + overdue] or ["- none"]
    L += ["", "## Shipped since last report"]
    L += [f"- {t.owner}: {t.id} {t.title} — {t.doc.meta.get('done_on')}" for t in shipped] or ["- nothing"]
    L += ["", "## People"]
    L += [f"- {n}: {st} — {line}" for n, st, line in rows]
    if stale:
        L += ["", "## Stale (not updated in "
              f"{cfg.stale_days}+ days)"] + [f"- {t.line(tz)} — last update {t.doc.meta.get('updated')}" for t in stale]
    L += ["", "## Critical path"]
    L += [f"- {t.line(tz)}" for t in p0[:3]] or ["- no P0 set — assign one per active project"]
    hb = state.get("heartbeat", {})
    errs = [f"- {k}: {v.get('error')}" for k, v in hb.items() if v.get("error")]
    if errs:
        L += ["", "## System issues"] + errs
    return "\n".join(L) + "\n"


def write_report(cfg: Config, ws: Workspace, tasks: TaskStore, asks: AskStore) -> tuple[str, str]:
    text = build_report(cfg, ws, tasks, asks)
    rel = f"reports/{today(cfg.timezone).isoformat()}.md"
    ws.write(rel, text)
    ws.update_state(lambda s: s.__setitem__("last_report_date", today(cfg.timezone).isoformat()))
    ws.commit(f"report: {today(cfg.timezone).isoformat()}", author=cfg.monitor.name)
    return rel, text


def checks(cfg: Config, ws: Workspace, tasks: TaskStore, asks: AskStore) -> list[Alert]:
    """Periodic checks. Returns alerts not yet sent today."""
    tz = cfg.timezone
    d = today(tz)
    alerts: list[Alert] = []
    for a in asks.pending():
        dl = a.deadline()
        if dl and dl <= now(tz) and a.default == "wait":
            alerts.append(Alert(f"ask-wait:{a.id}", f"⏳ {a.id} from {a.requester} is past its deadline and "
                                                    f"waiting on you: {a.summary}"))
    for t in tasks.all():
        if t.status == "blocked":
            since = t.date_field("status_since") or d
            if (d - since).days >= cfg.blocked_escalate_days:
                alerts.append(Alert(f"blocked:{t.id}", f"🚧 {t.id} ({t.owner}) blocked {(d - since).days} days — "
                                                        f"{t.blocked_on}"))
        elif t.overdue(tz):
            alerts.append(Alert(f"overdue:{t.id}", f"⚠️ {t.id} ({t.owner}) is overdue (due {t.doc.meta.get('due')}): "
                                                    f"{t.title}"))
    if ws.state().get("push_error"):
        alerts.append(Alert("push", f"⚠️ Team repo can't push to its remote: {ws.state()['push_error']}"))
    for agent, hb in ws.state().get("heartbeat", {}).items():
        if hb.get("error"):
            alerts.append(Alert(f"err:{agent}", f"🚨 {agent} is failing: {hb['error']}", incident=True))
    sent = ws.state().get("alerts_sent", {})
    key_day = d.isoformat()
    fresh = [a for a in alerts if sent.get(a.key) != key_day]

    def mark(s):
        s.setdefault("alerts_sent", {})
        for a in fresh:
            s["alerts_sent"][a.key] = key_day
    if fresh:
        ws.update_state(mark)
    return fresh
