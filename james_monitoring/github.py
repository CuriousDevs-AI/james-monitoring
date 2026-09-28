"""GitHub: the task board mirrored into a GitHub Project, and code changes opened as pull requests.

Git (tasks/*.md) stays the engine: agents read the board on every turn, rules are enforced there, it works offline.
GitHub is the window: every task is an issue on a Project board with fields, so you can run the team from
github.com or the GitHub mobile app. Changes you make there come back through the same rules:

    Stage/Status → Done        = you accept it (only you can)
    Stage → Doing (from Review) = send it back for changes
    Stage/Priority/Due/Owner   = the same edit as in the console
    a comment on the issue     = feedback (on work in review: request changes)

Everything goes through the `gh` CLI, logged in as the owner — issues, comments and PRs are the owner's own.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

from .config import Config

log = logging.getLogger("jm.github")

STAGES = {"todo": "To do", "doing": "Doing", "blocked": "Blocked", "review": "Review", "done": "Done", "cut": "Cut"}
FROM_STAGE = {v.lower(): k for k, v in STAGES.items()}
STATUS = {"todo": "Todo", "doing": "In Progress", "blocked": "In Progress", "review": "In Progress",
          "done": "Done", "cut": "Done"}                     # the Project's built-in Status (default board)
FIELDS = [("Stage", "SINGLE_SELECT", list(STAGES.values())), ("Priority", "SINGLE_SELECT", ["P0", "P1", "P2"]),
          ("Owner", "TEXT", None), ("Due", "DATE", None), ("Project", "TEXT", None), ("Blocker", "TEXT", None)]
MARK = "<!-- jm -->"                                          # our own text on GitHub (never read back as feedback)


class GitHubError(Exception):
    pass


class Gh:
    """Runs the gh CLI. Never prompts; errors become GitHubError with gh's own message."""

    def __init__(self, bin: str | None = None):
        self.bin = bin or os.environ.get("JM_GH_BIN") or shutil.which("gh") or ""

    def __call__(self, *args: str, input: str | None = None, cwd=None, timeout: float = 60) -> str:
        if not self.bin:
            raise GitHubError("The GitHub CLI (gh) isn't installed: https://cli.github.com, then `gh auth login`.")
        env = {**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1", "GH_NO_UPDATE_NOTIFIER": "1"}
        try:
            r = subprocess.run([self.bin, *args], input=input, capture_output=True, text=True, cwd=cwd, env=env,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            raise GitHubError(f"gh {' '.join(args[:2])} timed out") from None
        if r.returncode != 0:
            raise GitHubError((r.stderr or r.stdout or "gh failed").strip()[-500:])
        return r.stdout

    def json(self, *args: str, **kw):
        out = self(*args, **kw)
        return json.loads(out) if out.strip() else {}


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


@dataclass
class Change:
    task: str
    field: str        # stage | priority | due | owner | project | comment
    value: str
    source: str = ""  # for stage changes: "stage" (our field) or "status" (GitHub's built-in column)
    raw: str = ""     # the value as shown on GitHub


class GitHubSync:
    def __init__(self, cfg: Config, tasks, ws, gh: Gh | None = None):
        self.cfg, self.tasks, self.ws = cfg, tasks, ws
        self.gh = gh or Gh()
        self._project: dict | None = None

    @property
    def g(self):
        return self.cfg.github

    # -- setup ------------------------------------------------------------------------------
    def auth(self) -> dict:
        """Who gh is logged in as, and whether it may use Projects."""
        try:
            login = self.gh("api", "user", "--jq", ".login").strip()
        except GitHubError as e:
            return {"ok": False, "error": str(e)}
        scopes = ""
        try:
            scopes = self.gh("api", "-i", "user")                      # headers include X-OAuth-Scopes
        except GitHubError:
            pass
        m = re.search(r"(?im)^x-oauth-scopes:\s*(.*)$", scopes)
        have = {x.strip() for x in (m.group(1) if m else "").split(",") if x.strip()}
        return {"ok": True, "login": login, "scopes": sorted(have),
                "projects_ok": bool({"project", "read:project"} & have) or not m,
                "fix": "" if ({"project"} & have or not m) else "gh auth refresh -s project"}

    def create_project(self, owner: str, title: str) -> dict:
        p = self.gh.json("project", "create", "--owner", owner, "--title", title, "--format", "json")
        return {"number": int(p["number"]), "url": p.get("url", ""), "id": p.get("id", "")}

    def project(self) -> dict:
        """The Project's id, url and our fields (created if missing)."""
        if self._project:
            return self._project
        v = self.gh.json("project", "view", str(self.g.project), "--owner", self.g.owner, "--format", "json")
        fields = self._fields()
        for name, kind, options in FIELDS:
            if _key(name) not in fields:
                args = ["project", "field-create", str(self.g.project), "--owner", self.g.owner, "--name", name,
                        "--data-type", kind, "--format", "json"]
                if options:
                    args += ["--single-select-options", ",".join(options)]
                self.gh.json(*args)
                fields = self._fields()
        self._project = {"id": v["id"], "url": v.get("url", ""), "fields": fields}
        return self._project

    def _fields(self) -> dict:
        out = self.gh.json("project", "field-list", str(self.g.project), "--owner", self.g.owner, "--limit", "60",
                           "--format", "json")
        return {_key(f["name"]): f for f in out.get("fields", [])}

    # -- push: tasks → GitHub ---------------------------------------------------------------
    def _state(self) -> dict:
        return (self.ws.state().get("github") or {}).get("items", {})

    def _save(self, tid: str, entry: dict) -> None:
        self.ws.update_state(lambda s: s.setdefault("github", {}).setdefault("items", {}).__setitem__(tid, entry))

    def values(self, t) -> dict:
        """What the board shows for a task (what we compare against to see what *you* changed on GitHub)."""
        return {"stage": STAGES.get(t.status, "To do"), "status": STATUS.get(t.status, "Todo"),
                "priority": t.priority, "owner": self._who(t.owner), "due": str(t.doc.meta.get("due") or ""),
                "project": t.project, "blocker": t.blocked_on}

    def _who(self, mid: str) -> str:
        m = self.cfg.member(mid) if mid else None
        return m.name if m else mid

    def body(self, t) -> str:
        sec = t.doc.sections
        parts = [MARK, f"**{t.id}** · owner **{self._who(t.owner)}** · {t.priority}"
                       + (f" · project `{t.project}`" if t.project else "")]
        for k in ("Goal", "Done means", "Output", "Feedback"):
            if sec.get(k, "").strip():
                parts.append(f"### {k}\n{sec[k].strip()}")
        log_lines = sec.get("Log", "").strip().splitlines()[:12]
        if log_lines:
            parts.append("### Log\n" + "\n".join(log_lines))
        parts.append("---\n_Synced from the team repo. Move it on the board or edit its fields; comment here to give "
                     "feedback (on work in review, a comment sends it back for changes)._")
        return "\n\n".join(parts)

    def _fp(self, t) -> str:
        return hashlib.sha1(json.dumps([t.title, self.values(t), self.body(t)], sort_keys=True).encode()).hexdigest()

    def push(self) -> int:
        """Create or update the issue + board item of every task that changed. Returns how many."""
        if not self.g.enabled:
            return 0
        proj, known, n = self.project(), self._state(), 0
        for t in self.tasks.all():
            entry = known.get(t.id)
            fp = self._fp(t)
            if entry and entry.get("fp") == fp:
                continue
            vals = self.values(t)
            if not entry or not entry.get("url"):
                url = self.gh("issue", "create", "--repo", self.g.repo, "--title", f"[{t.id}] {t.title}",
                              "--body-file", "-", input=self.body(t)).strip().splitlines()[-1]
                entry = {"url": url, "number": int(url.rstrip("/").rsplit("/", 1)[-1]), "item": "", "closed": False,
                         "pushed": {}}
                self._save(t.id, entry)                   # saved at once: a later failure can't duplicate the issue
            if not entry.get("item"):
                item = self.gh.json("project", "item-add", str(self.g.project), "--owner", self.g.owner,
                                    "--url", entry["url"], "--format", "json")
                entry["item"] = item["id"]
                self._save(t.id, entry)
            if entry.get("fp"):
                self.gh("issue", "edit", str(entry["number"]), "--repo", self.g.repo, "--title", f"[{t.id}] {t.title}",
                        "--body-file", "-", input=self.body(t))
            for name, value in vals.items():
                if entry.get("pushed", {}).get(name) != value:
                    self._set_field(proj, entry["item"], name, value)
                    entry.setdefault("pushed", {})[name] = value
                    self._save(t.id, entry)
            closed = t.status in ("done", "cut")
            if closed != entry.get("closed", False):
                if closed:
                    self.gh("issue", "close", str(entry["number"]), "--repo", self.g.repo, "--reason",
                            "completed" if t.status == "done" else "not planned")
                else:
                    self.gh("issue", "reopen", str(entry["number"]), "--repo", self.g.repo)
            entry.update(fp=fp, pushed=vals, closed=closed)
            self._save(t.id, entry)
            n += 1
        return n

    def _set_field(self, proj: dict, item: str, name: str, value: str) -> None:
        f = proj["fields"].get(_key(name))
        if not f:
            return
        base = ["project", "item-edit", "--id", item, "--project-id", proj["id"], "--field-id", f["id"]]
        if not value:
            self.gh(*base, "--clear")
        elif f.get("options"):
            opt = next((o for o in f["options"] if o["name"].lower() == str(value).lower()), None)
            if opt:
                self.gh(*base, "--single-select-option-id", opt["id"])
        elif name == "due":
            self.gh(*base, "--date", value)
        else:
            self.gh(*base, "--text", str(value))

    # -- pull: GitHub → tasks ----------------------------------------------------------------
    def pull(self) -> list[Change]:
        """What you changed on GitHub since the last push: board fields and new comments."""
        if not self.g.enabled:
            return []
        known = self._state()
        by_item = {e["item"]: tid for tid, e in known.items()}
        out: list[Change] = []
        items = self.gh.json("project", "item-list", str(self.g.project), "--owner", self.g.owner, "--limit", "1000",
                             "--format", "json").get("items", [])
        for it in items:
            tid = by_item.get(it.get("id"))
            if not tid:
                continue
            vals = {_key(k): ("" if v is None else str(v)) for k, v in it.items() if not isinstance(v, (dict, list))}
            pushed = known[tid].get("pushed", {})
            stage = vals.get("stage", pushed.get("stage", ""))
            if stage and stage != pushed.get("stage"):
                out.append(Change(tid, "stage", FROM_STAGE.get(stage.lower(), ""), source="stage"))
            elif vals.get("status") and vals["status"] != pushed.get("status"):
                # moved on the default board (built-in Status): map it, and remember it came from Status
                out.append(Change(tid, "stage", {"todo": "todo", "in progress": "doing", "done": "done"}.get(
                    vals["status"].lower(), ""), source="status", raw=vals["status"]))
            for f in ("priority", "owner", "project", "blocker"):
                if f in vals and vals[f] != pushed.get(f, ""):
                    out.append(Change(tid, f, vals[f]))
            if "due" in vals and vals["due"][:10] != pushed.get("due", ""):
                out.append(Change(tid, "due", vals["due"][:10]))
        out += self._new_comments(known)
        return [c for c in out if c.field != "stage" or c.value]

    def _new_comments(self, known: dict) -> list[Change]:
        g = self.ws.state().get("github") or {}
        since = g.get("comments_since", "")
        by_number = {e["number"]: tid for tid, e in known.items()}
        path = f"repos/{self.g.repo}/issues/comments?sort=created&direction=asc&per_page=100" + \
            (f"&since={since}" if since else "")
        comments = self.gh.json("api", path) or []
        out, last = [], since
        for c in comments:
            last = max(last, c.get("created_at", ""))
            if since and c.get("created_at", "") <= since:
                continue
            num = int(str(c.get("issue_url", "")).rstrip("/").rsplit("/", 1)[-1] or 0)
            body = (c.get("body") or "").strip()
            if num in by_number and body and MARK not in body:
                out.append(Change(by_number[num], "comment", body))
        if last != since:
            self.ws.update_state(lambda s: s.setdefault("github", {}).__setitem__("comments_since", last))
        return out

    def mark_pulled(self, tid: str, field: str, value: str) -> None:
        """Record a GitHub-side value we have applied, so it isn't seen as a new change again."""
        known = self._state()
        e = known.get(tid)
        if e:
            e.setdefault("pushed", {})[field] = value
            self._save(tid, e)

    def restore(self, tid: str, field: str, shown: str) -> None:
        """A change the rules refused: record what GitHub shows now and force the next push to put the real value
        back (the fingerprint alone wouldn't notice — the task itself didn't change)."""
        known = self._state()
        e = known.get(tid)
        if e:
            e.setdefault("pushed", {})[field] = shown
            e["fp"] = ""
            self._save(tid, e)

    def url_of(self, tid: str) -> str:
        return (self._state().get(tid) or {}).get("url", "")

    # -- pull requests (code tasks) -------------------------------------------------------------
    def open_pr(self, repo_dir, branch: str, base: str, title: str, body: str) -> str:
        out = self.gh("pr", "create", "--head", branch, "--base", base, "--title", title, "--body-file", "-",
                      input=body + f"\n\n{MARK}", cwd=repo_dir, timeout=120)
        return out.strip().splitlines()[-1]

    def merge_pr(self, repo_dir, url: str) -> None:
        self.gh("pr", "merge", url, "--merge", "--delete-branch", cwd=repo_dir, timeout=120)

    def close_pr(self, repo_dir, url: str, comment: str) -> None:
        self.gh("pr", "close", url, "--comment", comment + f"\n\n{MARK}", cwd=repo_dir, timeout=60)
