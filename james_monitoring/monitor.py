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
    owner: bool = True        # tell the founder
    nudge: str = ""           # member the manager chases directly
    task: str = ""

REVIEW_WAIT_DAYS = 2
INCIDENT_AFTER_FAILS = 3


def on_owner(cfg: Config, text: str) -> bool:
    """Is this "blocked on" about the founder? Their id, full name or first name, as whole words ("Al" never matches
    "approval") — the same rule the console uses."""
    import re
    first = cfg.owner_name.split()[0].lower() if cfg.owner_name.split() else ""
    names = {cfg.owner_key, cfg.owner_name.lower()} | ({first} if len(first) >= 3 else set())
    pat = r"\b(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True) if n) + r")\b"
    return bool(re.search(pat, text or "", re.I)) and "approval ask-" not in (text or "").lower()


def team_status_lines(cfg: Config, tasks: TaskStore) -> list[tuple[str, str, str]]:
    rows = []
    for m in cfg.workers:                              # the personal assistant's work is private
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


def activity(cfg: Config, ws: Workspace, tasks: TaskStore, day: str) -> dict[str, list[str]]:
    """What each person did on `day` (YYYY-MM-DD), from the files: task log lines they wrote and documents
    they saved. Chat replies don't count — only work that left a trace."""
    out: dict[str, list[str]] = {m.id: [] for m in cfg.team}
    for t in tasks.all():
        for line in t.doc.sections.get("Log", "").splitlines():
            parts = line.lstrip("- ").split(" — ", 1)
            if len(parts) == 2 and parts[0].strip() == day:
                who, _, what = parts[1].partition(":")
                who = who.strip()
                if who in out and what.strip():
                    out[who].append(f"{t.id} {what.strip()}")
    for m in cfg.team:
        for line in ws.read(f"team/{m.id}/log.md").splitlines():
            if line.startswith(f"- {day}") and " — wrote " in line:
                out[m.id].append("wrote " + line.split(" — wrote ", 1)[1])
    return out


def build_report(cfg: Config, ws: Workspace, tasks: TaskStore, asks: AskStore) -> str:
    tz = cfg.timezone
    d = today(tz)
    personal = {m.id for m in cfg.assistants}              # the founder's own tasks stay out of the company report
    all_tasks = [t for t in tasks.all() if t.owner not in personal]
    open_tasks = [t for t in all_tasks if t.is_open]
    rows = team_status_lines(cfg, tasks)
    workers = {m.name for m in cfg.workers if not m.monitor}   # the manager coordinates; they aren't "not started"
    counts = {k: sum(1 for r in rows if r[1] == k and r[0] in workers)
              for k in ["on track", "at risk", "blocked", "not started"]}
    stale_cut = d - timedelta(days=cfg.stale_days)
    stale = [t for t in open_tasks if (t.date_field("updated") or d) < stale_cut]

    state = ws.state()
    last = state.get("last_report_date")
    since = datetime.fromisoformat(last).date() if last else d - timedelta(days=1)
    already = set(state.get("reported_shipped") or [])       # each shipped task is in exactly one report
    shipped = [t for t in all_tasks if t.status == "done" and (t.date_field("done_on") or d) >= since
               and t.id not in already]
    in_review = [t for t in all_tasks if t.status == "review"]
    blocked = [t for t in open_tasks if t.status == "blocked"]
    overdue = [t for t in open_tasks if t.overdue(tz) and t.status != "blocked"]
    pend = asks.pending()
    owner_blocks = [t for t in blocked if on_owner(cfg, t.blocked_on)]
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
    L += [f"- {n}: {st} — {line}" for n, st, line in rows if n in workers or st != "not started"]
    did = activity(cfg, ws, tasks, d.isoformat())
    L += ["", "## Today's work (from task logs and saved documents)"]
    worked = [(m, did.get(m.id) or []) for m in cfg.workers if not m.monitor]
    L += [f"- {m.name}: " + ("; ".join(items[:6]) + (f" (+{len(items) - 6} more)" if len(items) > 6 else "")
                             if items else "no recorded work") for m, items in worked] or ["- nobody on the team yet"]
    if cfg.projects:
        L += ["", "## Projects"]
        for pid, pr in cfg.projects.items():
            pt = [t for t in all_tasks if t.project == pid]
            done_n = sum(1 for t in pt if t.status == "done")
            blk = sum(1 for t in pt if t.status == "blocked")
            L.append(f"- {pr.name or pid} [{pr.status}]: {done_n}/{len(pt)} done"
                     + (f", {blk} blocked" if blk else "") + (f", {sum(1 for t in pt if t.is_open)} open" if pt else ""))
    if cfg.departments:
        L += ["", "## Departments"]
        for did_, dep in cfg.departments.items():
            people = [m for m in cfg.workers if m.department == did_]
            dt = [t for t in open_tasks if any(t.owner == m.id for m in people)]
            head = cfg.member(dep.head).name if dep.head and cfg.member(dep.head) else "no head"
            L.append(f"- {dep.name} ({head}): {len(people)} people, {len(dt)} open"
                     + (f", {sum(1 for t in dt if t.status == 'blocked')} blocked" if dt else ""))
    if cfg.clients:
        L += ["", "## Clients"]
        for cid, c in cfg.clients.items():
            ps = [p for p in cfg.projects.values() if p.client == cid]
            pt = [t for t in all_tasks if any(t.project == p.id for p in ps)]
            L.append(f"- {c.name}: {len(ps)} project(s), {sum(1 for t in pt if t.status == 'done')}/{len(pt)} tasks done"
                     + (f", {sum(1 for t in pt if t.status == 'blocked')} blocked" if pt else ""))
    decided = [a for a in asks.all() if a.status != "pending"
               and str(a.doc.meta.get("decided_at", ""))[:10] >= since.isoformat()]
    if decided:
        L += ["", "## Decided since last report"]
        L += [f"- {a.id} {a.status} by {a.doc.meta.get('decided_by', '?')}: {a.summary}" for a in decided]
    if stale:
        L += ["", "## Stale (not updated in "
              f"{cfg.stale_days}+ days)"] + [f"- {t.line(tz)} — last update {t.doc.meta.get('updated')}" for t in stale]
    L += ["", "## Critical path"]
    L += [f"- {t.line(tz)}" for t in p0[:3]] or ["- no P0 set — assign one per active project"]
    usage = (state.get("usage") or {}).get(d.isoformat(), {})
    if usage:
        total = sum(int(v) for v in usage.values())
        L += ["", "## Model use today", f"- {total:,} tokens: " + ", ".join(
            f"{('personal' if cfg.member(k) and cfg.member(k).assistant else cfg.member(k).name) if cfg.member(k) else k} "
            f"{int(v):,}" for k, v in sorted(usage.items(),
                                                                                         key=lambda kv: -int(kv[1])))]
    hb = state.get("heartbeat", {})
    errs = [f"- {k}: {v.get('error')}" for k, v in hb.items() if v.get("error")
            and not (cfg.member(k) and cfg.member(k).assistant)]
    if errs:
        L += ["", "## System issues"] + errs
    return "\n".join(L) + "\n"


def write_report(cfg: Config, ws: Workspace, tasks: TaskStore, asks: AskStore) -> tuple[str, str]:
    text = build_report(cfg, ws, tasks, asks)
    rel = f"reports/{today(cfg.timezone).isoformat()}.md"
    ws.write(rel, text)
    shipped = [line.split(" ", 3)[2] for line in text.split("## Shipped since last report", 1)[-1].split("##", 1)[0]
               .splitlines() if line.startswith("- ") and " T-" in line]

    def mark(s):
        s["last_report_date"] = today(cfg.timezone).isoformat()
        s["reported_shipped"] = sorted(set(s.get("reported_shipped") or []) | set(shipped))[-2000:]
    ws.update_state(mark)
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
    all_tasks = tasks.all()
    stale_cut = d - timedelta(days=cfg.stale_days)
    quiet = {pid for pid, p in cfg.projects.items() if p.status in ("paused", "done")}   # no chasing there
    for t in all_tasks:
        if t.project in quiet:
            continue
        since = t.date_field("status_since") or d
        if t.status == "blocked":
            if (d - since).days >= cfg.blocked_escalate_days:
                owners = on_owner(cfg, t.blocked_on)
                alerts.append(Alert(f"blocked:{t.id}", f"🚧 {t.id} ({t.owner}) blocked {(d - since).days} days — "
                                                        f"{t.blocked_on}", nudge="" if owners else t.owner, task=t.id))
        elif t.status == "review":
            if (d - since).days >= REVIEW_WAIT_DAYS:
                alerts.append(Alert(f"review:{t.id}", f"👀 {t.id} ({t.owner}) has waited {(d - since).days} days "
                                                       f"for your review: {t.title}", task=t.id))
        elif t.overdue(tz):
            alerts.append(Alert(f"overdue:{t.id}", f"⚠️ {t.id} ({t.owner}) is overdue (due {t.doc.meta.get('due')}): "
                                                    f"{t.title}", nudge=t.owner, task=t.id))
        elif t.is_open and (t.date_field("updated") or d) < stale_cut:
            alerts.append(Alert(f"stale:{t.id}", f"💤 {t.id} ({t.owner}) hasn't moved in {cfg.stale_days}+ days: "
                                                  f"{t.title}", owner=False, nudge=t.owner, task=t.id))
    busy = {t.owner for t in all_tasks if t.is_open}
    idle = [m for m in cfg.workers if not m.monitor and m.id not in busy]
    if idle and all_tasks:
        alerts.append(Alert(f"idle:{','.join(m.id for m in idle)}",
                            f"🪑 Nothing assigned to {', '.join(m.name for m in idle)} — give them work or pause them."))
    state = ws.state()
    if state.get("push_error"):
        alerts.append(Alert("push", f"⚠️ Team repo can't push to its remote: {state['push_error']}"))
    if state.get("commit_error"):
        alerts.append(Alert("commit", f"⚠️ Team repo commits are failing (changes are saved, not committed): "
                                      f"{state['commit_error']}"))
    if (state.get("github") or {}).get("error"):
        alerts.append(Alert("github", f"⚠️ GitHub sync is failing: {state['github']['error']}"))
    for agent, hb in state.get("heartbeat", {}).items():
        m = cfg.member(agent)
        c = cfg.llm_for(m) if m else None
        current = (c.provider + (f"/{c.model}" if c.model else "")) if c else ""
        if hb.get("model") and current and hb["model"] != current:
            continue                                    # that error was from a model they no longer use
        if hb.get("error"):
            fails = int(hb.get("fails", 1) or 1)
            alerts.append(Alert(f"err:{agent}:{'incident' if fails >= INCIDENT_AFTER_FAILS else 'warn'}",
                                f"🚨 {agent}'s model keeps failing ({fails}×): {hb['error']}"
                                if fails >= INCIDENT_AFTER_FAILS else f"⚠️ {agent}'s last model call failed: {hb['error']}",
                                incident=fails >= INCIDENT_AFTER_FAILS))
    sent = state.get("alerts_sent", {})
    key_day = d.isoformat()
    fresh = [a for a in alerts if sent.get(a.key) != key_day]

    def mark(s):
        s.setdefault("alerts_sent", {})
        s["alerts_sent"] = {k: v for k, v in s["alerts_sent"].items() if v >= (d - timedelta(days=7)).isoformat()}
        for a in fresh:
            s["alerts_sent"][a.key] = key_day
    if fresh:
        ws.update_state(mark)
    return fresh
