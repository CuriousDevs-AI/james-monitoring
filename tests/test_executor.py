import subprocess

from james_monitoring.config import parse_config
from james_monitoring.llm.fake import FakeLLM
from james_monitoring.runtime import ConsoleBus, Event, Runtime

from .conftest import make_raw


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


async def test_code_on_branch_then_owner_approves_merge(tmp_path):
    repo = tmp_path / "site"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "index.html").write_text("<h1>old</h1>\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")

    raw = make_raw(tmp_path, projects={"site": {"repo": str(repo)}},
                   executor={"command": ["sh", "-c", "echo '<h1>new</h1>' > index.html"]},
                   permissions={"run_code": "green"})
    cfg = parse_config(raw, base_dir=tmp_path)
    rt = Runtime(cfg, FakeLLM(), bus=ConsoleBus(quiet=True))
    t = rt.tasks.create(title="New hero", owner="sofia", created_by="pankaj", project="site", done_means=["new h1"])

    rt.llm.push({"reply": "Changing it.", "actions": [
        {"type": "run_code", "task": t.id, "project": "site", "instructions": "make the h1 say new"}]})
    out = await rt.dispatch("sofia", Event("dm", "change the hero text", sender="pankaj"))
    assert "coding started on branch jm/T-001" in out
    await rt.drain()

    assert (repo / "index.html").read_text() == "<h1>old</h1>\n"          # main untouched
    ask = rt.asks.pending()[0]
    assert ask.kind == "merge" and ask.level == "red"
    assert rt.tasks.get(t.id).status == "review"

    rt.llm.push({"reply": "Merged, thanks.", "actions": []})
    res = await rt.decide_ask(ask.id, "approved", by="Pankaj")
    await rt.drain()
    assert "merged jm/T-001" in res
    assert (repo / "index.html").read_text() == "<h1>new</h1>\n"
    assert rt.tasks.get(t.id).status == "done"


async def test_failed_merge_is_rolled_back_and_the_request_stays_open(tmp_path):
    import pytest
    from james_monitoring.asks import AskError
    repo = tmp_path / "site"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "index.html").write_text("<h1>old</h1>\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    raw = make_raw(tmp_path, projects={"site": {"repo": str(repo)}},
                   executor={"command": ["sh", "-c", "echo '<h1>agent</h1>' > index.html"]},
                   permissions={"run_code": "green"})
    rt = Runtime(parse_config(raw, base_dir=tmp_path), FakeLLM(), bus=ConsoleBus(quiet=True))
    t = rt.tasks.create(title="Hero", owner="sofia", created_by="pankaj", project="site", done_means=["x"])
    rt.llm.push({"reply": "ok", "actions": [{"type": "run_code", "task": t.id, "project": "site", "instructions": "x"}]})
    await rt.dispatch("sofia", Event("dm", "go", sender="pankaj"))
    await rt.drain()
    # meanwhile someone changes main → the merge will conflict
    (repo / "index.html").write_text("<h1>human</h1>\n")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "human edit")
    ask = rt.asks.pending()[0]
    with pytest.raises(AskError, match="still open"):
        await rt.decide_ask(ask.id, "approved", by="Pankaj")
    assert rt.asks.get(ask.id).status == "pending"
    assert git(repo, "status", "--porcelain") == "" and (repo / "index.html").read_text() == "<h1>human</h1>\n"
    assert not (repo / ".git/MERGE_HEAD").exists()


async def test_agents_cannot_read_git_or_secrets(rt):
    (rt.ws.root / "docs").mkdir(exist_ok=True)
    (rt.ws.root / ".env").write_text("SECRET=1")
    for bad in ["docs/../.git/config", "team/../.git/config", "docs/../.env", "tasks/../.jm/state.json", "/etc/passwd"]:
        assert rt._resolve_readable(bad) is None, bad
    t = rt.tasks.create(title="Pricing", owner="sofia", created_by="pankaj")
    assert rt._resolve_readable("T-001") == t.path.resolve() == rt._resolve_readable("tasks/T-001.md")
