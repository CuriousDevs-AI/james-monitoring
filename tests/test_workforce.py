"""The workforce platform features: search, memory editor, audit log, roles and sign-in links, threads, task
reassign/dependencies, the personal assistant, departments, clients, notifications, health, Agent Studio, retries."""
import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from james_monitoring.llm import LLMError, LLMResult
from james_monitoring.llm.fake import FakeLLM
from james_monitoring.server import App, make_handler

KEY = "owner-key-123"


@pytest.fixture
def app(tmp_path):
    a = App(tmp_path / "co", telegram=False)
    a.base.mkdir(parents=True)
    a.load()
    a.setup({"company": "Acme", "owner": "Maria Lopez", "provider": "fake", "manager_name": "Nova"})
    a.admin.add(name="Riya", role="Backend lead", token="")
    a.admin.add(name="Sam", role="Designer", token="")
    a.load()
    a.project_save({"name": "Site", "lead": "riya", "members": ["riya", "sam"]})
    a.rt.llm = FakeLLM()
    yield a
    a.submit(a._stop_services(a.sched, a.gw, a.slack), timeout=10)
    a.loop.call_soon_threadsafe(a.loop.stop)


@pytest.fixture
def http(app):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, KEY))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, body=None, key=KEY, raw=False):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-JM-Key": key, "Content-Type": "application/json"},
                                     method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                return r.status, (data.decode() if raw else json.loads(data))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    yield call
    srv.shutdown()


def settle(app, fn, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("timed out")


# -- search -----------------------------------------------------------------------------------------
def test_search_everything_and_only_what_you_may_see(app):
    app.chat_send("riya", "the pelican migration is due friday")
    settle(app, lambda: len(app.chat.since("riya", -1)) >= 2)
    t = app.task_create({"title": "Pelican data import", "owner": "riya", "notify": False})
    app.rt.ws.write("docs/site/pelican.md", "# Notes\nThe pelican schema has three tables.\n")
    app.rt.ws.remember("sam", "Maria prefers pelican-blue in mockups")
    r = app.search("pelican")
    assert any(m["room"] == "riya" for m in r["messages"])
    assert r["tasks"][0]["id"] == t["id"]
    assert r["docs"][0]["path"] == "docs/site/pelican.md"
    assert r["memory"][0]["member"] == "sam"
    viewer = {"id": "v", "name": "Vic", "role": "viewer", "client": ""}
    r2 = app.search("pelican", viewer)                     # the founder's 1:1s and memory aren't for viewers
    assert not any(m["room"] == "riya" for m in r2["messages"]) and not r2["memory"] and r2["tasks"]
    assert app.search("p")["tasks"] == []                  # too short: nothing


# -- memory editor ------------------------------------------------------------------------------------
def test_memory_editor_add_pin_edit_delete_keeps_handwritten(app):
    ws = app.rt.ws
    ws.write("team/riya/memory.md", "# Memory — riya\n\nWritten by hand: Riya owns the billing code.\n")
    app.memory_edit({"id": "riya", "action": "add", "text": "Use Postgres 16"})
    app.memory_edit({"id": "riya", "action": "add", "text": "Never deploy on Friday", "pinned": True})
    e = {x["text"]: x for x in app.memory_get("riya")["entries"]}
    assert e["Never deploy on Friday"]["pinned"] and not e["Use Postgres 16"]["pinned"]
    app.memory_edit({"id": "riya", "action": "pin", "entry": e["Use Postgres 16"]["id"]})
    e = {x["text"]: x for x in app.memory_get("riya")["entries"]}
    assert e["Use Postgres 16"]["pinned"]
    app.memory_edit({"id": "riya", "action": "edit", "entry": e["Use Postgres 16"]["id"], "text": "Use Postgres 17"})
    e = {x["text"]: x for x in app.memory_get("riya")["entries"]}
    with pytest.raises(ValueError, match="changed or is gone"):     # stale id: never edits the wrong line
        app.memory_edit({"id": "riya", "action": "delete", "entry": "0123456789ab"})
    app.memory_edit({"id": "riya", "action": "delete", "entry": e["Never deploy on Friday"]["id"]})
    text = ws.read("team/riya/memory.md")
    assert "Written by hand: Riya owns the billing code." in text and "Postgres 17" in text and "Friday" not in text
    assert "Postgres 17" in ws.memory("riya")                          # what the model reads
    assert "memory" in ws.git_log(3)


# -- audit, roles, sign-in links (over HTTP) ----------------------------------------------------------
def test_roles_and_audit_over_http(app, http):
    st, r = http("/api/users", {"action": "add", "name": "Tom Admin", "role": "admin"})
    assert st == 200 and r["key"].startswith("u_")
    admin_key = r["key"]
    member_key = http("/api/users", {"action": "add", "name": "Mia", "role": "member"})[1]["key"]
    viewer_key = http("/api/users", {"action": "add", "name": "Vik", "role": "viewer"})[1]["key"]
    raw = app.cfg_path.read_text()
    assert admin_key not in raw and member_key not in raw          # only hashes are stored

    assert http("/api/state", key="u_wrong")[0] == 403
    assert http("/api/state", key=member_key)[1]["me"]["role"] == "member"
    # a member talks in All hands (as themselves), not in the founder's 1:1s, and can't approve or change settings
    assert http("/api/chat", {"room": "team", "text": "hello from Mia"}, key=member_key)[0] == 200
    msg = settle(app, lambda: [m for m in app.chat.since("team", -1) if m["text"] == "hello from Mia"])[0]
    assert msg["who"] == "mia"
    assert http("/api/chat", {"room": "riya", "text": "psst"}, key=member_key)[0] == 403
    assert http("/api/chat?room=riya", key=member_key)[0] == 403
    assert http("/api/settings", key=member_key)[0] == 403
    assert http("/api/ask", {"id": "ASK-001", "decision": "approved"}, key=member_key)[0] == 403
    assert http("/api/chat", {"room": "team", "text": "/cut T-001"}, key=member_key)[0] == 200
    settle(app, lambda: any("Only Maria Lopez can run commands" in m["text"] for m in app.chat.since("team", -1)))
    # a member can create and move tasks, but not accept them
    st, t = http("/api/tasks", {"title": "Hero image", "owner": "sam", "notify": False}, key=member_key)
    assert st == 200
    assert http("/api/task", {"id": t["id"], "action": "accept"}, key=member_key)[0] == 403
    # a viewer reads, never writes
    assert http("/api/dashboard", key=viewer_key)[0] == 200
    assert http("/api/chat", {"room": "team", "text": "hi"}, key=viewer_key)[0] == 403
    # an admin runs things, but can't make sign-in links
    assert http("/api/settings", key=admin_key)[0] == 200
    assert http("/api/users", {"action": "add", "name": "X", "role": "admin"}, key=admin_key)[0] == 403
    # rotating a link kills the old one
    http("/api/users", {"action": "rotate", "id": "vik"})
    assert http("/api/state", key=viewer_key)[0] == 403

    # the audit log: who did what — with no secrets in it
    http("/api/model_key", {"env": "OPENROUTER_API_KEY", "key": "sk-secret-value"}, key=admin_key)
    st, rows = http("/api/audit")
    assert st == 200
    acts = [(x["who"], x["action"]) for x in rows]
    assert ("Mia", "create task") in acts and ("Maria Lopez", "sign-in links") in acts
    assert ("Tom Admin", "save API key") in acts
    assert "sk-secret-value" not in json.dumps(rows) and "u_" not in json.dumps(rows)
    st, csv = http("/api/audit.csv", raw=True)
    assert st == 200 and csv.startswith("at,kind,who,action") and "Mia" in csv
    assert http("/api/audit", key=member_key)[0] == 403


def test_decisions_from_any_channel_are_audited(app):
    ask = app.rt.asks.create(requester="riya", summary="Buy a domain", details="", level="red", task="", kind="")
    app.decide(ask.id, "approved", "fine")
    rows = app.rt.audit.entries(kind="decision")
    assert rows[0]["target"] == ask.id and rows[0]["action"] == "approved" and "Buy a domain" in rows[0]["detail"]


def test_private_assistant_room_is_the_founders_alone(app, http):
    app.member_update({"id": "sam", "assistant": True})
    admin_key = http("/api/users", {"action": "add", "name": "Tom", "role": "admin"})[1]["key"]
    assert http("/api/chat?room=sam", key=admin_key)[0] == 403
    assert "sam" not in [m["id"] for m in http("/api/state", key=admin_key)[1]["members"]]
    assert http("/api/chat?room=sam")[0] == 200                      # the founder


def test_client_portal_sees_only_its_projects(app, http):
    app.clients_save({"name": "Globex", "contact": "hank@globex.test", "projects": ["site"]})
    app.project_save({"name": "Internal tools"})
    app.task_create({"title": "Launch page", "owner": "riya", "project": "site", "notify": False})
    t2 = app.task_create({"title": "Payroll script", "owner": "riya", "project": "internal_tools", "notify": False})
    app.rt.tasks.set_status(t2["id"], "blocked", by="riya", blocked_on="Maria — payroll export")
    key = http("/api/users", {"action": "add", "name": "Hank", "role": "client", "client": "globex"})[1]["key"]
    st, s = http("/api/state", key=key)
    assert s["me"]["role"] == "client" and s["client"]["name"] == "Globex" and "members" not in s
    st, p = http("/api/portal", key=key)
    assert [x["id"] for x in p["projects"]] == ["site"] and "Launch page" in p["report"]
    assert "Payroll" not in json.dumps(p) and "Riya" not in p["report"]
    for path in ("/api/dashboard", "/api/tasks", "/api/chat?room=team", "/api/project?id=internal_tools"):
        assert http(path, key=key)[0] == 403, path
    assert http("/api/client_report?id=globex", key=key)[0] == 403        # the portal carries it


# -- threads --------------------------------------------------------------------------------------------
def test_threads_reply_and_agents_answer_in_the_thread(app):
    app.rt.llm.push({"reply": "Here is the plan.", "actions": []})
    first = app.chat_send("riya", "Plan for the API?")["i"]
    settle(app, lambda: len(app.chat.since("riya", -1)) >= 2)
    app.rt.llm.push({"reply": "Tuesday.", "actions": []})
    r = app.chat_send("riya", "When will it be done?", reply_to=first)
    settle(app, lambda: len(app.chat.since("riya", -1)) >= 4)
    msgs = app.chat.since("riya", -1)
    mine = msgs[r["i"]]
    assert mine["reply_to"] == first and mine["thread"] == first and mine["reply_quote"].startswith("Maria Lopez:")
    answer = msgs[-1]
    assert answer["who"] == "riya" and answer["thread"] == first and answer["reply_to"] == r["i"]
    assert [m["i"] for m in app.chat_thread("riya", first)["messages"]] == [first, r["i"], answer["i"]]
    system, sent = app.rt.llm.calls[-1]
    assert "replying to “Maria Lopez: Plan for the API?”" in sent[-1]["content"]
    # in a project room, replying to Sam's message goes to Sam (not the lead)
    app.rt.llm.push({"reply": "Hi from Sam.", "actions": []})
    app.chat_send("p-site", "@sam can you do the hero?")
    settle(app, lambda: len(app.chat.since("p-site", -1)) >= 2)
    sam_msg = app.chat.since("p-site", -1)[-1]
    assert sam_msg["who"] == "sam"
    app.rt.llm.push({"reply": "Sure, Friday.", "actions": []})
    app.chat_send("p-site", "and when?", reply_to=sam_msg["i"])
    settle(app, lambda: len(app.chat.since("p-site", -1)) >= 4)
    assert app.chat.since("p-site", -1)[-1]["who"] == "sam"


# -- tasks: reassign, dependencies, history -------------------------------------------------------------
def test_reassign_tells_both_and_dependencies_are_checked(app):
    a = app.task_create({"title": "Schema", "owner": "riya", "notify": False})
    b = app.task_create({"title": "Mockups", "owner": "riya", "notify": False})
    app.rt.llm.push({"reply": "On it.", "actions": []})
    r = app.task_action({"id": b["id"], "action": "edit", "owner": "sam"})
    assert "reassigned to Sam" in r["message"]
    settle(app, lambda: any("moved to Sam" in m["text"] for m in app.chat.since("riya", -1)))
    with pytest.raises(Exception, match="can't depend"):
        app.task_action({"id": b["id"], "action": "edit", "depends_on": ["T-999"]})
    app.task_action({"id": b["id"], "action": "edit", "depends_on": [a["id"]], "reviewer": "riya"})
    d = app.task_detail(b["id"])
    assert d["depends_on"] == [a["id"]] and d["reviewer"] == "riya" and d["deps"][0]["title"] == "Schema"
    assert app.task_detail(a["id"])["needed_by"][0]["id"] == b["id"]
    assert len(d["history"]) >= 2 and all(h["hash"] for h in d["history"])
    assert all(a["id"] not in h["subject"] for h in d["history"])      # never another task's commits


# -- personal assistant -----------------------------------------------------------------------------------
def test_personal_assistant_reminders_brief_and_privacy(app):
    from james_monitoring.assistant import Reminders, parse_when
    from james_monitoring.util import now
    app.studio_hire({"name": "Ada", "role": "Personal assistant", "assistant": True})
    rt = app.rt
    assert rt.cfg.member("ada").assistant and "ada" not in [m.id for m in rt.cfg.workers]
    base = now(rt.cfg.timezone).replace(hour=10, minute=0)
    from datetime import timedelta as _td
    assert parse_when("in 2h", rt.cfg.timezone, base) - base == _td(hours=2)
    assert parse_when("09:30", rt.cfg.timezone, base).day != base.day             # passed today → tomorrow
    with pytest.raises(ValueError):
        parse_when("sometime", "UTC")
    # the assistant sets a reminder through its action; it fires once
    rt.llm.push({"reply": "Done.", "actions": [{"type": "remind", "at": "in 1m", "text": "Call the bank"}]})
    app.chat_send("ada", "remind me to call the bank in a minute")
    settle(app, lambda: Reminders(rt).all())
    later = now(rt.cfg.timezone).replace(microsecond=0)
    from datetime import timedelta
    assert app.submit(rt.run_reminders(later + timedelta(minutes=2))) == 1
    settle(app, lambda: any("⏰ Reminder: Call the bank" in m["text"] for m in app.chat.since("ada", -1)))
    assert app.submit(rt.run_reminders(later + timedelta(minutes=3))) == 0
    # other teammates can't set reminders, nor reach the assistant
    rt.llm.push({"reply": "ok", "actions": [{"type": "remind", "at": "in 5m", "text": "x"},
                                            {"type": "message_agent", "to": "ada", "text": "hi"}]})
    rt.llm.push({"reply": "ok", "actions": []})
    app.chat_send("riya", "hello")
    settle(app, lambda: any("personal assistant" in m["text"] for m in app.chat.since("riya", -1)))
    # personal tasks: private, not in the company report or @all
    t = app.task_create({"title": "Renew passport", "owner": "ada", "notify": False})
    assert t["personal"]
    viewer = {"id": "v", "name": "V", "role": "viewer", "client": ""}
    assert not app.can_see_task(viewer, rt.tasks.get(t["id"]))
    from james_monitoring.router import mentions
    assert "ada" not in mentions("@all status", rt.cfg)[1]
    app.submit(rt.run_daily_report())
    rep = sorted((rt.ws.root / "reports").glob("*.md"))[-1].read_text()
    assert "Renew passport" not in rep and "Ada" not in rep
    assert app.dashboard()["my_day"]["tasks"][0]["title"] == "Renew passport"
    app.submit(rt.run_brief())
    settle(app, lambda: any("Good morning" in m["text"] and "Renew passport" in m["text"]
                            for m in app.chat.since("ada", -1)))


# -- departments and clients ------------------------------------------------------------------------------
def test_departments_route_mentions_and_show_in_report(app):
    app.departments_save({"name": "Engineering", "head": "riya", "members": ["riya"]})
    rt = app.rt
    assert rt.cfg.member("riya").department == "engineering" and rt.cfg.departments["engineering"].head == "riya"
    from james_monitoring.router import mentions
    assert mentions("@engineering please check", rt.cfg) == (False, ["riya"])
    assert "Engineering (Riya)" in rt.roster() or "Engineering (head)" in rt.roster()
    app.submit(rt.run_daily_report())
    rep = sorted((rt.ws.root / "reports").glob("*.md"))[-1].read_text()
    assert "## Departments" in rep and "Engineering (Riya)" in rep
    app.departments_save({"id": "engineering", "action": "delete"})
    assert app.rt.cfg.member("riya").department == ""


def test_client_report_is_client_safe(app):
    app.clients_save({"name": "Globex", "projects": ["site"]})
    t = app.task_create({"title": "Launch page", "owner": "riya", "project": "site", "notify": False})
    app.rt.tasks.set_status(t["id"], "blocked", by="riya", blocked_on="Maria — copy for the hero")
    rep = app.client_report("globex")["text"]
    assert "Launch page" in rep and "Waiting on something" in rep and "Maria" not in rep and "Riya" not in rep
    assert app.clients()[0]["projects"][0]["id"] == "site"
    with pytest.raises(ValueError, match="sign-in links"):
        app.users_save({"action": "add", "name": "Hank", "role": "client", "client": "globex"})
        app.clients_save({"id": "globex", "action": "delete"})


# -- notifications, health, studio, retries ------------------------------------------------------------------
def test_notifications_collect_and_mark_read(app):
    ask = app.rt.asks.create(requester="riya", summary="Pay for hosting", details="", level="red", task="", kind="")
    t = app.task_create({"title": "Copy", "owner": "sam", "notify": False})
    app.rt.tasks.set_status(t["id"], "review", by="sam")
    app.rt.chat.append("p-site", "sam", "@maria can you look at the hero copy?")
    n = app.notifications()
    kinds = {x["kind"] for x in n["items"]}
    assert {"approval", "review", "mention"} <= kinds and n["unread"] >= 3
    app.notifications_read({"ids": [f"ask:{ask.id}"]})
    n2 = app.notifications()
    assert n2["unread"] == n["unread"] - 1
    assert app.notifications_read({"all": True})["unread"] == 0


def test_health_reports_every_part(app):
    h = app.health()
    names = {c["name"] for c in h["checks"]}
    assert {"Team engine", "Scheduler", "Team repo (git)", "Disk space", "AI models", "Queue"} <= names
    assert next(c for c in h["checks"] if c["name"] == "Team engine")["ok"]


def test_studio_try_and_hire(app):
    tpl = app.studio_templates()
    assert any(t["id"] == "assistant" and t.get("assistant") for t in tpl["templates"]) and tpl["skills"]
    app.rt.llm.push({"reply": "I'd start with the test plan.", "actions": [
        {"type": "create_task", "title": "Test plan", "owner": "me"}]})
    import james_monitoring.server as srv
    orig = srv.make_llm
    srv.make_llm = lambda conf: app.rt.llm
    try:
        r = app.studio_try({"name": "Quinn", "role": "QA engineer", "mission": "Nothing ships broken.",
                            "message": "What would you do first?"})
    finally:
        srv.make_llm = orig
    assert r["ok"] and "test plan" in r["reply"] and r["would_do"] and not app.rt.tasks.all()   # nothing ran
    h = app.studio_hire({"name": "Quinn", "role": "QA engineer", "mission": "Nothing ships broken.",
                         "responsibilities": ["Test plans"], "skills": ["docs"], "rules": ["Steps to reproduce"]})
    persona = app.rt.ws.read(f"team/{h['id']}/persona.md")
    assert "## Mission" in persona and "Nothing ships broken." in persona and "Steps to reproduce" in persona
    assert app.rt.cfg.member("quinn").permissions.get("run_code") == "red"
    from james_monitoring.studio import parse_persona
    back = parse_persona(persona)
    assert back["mission"] == "Nothing ships broken." and back["skills"] == ["docs"]


def test_transient_model_errors_are_retried(app, monkeypatch):
    import james_monitoring.runtime as rtmod
    monkeypatch.setattr(rtmod, "RETRY_BACKOFF", (0, 0))

    class Flaky:
        name = "flaky"

        def __init__(self):
            self.n = 0

        def complete(self, system, messages):
            self.n += 1
            if self.n == 1:
                raise LLMError("HTTP 529 overloaded — try again")
            return LLMResult(text='{"reply": "fine now", "actions": []}', input_tokens=1, output_tokens=1)
    flaky = Flaky()
    app.rt.llm = flaky
    app.chat_send("riya", "status?")
    settle(app, lambda: any(m["text"] == "fine now" for m in app.chat.since("riya", -1)))
    assert flaky.n == 2

    class Dead(Flaky):
        def complete(self, system, messages):
            self.n += 1
            raise LLMError("Not logged in")
    dead = Dead()
    app.rt.llm = dead
    app.chat_send("riya", "again?")
    settle(app, lambda: any("couldn't think" in m["text"] for m in app.chat.since("riya", -1)))
    assert dead.n == 1                                        # a login problem isn't retried


# -- project settings: a project's own model, rules and instructions, for everyone or per person ------------------
def test_project_settings_override_company_and_person(app, monkeypatch):
    import james_monitoring.runtime as rtmod
    app.member_update({"id": "sam", "permissions": {"write_file": "yellow"}})
    r = app.project_settings_save({
        "id": "site", "instructions": "Client is Globex. British spelling.",
        "permissions": {"write_file": "red"},
        "agents": {"riya": {"role": "Tech reviewer", "instructions": "Review every PR within a day.",
                            "permissions": {"write_file": "green"}, "llm": {"provider": "fake", "model": "riya-site"}}}})
    cfg = app.rt.cfg
    riya, sam = cfg.member("riya"), cfg.member("sam")
    # most specific wins: person-on-project → project → person → company
    assert cfg.permission(riya, "write_file", "site") == "green"
    assert cfg.permission(sam, "write_file", "site") == "red"          # the project's rule beats Sam's own
    assert cfg.permission(sam, "write_file") == "yellow"                # elsewhere Sam's own still applies
    assert cfg.llm_for(riya, "site").model == "riya-site" and cfg.llm_source(riya, "site") == "person_project"
    assert cfg.llm_source(riya) == "company" and cfg.llm_source(sam, "site") == "company"
    people = {x["id"]: x for x in r["people"]}
    assert people["sam"]["permissions"]["write_file"] == {"level": "red", "source": "project"}
    assert people["riya"]["model_source"] == "person_project"

    # in the project room Riya runs on her project model and reads the project's settings
    made = {}
    orig = rtmod.make_llm

    def fake_make(conf):
        made["model"] = conf.model
        return app.rt.llm
    monkeypatch.setattr(rtmod, "make_llm", fake_make)
    app.rt.llm.push({"reply": "ok", "actions": []})
    app.chat_send("p-site", "@riya status of the review?")
    settle(app, lambda: len(app.chat.since("p-site", -1)) >= 2)
    assert made["model"] == "riya-site"
    system, _ = app.rt.llm.calls[-1]
    assert "your role is: Tech reviewer" in system and "British spelling" in system and "within a day" in system
    # …but in her 1:1 she's on the company model, without the project's instructions
    app.rt.llm.push({"reply": "ok", "actions": []})
    app.chat_send("riya", "hi")
    settle(app, lambda: len(app.chat.since("riya", -1)) >= 2)
    assert "British spelling" not in app.rt.llm.calls[-1][0]
    monkeypatch.setattr(rtmod, "make_llm", orig)

    # Sam's write_file in the project room becomes an approval card (the project says "ask me first")
    app.rt.llm.push({"reply": "Drafted.", "actions": [{"type": "write_file", "path": "docs/site/copy.md", "content": "x"}]})
    app.chat_send("p-site", "@sam write the copy doc")
    settle(app, lambda: app.rt.asks.pending())
    assert "write docs/site/copy.md" in app.rt.asks.pending()[0].summary

    # clearing falls back; bad input is refused
    app.project_settings_save({"id": "site", "permissions": {}, "agents": {}})
    assert app.rt.cfg.permission(app.rt.cfg.member("sam"), "write_file", "site") == "yellow"
    with pytest.raises(ValueError, match="OpenCode model"):
        app.project_settings_save({"id": "site", "llm": {"provider": "opencode", "model": "glm"}})
    with pytest.raises(ValueError, match="isn't on the team"):
        app.project_settings_save({"id": "site", "agents": {"ghost": {"role": "x"}}})


def test_project_model_problems_show_in_health(app, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    app.project_settings_save({"id": "site", "llm": {"provider": "openrouter", "model": "z-ai/glm-4.6"}})
    h = app.models_health()
    assert not h["ok"] and any("Riya on Site" in p["who"] for p in h["problems"])


# -- pause a project; removing someone leaves nothing dangling --------------------------------------------------
def test_pause_a_project_queues_its_work_and_resume_answers_it(app):
    rt = app.rt
    t = app.task_create({"title": "Hero copy", "owner": "sam", "project": "site", "notify": False,
                         "done_means": ["copy doc"]})
    other = app.task_create({"title": "Payroll", "owner": "sam", "notify": False, "done_means": ["done"]})
    r = app.project_pause("site", True)
    assert "paused" in r["message"] and app.rt.cfg.projects["site"].status == "paused"
    rt = app.rt
    assert rt.next_work(rt.cfg.member("sam"))[1].id == other["id"]          # only work outside the paused project
    app.chat_send("p-site", "@sam how is the hero going?")
    settle(app, lambda: any("is paused" in m["text"] and m["who"] == "sam" for m in app.chat.since("p-site", -1)))
    assert rt.queued() and rt.queued()[0]["member"] == "sam"
    assert app.submit(rt.drain_queue()) == 0                                # still paused: stays queued
    # nor can a coding run start there
    with pytest.raises(ValueError, match="paused"):
        app.submit(rt._do(rt.cfg.member("sam"), {"type": "run_code", "task": t["id"], "project": "site",
                                                 "instructions": "x"}, __import__("james_monitoring.runtime", fromlist=["Event"]).Event("dm", "x", sender="maria_lopez")))
    rt.llm.push({"reply": "Hero is half done.", "actions": []})
    r = app.project_pause("site", False)
    assert "running again" in r["message"] and "1 waiting message" in r["message"]
    settle(app, lambda: any(m["text"].startswith("(answering what came in while Site was paused)") and m["who"] == "sam"
                            for m in app.chat.since("p-site", -1)))       # answered after the resume (a reload)
    assert not app.rt.queued()


def test_removing_someone_cleans_every_reference(app):
    app.departments_save({"name": "Design", "head": "sam", "members": ["sam"]})
    app.project_settings_save({"id": "site", "agents": {"sam": {"role": "Art director"}}})
    t = app.task_create({"title": "Logo", "owner": "sam", "project": "site", "notify": False})
    r = app.remove_person("sam")                          # used to fail: config refused the dangling references
    assert r["removed"] == "Sam" and r["tasks_moved"] == [t["id"]]
    cfg = app.rt.cfg
    assert not cfg.member("sam") and cfg.departments["design"].head == "" and "sam" not in cfg.projects["site"].agents
    task = app.rt.tasks.get(t["id"])
    assert task.owner == cfg.monitor.id and "left the team" in task.doc.sections["Log"]
    settle(app, lambda: any("Their open tasks are with me" in m["text"] for m in app.chat.since(cfg.monitor.id, -1)))


# -- the review's findings stay fixed ------------------------------------------------------------------------------
def test_roles_cant_bend_the_rules(app, http):
    member = http("/api/users", {"action": "add", "name": "Mia", "role": "member"})[1]["key"]
    admin = http("/api/users", {"action": "add", "name": "Tom", "role": "admin"})[1]["key"]
    t = app.task_create({"title": "Copy", "owner": "sam", "project": "site", "notify": False})
    app.rt.tasks.set_status(t["id"], "review", by="sam")
    # a member's "feedback" would be a founder's binding correction → refused
    assert http("/api/task", {"id": t["id"], "action": "feedback", "text": "redo"}, key=member)[0] == 403
    assert app.rt.tasks.get(t["id"]).status == "review"
    # a member can't reshape a project's team by assigning an outsider
    app.admin.add(name="Leo", role="Marketing", token=""); app.load()
    assert http("/api/tasks", {"title": "x", "owner": "leo", "project": "site", "notify": False}, key=member)[0] == 403
    # only the founder sets up (or unmasks) the personal assistant
    app.member_update({"id": "sam", "assistant": True})
    assert http("/api/member", {"id": "sam", "assistant": False}, key=admin)[0] == 403
    assert http("/api/memory", {"id": "sam", "action": "add", "text": "x"}, key=admin)[0] == 403
    assert http("/api/memory?id=riya", key=member)[0] == 403                       # memory is for admins
    assert http("/api/member?id=riya", key=member)[1]["memory"] == ""
    # the assistant's requests are private too
    app.rt.asks.create(requester="sam", summary="Buy a gift", details="", level="red", task="", kind="")
    assert all(a["from"] != "sam" for a in http("/api/asks", key=admin)[1])
    assert any(a["from"] == "sam" for a in http("/api/asks")[1])
    # …and nobody can hand company work to it but the founder
    t2 = app.task_create({"title": "Logo", "owner": "riya", "notify": False})
    assert http("/api/task", {"id": t2["id"], "action": "edit", "owner": "sam"}, key=admin)[0] == 403


def test_project_rules_follow_the_action_not_the_room(app):
    app.project_settings_save({"id": "site", "permissions": {"create_task": "red"}})
    app.rt.llm.push({"reply": "Done.", "actions": [{"type": "create_task", "title": "Site banner", "project": "site",
                                                    "owner": "riya"}]})
    app.chat_send("riya", "add a banner task for the site")          # a 1:1, not the project room
    settle(app, lambda: app.rt.asks.pending())
    assert "Site banner" in app.rt.asks.pending()[0].summary and not [t for t in app.rt.tasks.all()
                                                                        if t.title == "Site banner"]


def test_a_failed_studio_hire_leaves_nothing_behind(app):
    with pytest.raises(ValueError, match="department"):
        app.studio_hire({"name": "Quinn", "role": "QA", "department": "nope"})
    assert not app.rt.cfg.member("quinn")
    import james_monitoring.server as srv
    orig = srv.App._studio_finish
    srv.App._studio_finish = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))
    try:
        with pytest.raises(RuntimeError):
            app.studio_hire({"name": "Quinn", "role": "QA"})
    finally:
        srv.App._studio_finish = orig
    assert not app.rt.cfg.member("quinn")
    assert app.studio_hire({"name": "Quinn", "role": "QA"})["id"] == "quinn"      # and hiring again works


def test_reminders_survive_a_failed_send_and_times_parse(app):
    from datetime import timedelta
    from james_monitoring.assistant import Reminders, parse_when
    from james_monitoring.util import now
    app.studio_hire({"name": "Ada", "role": "Assistant", "assistant": True})
    rt = app.rt
    Reminders(rt).add("ada", "in 1m", "Call the bank")
    later = now(rt.cfg.timezone) + timedelta(minutes=2)
    real = rt.bus.send_owner

    async def boom(*a, **k):
        raise RuntimeError("network down")
    rt.bus.send_owner = boom
    assert app.submit(rt.run_reminders(later)) == 0 and len(Reminders(rt).all()) == 1   # kept for the next tick
    rt.bus.send_owner = real
    assert app.submit(rt.run_reminders(later)) == 1 and not Reminders(rt).all()
    base = now(rt.cfg.timezone)
    assert parse_when("tomorrow", rt.cfg.timezone, base).hour == 9
    assert parse_when("2030-01-02T09:30Z", rt.cfg.timezone).utcoffset().total_seconds() == 0


def test_audit_never_keeps_credentials_and_csv_is_safe(app):
    app.rt.audit.record("console", "Maria", "set up", "https://bob:tok@github.com/x",
                        {"clone_url": "https://bob:ghp_abcdefghijklmnopqrstuv@github.com/x",
                         "llm": {"provider": "openai", "api_key": "sk-live-123456789"}, "title": "=HYPERLINK(1)"})
    row = app.rt.audit.entries(limit=1)[0]
    blob = json.dumps(row)
    assert "tok@" not in blob and "ghp_" not in blob and "sk-live" not in blob and "***@github.com" in blob
    csv = app.rt.audit.csv()
    assert "\n'=" not in csv.split("\n", 1)[0] and ",=" not in csv


def test_a_hung_turn_is_stopped_without_blocking_the_next(app, monkeypatch):
    import james_monitoring.runtime as rtmod
    monkeypatch.setattr(rtmod, "WATCHDOG_SECONDS", 1)

    class Slow:
        name = "slow"

        def complete(self, system, messages):
            time.sleep(3)
            return LLMResult(text='{"reply": "late", "actions": []}', input_tokens=1, output_tokens=1)
    app.rt.llm = Slow()
    app.chat_send("riya", "hello?")
    settle(app, lambda: any("I got stuck" in m["text"] for m in app.chat.since("riya", -1)))
    assert app.rt.ws.state().get("stuck") and "riya" not in app.rt.turns


def test_a_guessed_task_id_in_a_reply_is_corrected(app):
    app.rt.llm.push({"reply": "Created T-009 for the hero.", "actions": [{"type": "create_task", "title": "Hero",
                                                                          "owner": "riya"}]})
    app.chat_send("riya", "make a task for the hero")
    msg = settle(app, lambda: [m for m in app.chat.since("riya", -1) if m["who"] == "riya"])[0]
    assert "T-009" not in msg["text"] and msg["text"].startswith("Created T-001")


def test_an_approval_asked_in_a_project_room_shows_there(app):
    app.project_settings_save({"id": "site", "permissions": {"create_task": "red"}})
    app.rt.llm.push({"reply": "Needs your OK.", "actions": [{"type": "create_task", "title": "FAQ copy", "owner": "riya"}]})
    app.chat_send("p-site", "@riya add an FAQ task")
    card = settle(app, lambda: [m for m in app.chat.since("p-site", -1) if m.get("kind") == "ask"])[0]
    ask_id = card["ask_id"]
    app.decide(ask_id, "approved")
    assert any(t.title == "FAQ copy" for t in app.rt.tasks.all())


def test_report_sees_a_block_on_the_founders_first_name(app):
    t = app.task_create({"title": "Waitlist", "owner": "riya", "notify": False})
    app.rt.tasks.set_status(t["id"], "blocked", by="riya", blocked_on="Maria — which email provider")
    from james_monitoring.monitor import build_report, on_owner
    rep = build_report(app.rt.cfg, app.rt.ws, app.rt.tasks, app.rt.asks)
    assert "1 block(s) waiting on Maria Lopez" in rep and f"Unblock {t['id']}" in rep
    assert not on_owner(app.rt.cfg, "Mariana from marketing — the copy")     # whole words only


# -- the public link (Cloudflare Tunnel) ------------------------------------------------------------------------------
def test_public_link_starts_a_tunnel_and_guards_the_key(app, http, tmp_path, monkeypatch):
    fake = tmp_path / "cloudflared"
    fake.write_text("#!/bin/sh\necho 'INF Starting tunnel'\necho 'INF |  https://brave-otter-12.trycloudflare.com  |'\n"
                    "sleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setenv("JM_CLOUDFLARED_BIN", str(fake))
    app.console_key, app.port = "short", 8765
    assert "too short" in app.public_set(True)["error"]                  # a guessable key never goes public
    app.console_key = KEY + "-long-enough-key"
    st = app.public_set(True)
    assert st["on"] and st["url"] == "https://brave-otter-12.trycloudflare.com"
    assert st["link"].endswith("/?k=" + app.console_key)
    assert app.console_link().startswith("https://brave-otter-12.trycloudflare.com/?k=")
    # /link: in a private chat only
    app.rt.console_link = app.console_link
    mgr = app.rt.cfg.monitor.id
    app.chat_send(mgr, "/link")
    assert "trycloudflare.com/?k=" in settle(app, lambda: [m for m in app.chat.since(mgr, -1) if m["who"] == mgr])[-1]["text"]
    app.chat_send("team", "/link")
    assert "private chat" in settle(app, lambda: [m for m in app.chat.since("team", -1) if m["who"] != "maria_lopez"])[-1]["text"]
    assert not app.public_set(False)["on"] and app.public.proc is None


def test_wrong_keys_lock_out_that_visitor_only(app, http):
    for _ in range(20):
        http("/api/state", key="guess")
    st, body = http("/api/state", key="guess")
    assert st == 429 and "Too many" in body["error"]
    assert http("/api/state")[0] == 429                                  # same visitor (this test's IP), even with the key
    app.failed_keys.hits.clear()
    assert http("/api/state")[0] == 200


def test_board_command_hides_personal_tasks(app):
    app.studio_hire({"name": "Ada", "role": "Assistant", "assistant": True})
    app.task_create({"title": "Renew passport", "owner": "ada", "notify": False})
    app.task_create({"title": "Ship v1", "owner": "riya", "notify": False})
    app.chat_send("team", "/board")
    out = settle(app, lambda: [m for m in app.chat.since("team", -1) if m.get("kind") == "system"])[-1]["text"]
    assert "Ship v1" in out and "Renew passport" not in out


# -- attachments: documents, PDFs, images ---------------------------------------------------------------------------
def _pdf_bytes(text: str) -> bytes:
    """A one-page PDF with a text layer, without extra libraries."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


def test_attachments_reach_the_agent_and_stay_in_their_room(app, http):
    import base64
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    app.rt.llm.push({"reply": "Read both.", "actions": []})
    r = app.chat_send("p-site", "@riya see the brief and the mock", files=[
        {"name": "brief.md", "data": base64.b64encode(b"# Brief\nLaunch on Oct 10. Budget 2k.").decode()},
        {"name": "spec.pdf", "data": base64.b64encode(_pdf_bytes("Pricing tiers: Free, Pro 29, Team 99")).decode()},
        {"name": "mock.png", "data": base64.b64encode(png).decode()}])
    settle(app, lambda: len(app.chat.since("p-site", -1)) >= 2)
    msg = app.chat.since("p-site", -1)[r["i"]]
    kinds = {f["name"]: f for f in msg["files"]}
    assert kinds["spec.pdf"]["kind"] == "pdf" and kinds["spec.pdf"]["pages"] == 1 and kinds["mock.png"]["kind"] == "image"
    sent = app.rt.llm.calls[-1][1][-1]["content"]
    assert "Launch on Oct 10" in sent and "Pricing tiers: Free, Pro 29, Team 99" in sent
    assert "can't see images" in sent                                      # the fake model has no eyes
    assert "files for p-site: brief.md, spec.pdf, mock.png" in app.rt.ws.git_log(5)
    # served only to people who can see the room, never as a page
    st, _ = http(f"/api/file?room=p-site&path={kinds['brief.md']['path']}", raw=True)
    assert st == 200
    assert http(f"/api/file?room=riya&path={kinds['brief.md']['path']}")[0] == 404      # wrong room
    assert http("/api/file?room=p-site&path=files/p-site/../../config.yaml")[0] == 404
    viewer = http("/api/users", {"action": "add", "name": "Vi", "role": "viewer"})[1]["key"]
    assert http(f"/api/file?room=riya&path=files/riya/x", key=viewer)[0] == 403        # a 1:1 isn't theirs
    with pytest.raises(ValueError, match="limit"):
        app.chat_send("p-site", "big", files=[{"name": "big.bin", "data": base64.b64encode(b"0" * 15_000_001).decode()}])


def test_images_go_to_models_that_can_see(app, tmp_path):
    from james_monitoring.files import for_agent, save
    p = save(app.rt.ws.root, "team", "a.png", b"\x89PNG\r\n\x1a\nxx")
    text, images = for_agent(app.rt.ws.root, [p], can_see_images=True)
    assert images and images[0].endswith("a.png") and "you can see it" in text
    from james_monitoring.llm.anthropic_llm import _with_images
    msgs = _with_images([{"role": "user", "content": "look"}], [{"role": "user", "content": "look", "images": images}])
    assert msgs[-1]["content"][0]["type"] == "image" and msgs[-1]["content"][-1]["text"] == "look"


# -- getting better at the job: playbook, reflection, related past work --------------------------------------------
def test_agents_learn_from_accepted_work_and_use_it_next_time(app, http):
    rt = app.rt
    t = app.task_create({"title": "Landing page hero copy", "owner": "sam", "notify": False, "done_means": ["copy"]})
    rt.tasks.set_output(t["id"], "sam", "docs/site/hero-copy.md — headline 'Ship faster', subline with the yearly price")
    rt.tasks.set_status(t["id"], "review", by="sam")
    # accepting it asks Sam what's worth keeping — Sam writes a lesson
    rt.llm.push({"reply": "Noted what worked.", "actions": [
        {"type": "learn", "lesson": "Hero copy: lead with the customer's problem and show the yearly price", "topic": "copy"}]})
    app.task_action({"id": t["id"], "action": "accept", "note": "great, the yearly price sells it"})
    settle(app, lambda: rt.ws.lessons("sam"))
    lesson = rt.ws.lessons("sam")[0]
    assert lesson["topic"] == "copy" and lesson["source"] == t["id"]
    assert "accepted" in rt.llm.calls[-1][1][-1]["content"] and "learn" in rt.llm.calls[-1][1][-1]["content"]
    settle(app, lambda: any("📚 learned" in m["text"] for m in app.chat.since("sam", -1)))
    # the same lesson twice is kept once
    assert rt.ws.learn("sam", "Hero copy: lead with the customer's problem and show the yearly price") is False
    # next time: the playbook is in the prompt, and the similar finished task shows up as related past work
    t2 = app.task_create({"title": "Pricing page hero copy", "owner": "sam", "notify": False})
    rt.llm.push({"reply": "On it.", "actions": []})
    app.chat_send("sam", f"Please start {t2['id']}: the pricing page hero copy")
    settle(app, lambda: len([m for m in app.chat.since("sam", -1) if m["who"] == "sam"]) >= 2)
    system = rt.llm.calls[-1][0]
    assert "# Your playbook" in system and "show the yearly price" in system
    assert "Your related past work" in system and "Landing page hero copy" in system and "yearly price sells it" in system
    # the founder curates it
    st, pb = http("/api/playbook?id=sam")
    assert st == 200 and pb["done"] == 1 and len(pb["lessons"]) == 1
    http("/api/playbook", {"id": "sam", "text": "Always add alt text to hero images", "topic": "design"})
    assert len(rt.ws.lessons("sam")) == 2
    http("/api/playbook", {"id": "sam", "action": "delete", "entry": pb["lessons"][0]["id"]})
    assert [x["topic"] for x in rt.ws.lessons("sam")] == ["design"]
    member = http("/api/users", {"action": "add", "name": "Mo", "role": "member"})[1]["key"]
    assert http("/api/playbook", {"id": "sam", "text": "x" * 20}, key=member)[0] == 403


def test_sent_back_work_asks_what_to_do_differently(app):
    t = app.task_create({"title": "Logo", "owner": "sam", "notify": False})
    app.rt.tasks.set_status(t["id"], "review", by="sam")
    app.rt.llm.push({"reply": "Will fix.", "actions": [{"type": "learn", "lesson": "Logos: always test at 16px favicon size"}]})
    app.task_action({"id": t["id"], "action": "changes", "text": "unreadable at small sizes"})
    settle(app, lambda: app.rt.ws.lessons("sam"))
    assert "what you'll do differently next time with learn" in app.rt.llm.calls[-1][1][-1]["content"]
