"""GitHub: the board mirrored into a Project (both ways, through the rules), PRs and commits as the owner."""
import json
import stat
import subprocess
import sys

from james_monitoring.config import parse_config
from james_monitoring.llm.fake import FakeLLM
from james_monitoring.runtime import ConsoleBus, Event, Runtime

from .conftest import make_raw


def fake_gh(tmp_path, monkeypatch):
    exe = tmp_path / "gh"
    exe.write_text(f"#!/bin/sh\nexec {sys.executable} {__file__.replace('test_github.py', 'fake_gh.py')} \"$@\"\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "gh.json"
    monkeypatch.setenv("JM_GH_BIN", str(exe))
    monkeypatch.setenv("FAKE_GH_STATE", str(state))
    return lambda: json.loads(state.read_text()) if state.exists() else {}


def runtime(tmp_path, **over):
    raw = make_raw(tmp_path, github={"owner": "pankajneema", "repo": "acme/team", "project": 7}, **over)
    raw["owner"].update(git_name="pankajneema", git_email="pankaj200321@gmail.com")
    return Runtime(parse_config(raw, base_dir=tmp_path), FakeLLM(), bus=ConsoleBus(quiet=True))


async def test_tasks_become_issues_on_the_board_and_stay_in_sync(tmp_path, monkeypatch):
    gh = fake_gh(tmp_path, monkeypatch)
    rt = runtime(tmp_path)
    t = rt.tasks.create(title="Pricing page", owner="sofia", created_by="pankaj", priority="P0", due="2099-10-03",
                        project="site", done_means=["tiers", "FAQ"])
    r = await rt.sync_github()
    s = gh()
    assert r["pushed"] == 1 and s["issues"]["1"]["title"] == "[T-001] Pricing page"
    assert "- [ ] tiers" in s["issues"]["1"]["body"]
    item = s["items"]["PVTI_1"]
    assert (item["stage"], item["priority"], item["owner"], item["due"], item["status"]) == \
        ("To do", "P0", "Sofia", "2099-10-03", "Todo")
    assert {f["name"] for f in s["fields"]} >= {"Stage", "Priority", "Owner", "Due", "Project", "Blocker"}
    calls = len(s["calls"])
    assert (await rt.sync_github())["pushed"] == 0 and len(gh()["calls"]) <= calls + 3     # nothing changed → no edits
    rt.tasks.set_status(t.id, "review", by="sofia")
    await rt.sync_github()
    assert gh()["items"]["PVTI_1"]["stage"] == "Review" and rt.github.url_of(t.id).endswith("/issues/1")


async def test_changes_on_github_come_back_through_the_rules(tmp_path, monkeypatch):
    gh = fake_gh(tmp_path, monkeypatch)
    rt = runtime(tmp_path)
    a = rt.tasks.create(title="Landing copy", owner="sofia", created_by="pankaj", done_means=["x"])
    b = rt.tasks.create(title="Schema", owner="marcus", created_by="pankaj", done_means=["x"])
    rt.tasks.set_status(a.id, "review", by="sofia")
    await rt.sync_github()
    s = gh()
    s["items"]["PVTI_1"]["stage"] = "Done"                       # you dragged it to Done on GitHub = accepted
    s["items"]["PVTI_2"].update(owner="Sofia", priority="P2")    # reassigned + re-prioritised on GitHub
    s["comments"] = [{"issue_url": "https://api.github.com/repos/acme/team/issues/2", "body": "Use UUID keys",
                      "created_at": "2099-01-01T00:00:00Z"}]
    json.dump(s, open(tmp_path / "gh.json", "w"))
    rt.llm.push({"reply": "Switching to UUIDs.", "actions": []})
    r = await rt.sync_github()
    await rt.drain()
    assert rt.tasks.get(a.id).status == "done"
    tb = rt.tasks.get(b.id)
    assert (tb.owner, tb.priority) == ("sofia", "P2") and "Use UUID keys" in tb.doc.sections["Feedback"]
    assert gh()["issues"]["1"]["state"] == "closed" and r["pulled"] == 4
    assert (await rt.sync_github())["pulled"] == 0                        # applied once, not again


async def test_owner_is_the_author_of_team_commits(tmp_path):
    rt = runtime(tmp_path)
    rt.llm.push({"reply": "ok", "actions": [{"type": "create_task", "title": "Hero"}]})
    await rt.dispatch("sofia", Event("dm", "add a hero task", sender="pankaj"))
    out = subprocess.run(["git", "log", "-1", "--format=%an|%ae|%cn|%s"], cwd=rt.ws.root, capture_output=True,
                         text=True).stdout.strip()
    assert out.startswith("pankajneema|pankaj200321@gmail.com|pankajneema|Sofia: replied to Pankaj")


async def test_code_changes_open_a_pr_as_the_owner_and_approval_merges_it(tmp_path, monkeypatch):
    gh = fake_gh(tmp_path, monkeypatch)
    origin, repo = tmp_path / "origin.git", tmp_path / "site"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True)
    (repo / "index.html").write_text("old\n")
    for c in (["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
              ["push", "-q", "origin", "HEAD:main"]):
        subprocess.run(["git", *c], cwd=repo, check=True, capture_output=True)
    rt = runtime(tmp_path, projects={"site": {"repo": str(repo)}}, permissions={"run_code": "green"},
                 executor={"command": ["sh", "-c", "echo new > index.html"]})
    rt.cfg.github.prs = True
    t = rt.tasks.create(title="New hero", owner="sofia", created_by="pankaj", project="site", done_means=["x"])
    rt.llm.push({"reply": "Coding.", "actions": [{"type": "run_code", "task": t.id, "project": "site",
                                                 "instructions": "new hero"}]})
    await rt.dispatch("sofia", Event("dm", "do it", sender="pankaj"))
    await rt.drain()
    ask = rt.asks.pending()[0]
    pr = ask.doc.meta["payload"]["pr"]
    assert pr == "https://github.com/acme/site/pull/1" and gh()["prs"][pr]["title"] == "T-001: New hero"
    branch_log = subprocess.run(["git", "log", "-1", "--format=%an|%ae|%s", "jm/T-001"], cwd=origin,
                                capture_output=True, text=True).stdout.strip()
    assert branch_log == "pankajneema|pankaj200321@gmail.com|T-001: New hero"      # pushed, and it's yours
    rt.llm.push({"reply": "Merged.", "actions": []})
    res = await rt.decide_ask(ask.id, "approved", by="Pankaj")
    assert "PR #1" in res and gh()["prs"][pr]["state"] == "merged" and rt.tasks.get(t.id).status == "done"
