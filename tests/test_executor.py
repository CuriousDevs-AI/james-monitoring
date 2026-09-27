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
                   executor={"command": ["sh", "-c", "echo '<h1>new</h1>' > index.html"]})
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
