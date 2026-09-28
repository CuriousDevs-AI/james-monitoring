"""Data safety: config.yaml, .env, state.json and git under concurrency and hostile conditions."""
import os
import subprocess
import threading
import time

import yaml

from james_monitoring.config import parse_config
from james_monitoring.fileio import read_config, set_env, update_config
from james_monitoring.workspace import Workspace

from .conftest import make_raw


def test_concurrent_config_saves_never_corrupt_or_lose_updates(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(make_raw(tmp_path)))
    errors = []

    def worker(i):
        try:
            for j in range(10):
                update_config(path, lambda raw: raw.setdefault("projects", {}).__setitem__(f"p{i}_{j}", {"repo": ""}))
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    raw = read_config(path)
    assert len([k for k in raw["projects"] if k.startswith("p")]) == 80        # no update lost
    parse_config(raw, base_dir=tmp_path)                                         # still loads
    assert (tmp_path / "config.yaml.bak").exists()


def test_invalid_config_is_never_written(tmp_path):
    from james_monitoring.config import ConfigError
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(make_raw(tmp_path)))
    before = path.read_text()
    try:
        update_config(path, lambda raw: raw.__setitem__("team", []))
    except ConfigError:
        pass
    else:
        raise AssertionError("an empty team must be refused")
    assert path.read_text() == before


def test_env_values_are_replaced_not_duplicated(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# keys\nANTHROPIC_API_KEY=old\nTG_TOKEN_JAMES=t1\nANTHROPIC_API_KEY=older\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    set_env(env, {"ANTHROPIC_API_KEY": "new", "SLACK_BOT_TOKEN": "xoxb-1"})
    text = env.read_text()
    assert text.count("ANTHROPIC_API_KEY=") == 1 and "ANTHROPIC_API_KEY=new" in text
    assert "TG_TOKEN_JAMES=t1" in text and "# keys" in text and "SLACK_BOT_TOKEN=xoxb-1" in text
    assert os.environ["ANTHROPIC_API_KEY"] == "new" and oct(env.stat().st_mode & 0o777) == "0o600"


def _ws(tmp_path, **ws_over):
    raw = make_raw(tmp_path)
    raw["workspace"].update(ws_over)
    ws = Workspace(parse_config(raw, base_dir=tmp_path))
    ws.ensure()
    return ws


def test_parallel_commits_from_two_workspaces_never_collide(tmp_path):
    a, b = _ws(tmp_path), _ws(tmp_path)          # two runtimes on one repo (e.g. during a reload)
    errors = []

    def worker(ws, i):
        for j in range(10):
            ws.write(f"docs/{i}-{j}.md", "x")
            ws.commit(f"c{i}-{j}")
        if ws.state().get("commit_error"):
            errors.append(ws.state()["commit_error"])
    threads = [threading.Thread(target=worker, args=(ws, i)) for i, ws in enumerate([a, b, a, b])]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert subprocess.run(["git", "status", "--porcelain"], cwd=a.root, capture_output=True, text=True).stdout == ""


def test_commit_ignores_global_signing_and_a_hanging_push_doesnt_block(tmp_path):
    ws = _ws(tmp_path, push=True)
    subprocess.run(["git", "config", "commit.gpgsign", "true"], cwd=ws.root)
    subprocess.run(["git", "config", "gpg.program", "false"], cwd=ws.root)          # signing would fail
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=ws.root)
    hook = ws.root / ".git/hooks/pre-push"
    hook.write_text("#!/bin/sh\nsleep 20\n")
    hook.chmod(0o755)
    ws.write("team/charter.md", "x")
    t0 = time.time()
    assert ws.commit("signed? no")
    assert time.time() - t0 < 5                                              # push runs in the background


def test_broken_state_json_recovers_from_backup(tmp_path):
    ws = _ws(tmp_path)
    ws.update_state(lambda s: s.__setitem__("paused", ["sofia"]))
    ws.update_state(lambda s: s.__setitem__("x", 1))
    (ws.root / ".jm/state.json").write_text("{broken")
    assert ws.state().get("paused") == ["sofia"]


def test_bad_times_are_refused_not_silently_breaking_the_schedule(tmp_path):
    import pytest
    from james_monitoring.config import ConfigError
    for bad in ({"daily_report": "6pm"}, {"daily_report": "25:00"}, {"work_sessions": ["10am"]}):
        with pytest.raises(ConfigError, match="24-hour time"):
            parse_config(make_raw(tmp_path, monitor=bad), base_dir=tmp_path)
    cfg = parse_config(make_raw(tmp_path, monitor={"daily_report": "9:05", "work_sessions": ["15:00", "10:00"]}),
                       base_dir=tmp_path)
    assert cfg.daily_report == "09:05" and cfg.work_sessions == ["10:00", "15:00"]


def test_chat_reads_are_incremental_and_shared(tmp_path):
    from james_monitoring.chat import ChatStore
    a, b = ChatStore(tmp_path), ChatStore(tmp_path)          # e.g. `jm run` and `jm chat`, or two runtimes
    for i in range(5):
        (a if i % 2 else b).append("sofia", "pankaj", f"m{i}")
    assert [m["i"] for m in a.since("sofia")] == [0, 1, 2, 3, 4] == [m["i"] for m in b.since("sofia")]
    assert [m["text"] for m in a.since("sofia", 2)] == ["m3", "m4"] and a.last_index("sofia") == 4


def test_runtime_folder_is_backed_up_daily(tmp_path):
    from james_monitoring.llm.fake import FakeLLM
    from james_monitoring.runtime import Runtime
    import zipfile
    rt = Runtime(parse_config(make_raw(tmp_path), base_dir=tmp_path), FakeLLM())
    rt.chat.append("sofia", "pankaj", "hello")
    rt.ws.update_state(lambda s: s.__setitem__("paused", ["marcus"]))
    names = zipfile.ZipFile(rt.backup_runtime()).namelist()
    assert "chat/sofia.jsonl" in names and "state.json" in names
