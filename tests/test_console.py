"""The console backend end to end: setup from an empty folder → projects → people → chat → board → approvals."""
import time

import pytest

from james_monitoring.llm.fake import FakeLLM
from james_monitoring.server import App


def wait_for(fn, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("timed out")


@pytest.fixture
def app(tmp_path):
    a = App(tmp_path / "company", telegram=False)
    a.base.mkdir(parents=True)
    a.load()
    yield a
    if a.ready:
        a.submit(a._stop_services(), timeout=10)
    a.loop.call_soon_threadsafe(a.loop.stop)


def use_fake(app):
    fake = FakeLLM()
    app.rt.llm = fake
    return fake


def test_setup_to_daily_use(app):
    assert app.state()["setup_needed"]
    app.setup({"company": "Acme", "owner": "Maria Lopez", "timezone": "Europe/Madrid", "provider": "fake",
               "goals": "Ship v1\nFirst customer", "manager_name": "Nova"})
    st = app.state()
    assert not st["setup_needed"] and st["company"] == "Acme" and st["members"][0]["name"] == "Nova"
    assert not st["telegram"]["connected"]
    with pytest.raises(ValueError, match="already has a team"):
        app.setup({"company": "x", "owner": "y"})

    # people (console only, no Telegram bot needed)
    app.admin.add(name="Riya", role="Backend lead", token="")
    app.load()
    assert [m["name"] for m in app.state()["members"]] == ["Nova", "Riya"]

    # project with a lead → lead gets the project
    app.project_save({"name": "API v1", "description": "Public REST API", "lead": "riya", "status": "active"})
    st = app.state()
    assert st["projects"][0]["id"] == "api_v1" and "api_v1" in st["members"][1]["projects"]
    assert "Public REST API" in app.rt.ws.read("projects/api_v1.md")

    # board: owner creates a task → the owner acknowledges in her chat room
    fake = use_fake(app)
    fake.push({"reply": "Got T-001, starting today.", "actions": []})
    t = app.task_create({"title": "Rate limiting", "owner": "riya", "priority": "P0", "project": "api_v1",
                         "done_means": ["429 on limit", ""]})
    assert t["id"] == "T-001" and t["priority"] == "P0"
    wait_for(lambda: any("Got T-001" in m["text"] for m in app.chat.since("riya")))

    # chat DM → agent works → task to review → dashboard shows it waiting on the owner
    fake.push({"reply": "Draft written, ready for review.", "actions": [
        {"type": "write_file", "path": "docs/api/rate-limits.md", "content": "# Limits", "task": "T-001"},
        {"type": "update_task", "id": "T-001", "status": "review"}]})
    app.chat_send("riya", "write the rate limit note")
    wait_for(lambda: any("ready for review" in m["text"] for m in app.chat.since("riya")))
    d = app.dashboard()
    assert d["tasks"]["review"] == 1 and d["review"][0]["id"] == "T-001"
    assert any(m["kind"] == "msg" and "ready for your review" in m["text"] for m in app.chat.since("riya"))

    # feedback + accept from the board
    app.task_action({"id": "T-001", "action": "feedback", "text": "Use per-key limits"})
    assert "per-key limits" in app.rt.ws.memory("riya")
    app.task_action({"id": "T-001", "action": "accept"})
    assert app.task_detail("T-001")["status"] == "done"
    with pytest.raises(Exception):
        app.task_action({"id": "T-001", "action": "bogus"})

    # permission request → shows as an ask card in chat → approve from the console
    fake.push({"reply": "Need a domain.", "actions": [{"type": "ask_permission", "summary": "Buy acme.dev (€12)",
                                                        "level": "red"}]})
    fake.push({"reply": "Buying it.", "actions": []})
    app.chat_send("riya", "get us a domain")
    wait_for(lambda: any(m["kind"] == "ask" for m in app.chat.since("riya")))
    assert app.dashboard()["asks"][0]["id"] == "ASK-001"
    assert "approved" in app.decide("ASK-001", "approved")["result"]
    wait_for(lambda: any("Buying it." in m["text"] for m in app.chat.since("riya")))

    # team room: @all status is instant from the board, /commands work
    app.chat_send("team", "@all give status")
    wait_for(lambda: len([m for m in app.chat.since("team") if m["who"] in ("nova", "riya")]) >= 2)
    app.chat_send("team", "/status")
    wait_for(lambda: any(m["kind"] == "system" for m in app.chat.since("team")))

    # decisions + report
    app.decisions_save("# Open\n\n1. Pick hosting")
    rel = app.submit(app.rt.run_daily_report())
    assert "Pick hosting" in app.rt.ws.read(rel)

    # settings persist and reload
    app.settings_save({"daily_report": "17:45", "work_sessions": ["09:30", " "], "budget": "50000", "model": "m2"})
    assert app.rt.cfg.daily_report == "17:45" and app.rt.cfg.work_sessions == ["09:30"]
    assert app.rt.cfg.daily_tokens_per_agent == 50000

    # member profile edit
    app.member_update({"id": "riya", "role": "Backend & infra", "projects": ["api_v1"], "persona": "# Riya\nNew persona"})
    assert app.member_detail("riya")["persona"].startswith("# Riya\nNew persona")
    assert app.state()["members"][1]["role"] == "Backend & infra"


def test_scheduler_due_jobs():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from types import SimpleNamespace
    from james_monitoring.scheduler import due_jobs
    cfg = SimpleNamespace(daily_report="18:30", work_sessions=["10:00", "15:00"], check_every_minutes=60)
    tz = ZoneInfo("Asia/Kolkata")
    mon_1005 = datetime(2026, 9, 28, 10, 5, tzinfo=tz)
    assert due_jobs(cfg, {}, mon_1005) == ["work:10:00", "checks"]
    st = {"sched": {"work:10:00": "2026-09-28", "checks": mon_1005.isoformat()}}
    assert due_jobs(cfg, st, datetime(2026, 9, 28, 10, 30, tzinfo=tz)) == []
    eve = datetime(2026, 9, 28, 18, 31, tzinfo=tz)
    assert "report" in due_jobs(cfg, st, eve) and "work:15:00" in due_jobs(cfg, st, eve)
    sunday = datetime(2026, 9, 27, 15, 1, tzinfo=tz)
    assert not any(j.startswith("work") for j in due_jobs(cfg, {}, sunday))
