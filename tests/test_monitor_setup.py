from james_monitoring.config import load_config
from james_monitoring.monitor import build_report, checks
from james_monitoring.setup import scaffold, strip_frontmatter

from .conftest import make_raw


def test_report_sections(rt):
    t = rt.tasks.create(title="late one", owner="sofia", created_by="pankaj", due="2000-01-01", done_means=["x"])
    rt.tasks.set_status(t.id, "blocked", by="sofia", blocked_on="Pankaj — go/no-go on site")
    rt.asks.create(requester="marcus", summary="Pay for VPS", level="red")
    text = build_report(rt.cfg, rt.ws, rt.tasks, rt.asks)
    for h in ["## Headline", "## Snapshot", "## Needs Pankaj", "## Blocked / at risk", "## Shipped since last report",
              "## Critical path"]:
        assert h in text
    assert "ASK-001" in text and "Unblock T-001" in text


def test_checks_alert_once_per_day(rt):
    rt.tasks.create(title="late", owner="sofia", created_by="pankaj", due="2000-01-01")
    first = checks(rt.cfg, rt.ws, rt.tasks, rt.asks)
    assert any("overdue" in a.text for a in first)
    assert checks(rt.cfg, rt.ws, rt.tasks, rt.asks) == []


def test_scaffold_imports_skill_persona(tmp_path):
    raw = make_raw(tmp_path)
    raw["llm"] = {"provider": "anthropic", "model": "m"}
    skill = "---\nname: sofia\ndescription: x\n---\n# Sofia\nFrontend lead persona body\n"
    cfg = scaffold(raw, personas={"sofia": strip_frontmatter(skill)}, goals=["Ojas demo", "First Janus customer"],
                   tokens={"sofia": "123:abc"}, api_key="sk-test", base_dir=tmp_path / "jm")
    ws = cfg.workspace_path
    assert (ws / "team/sofia/persona.md").read_text().startswith("# Sofia")
    assert "delivery manager" in (ws / "team/james/persona.md").read_text().lower()
    assert "1. Ojas demo" in (ws / "team/charter.md").read_text()
    env = (tmp_path / "jm/.env").read_text()
    assert "TG_TOKEN_SOFIA=123:abc" in env and "ANTHROPIC_API_KEY=sk-test" in env
    again = load_config(tmp_path / "jm/config.yaml")
    assert again.member("sofia").bot_token == "123:abc"


def test_open_decisions_reach_the_report(rt):
    rt.ws.write("decisions/OPEN.md", "# Open\n\n1. Server + budget\n2. ~~done thing~~\n- Pick a vertical\n")
    text = build_report(rt.cfg, rt.ws, rt.tasks, rt.asks)
    assert "- Decide: Server + budget" in text and "- Decide: Pick a vertical" in text
    assert "done thing" not in text and "waiting on Pankaj" in text


def test_first_push_to_empty_remote_sets_upstream(tmp_path):
    import subprocess
    from james_monitoring.config import parse_config
    from james_monitoring.workspace import Workspace
    from .conftest import make_raw
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(tmp_path / "ws")], check=True, capture_output=True)
    raw = make_raw(tmp_path)
    raw["workspace"]["push"] = True
    ws = Workspace(parse_config(raw, base_dir=tmp_path))
    ws.ensure()
    ws.write("team/charter.md", "x")
    assert ws.commit("first")
    assert not ws.state().get("push_error")
    heads = subprocess.run(["git", "ls-remote", "--heads", str(remote)], capture_output=True, text=True).stdout
    assert heads.strip(), "nothing reached the empty remote"
