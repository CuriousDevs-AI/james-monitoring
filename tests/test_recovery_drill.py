"""Offline drill: shared Git checkpoint + private runtime archive into a separate company directory."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from james_monitoring.asks import AskError
from james_monitoring.backups import restore
from james_monitoring.config import load_config
from james_monitoring.fileio import write_config
from james_monitoring.llm.fake import FakeLLM
from james_monitoring.runtime import ConsoleBus, Runtime
from james_monitoring.scheduler import Scheduler
from james_monitoring.workspace import run_git
from .conftest import make_raw


async def test_company_recovery_preserves_tasks_private_context_and_no_replay(rt, tmp_path):
    research = rt.tasks.create(title="Find synthetic candidates", owner="sofia", created_by=rt.owner_id)
    validation = rt.tasks.create(title="Validate candidates", owner="marcus", created_by=rt.owner_id,
                                  depends_on=[research.id])
    rt.chat.append("sofia", rt.owner_id, "Private source-scope instructions")
    rt.ws.update_state(lambda s: s.update(paused_all=True))
    ask = rt.asks.create(requester="sofia", summary="Write approved result", kind="action", task=research.id,
                         payload={"action": {"type": "write_file", "path": "docs/result.md", "content": "approved"}})
    rt.asks.start_execution(ask, rt.owner_id)  # checkpoint after intent, with no recorded outcome
    scheduler = Scheduler(rt)
    at = datetime(2026, 10, 19, 10, tzinfo=ZoneInfo("Asia/Kolkata"))
    with scheduler.history.guard("checks"):
        scheduled = scheduler.history.claim("checks", at.isoformat(), at.isoformat())
    assert rt.ws.commit("Synthetic recovery checkpoint", rt.cfg.owner_name)
    revision = run_git(rt.ws.root, "rev-parse", "HEAD").strip()
    archive = Path(rt.backup_runtime())

    company = tmp_path / "recovered-company"
    company.mkdir()
    workspace = company / "workspace"
    run_git(company, "clone", "--no-hardlinks", str(rt.ws.root), str(workspace))
    run_git(workspace, "remote", "remove", "origin")  # recovered test company never pushes into its source
    restore(archive, workspace / ".jm")
    raw = make_raw(company, workspace={"path": "workspace", "push": False},
                   monitor={"work_sessions": []})
    write_config(company / "config.yaml", raw)
    recovered = Runtime(load_config(company / "config.yaml"), FakeLLM(), bus=ConsoleBus(quiet=True))

    assert recovered.ws.root == workspace.resolve() and recovered.paused("all")
    assert run_git(workspace, "rev-parse", "HEAD").strip() == revision
    assert recovered.tasks.unmet_dependencies(recovered.tasks.get(validation.id))
    assert recovered.chat.since("sofia")[0]["text"] == "Private source-scope instructions"
    assert recovered.next_work(recovered.cfg.member("sofia")) is None
    with pytest.raises(AskError, match="No retry"):
        await recovered.decide_ask(ask.id, "approved", recovered.owner_id)
    restored_scheduler = Scheduler(recovered)
    restored_scheduler.history.recover(at.isoformat())
    assert restored_scheduler.history.all()[0]["id"] == scheduled["id"]
    assert restored_scheduler.history.all()[0]["status"] == "interrupted"
    assert not await restored_scheduler._run_job("checks", at)
    assert not (workspace / "docs/result.md").exists()
    assert not recovered.llm.calls
    assert not (company / ".env").exists()
