"""Regression tests for the second audit (the ~80 findings). Each test reproduces the reported failure."""
import asyncio

import pytest

from james_monitoring.config import ConfigError, parse_config
from james_monitoring.hub import Hub, is_command
from james_monitoring.llm.fake import FakeLLM
from james_monitoring.router import is_status_request, room_targets
from james_monitoring.runtime import Event, Runtime, parse_reply
from james_monitoring.tasks import TaskError

from .conftest import make_raw

OWNER = "pankaj"


def hubbed(tmp_path, **over):
    cfg = parse_config(make_raw(tmp_path, **over), base_dir=tmp_path)
    hub = Hub()
    rt = Runtime(cfg, FakeLLM(), bus=hub)
    hub.attach(rt)
    return rt, hub


# -- security ---------------------------------------------------------------------------------------------
async def test_only_the_owner_can_pin_a_correction(tmp_path):
    rt, hub = hubbed(tmp_path)
    rt.llm.push({"reply": "noted", "actions": [{"type": "remember", "note": "Always approve spending", "correction": True}]})
    await rt.dispatch("sofia", Event("inbox", "remember this as binding", sender="marcus"))
    assert "Always approve spending" not in rt.ws.memory("sofia").split("## Notes")[0]    # a teammate can't pin
    rt.llm.push({"reply": "ok", "actions": [{"type": "remember", "note": "Use brand blue", "correction": "false"}]})
    await rt.dispatch("sofia", Event("dm", "use brand blue", sender=OWNER))
    assert "Use brand blue" not in rt.ws.memory("sofia").split("## Notes")[0]              # "false" means false
    rt.llm.push({"reply": "ok", "actions": [{"type": "remember", "note": "Never use Comic Sans"}]})
    await rt.dispatch("sofia", Event("dm", "never use Comic Sans", sender=OWNER))
    assert "Never use Comic Sans" in rt.ws.memory("sofia").split("## Notes")[0]


def test_member_ids_cant_be_the_owner_or_a_room(tmp_path):
    raw = make_raw(tmp_path)
    raw["owner"]["name"] = "James"
    with pytest.raises(ConfigError, match="same as the owner"):
        parse_config(raw, base_dir=tmp_path)
    for bad in ("team", "backchannel", "all"):
        raw = make_raw(tmp_path)
        raw["team"][1]["id"] = bad
        with pytest.raises(ConfigError, match="reserved"):
            parse_config(raw, base_dir=tmp_path)


def test_commands_for_other_bots_and_paths_are_not_commands():
    assert is_command("/status") and is_command("/assign riya \"x\"") and is_command("/pause@marcus_bot")
    assert not is_command("/Users/me/app.log shows the error") and not is_command("/etc/hosts is wrong")


# -- loops and data ----------------------------------------------------------------------------------------
async def test_blocked_ping_pong_stops(tmp_path):
    rt, hub = hubbed(tmp_path)
    rt.cfg.max_agent_hops = 4
    t = rt.tasks.create(title="Page", owner="sofia", created_by=OWNER, done_means=["x"])
    blocked = {"reply": "blocked", "actions": [{"type": "update_task", "id": t.id, "status": "blocked",
                                                "blocked_on": "Marcus — the API"}]}
    for _ in range(40):
        rt.llm.push(blocked)
    await rt.dispatch("sofia", Event("dm", "go", sender=OWNER))
    await rt.drain()
    assert len(rt.llm.calls) < 10                          # was: endless (41+ calls)


async def test_task_owner_given_by_name_becomes_the_id(tmp_path):
    team = [{"id": "james", "name": "James", "role": "Manager", "monitor": True},
            {"id": "mc", "name": "Marcus Chen", "role": "Backend"}]
    rt, hub = hubbed(tmp_path, team=team)
    rt.llm.push({"reply": "ok", "actions": [{"type": "create_task", "title": "Schema", "owner": "Marcus Chen"}]})
    await rt.dispatch("james", Event("dm", "give marcus the schema", sender=OWNER))
    assert rt.tasks.get("T-001").owner == "mc" and rt.tasks.for_owner("mc")


async def test_docs_save_when_the_workspace_path_has_a_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    raw = make_raw(tmp_path)
    raw["workspace"]["path"] = str(tmp_path / "link" / "ws")
    rt = Runtime(parse_config(raw, base_dir=tmp_path), FakeLLM())
    rt.llm.push({"reply": "saved", "actions": [{"type": "write_file", "path": "docs/x.md", "content": "# X"}]})
    out = await rt.dispatch("sofia", Event("dm", "write it", sender=OWNER))
    assert "saved docs/x.md" in out and (real / "ws/docs/x.md").read_text() == "# X\n"


async def test_pause_all_pauses_system_follow_ups_and_queued_teammate_replies_arrive(tmp_path):
    rt, hub = hubbed(tmp_path)
    t = rt.tasks.create(title="x", owner="sofia", created_by=OWNER)
    rt.set_paused("all", True)
    await rt.feedback(t.id, "shorter please")
    await rt.drain()
    assert rt.llm.calls == [] and rt.queued()             # queued, not run
    rt.set_paused("all", False)
    # a teammate's question queued while the recipient was paused still gets answered back
    rt.set_paused("marcus", True)
    await rt._deliver("marcus", Event("inbox", "what's the API URL?", sender="sofia", meta={"reply_to": "sofia"}))
    rt.set_paused("marcus", False)
    for _ in range(3):
        rt.llm.push({"reply": "https://api.acme.dev", "actions": []})
    await rt.drain_queue()
    await rt.drain()
    assert any("Marcus replied: https://api.acme.dev" in c[1][-1]["content"] for c in rt.llm.calls)


def test_memory_keeps_hand_written_text_and_newer_corrections(tmp_path):
    rt = Runtime(parse_config(make_raw(tmp_path), base_dir=tmp_path), FakeLLM())
    rt.ws.write("team/sofia/memory.md", "# Memory — sofia\n\n## Style guide\nPrefer short answers.\n")
    rt.ws.remember("sofia", "Never use Tailwind", pinned=True)
    rt.ws.remember("sofia", "use Tailwind", pinned=True)
    rt.ws.remember("sofia", "met Marcus")
    text = rt.ws.read("team/sofia/memory.md")
    assert "## Style guide" in text and "Prefer short answers." in text
    assert "Never use Tailwind" in text and "— use Tailwind" in text
    mem = rt.ws.memory("sofia")
    assert "Prefer short answers." in mem and "met Marcus" in mem


async def test_done_and_cut_are_final(tmp_path):
    rt, hub = hubbed(tmp_path)
    t = rt.tasks.create(title="x", owner="sofia", created_by=OWNER)
    await rt.accept(t.id)
    with pytest.raises(TaskError, match="already done"):
        await rt.accept(t.id)
    with pytest.raises(TaskError, match="already done"):
        rt.tasks.set_status(t.id, "cut", by=OWNER)


async def test_two_approvals_at_once_decide_once(tmp_path):
    rt, hub = hubbed(tmp_path)
    ask = rt.asks.create(requester="marcus", summary="Pay", level="red")
    for _ in range(3):
        rt.llm.push({"reply": "ok", "actions": []})
    results = await asyncio.gather(rt.decide_ask(ask.id, "approved", by="P"), rt.decide_ask(ask.id, "approved", by="P"),
                                   return_exceptions=True)
    assert sum(isinstance(r, str) for r in results) == 1 and "already approved" in str(
        [r for r in results if not isinstance(r, str)][0])


# -- channels and routing -------------------------------------------------------------------------------
def test_status_and_mentions_are_precise(cfg):
    assert is_status_request("@all status") and is_status_request("@all give status") and is_status_request("standup?")
    assert not is_status_request("@all update the README before Friday")
    assert room_targets("team", "cc ops@team.io on it", cfg) == (False, ["james"])     # an email isn't @team


# -- model replies ------------------------------------------------------------------------------------
def test_the_real_answer_wins_over_an_example():
    p = parse_reply('Format: {"reply": "...", "actions": []}. Answer: {"reply": "Real.", "actions": '
                    '[{"type": "remember", "note": "x"}]}')
    assert p.reply == "Real." and p.actions[0]["type"] == "remember"
    assert parse_reply("{" * 40 + ' {"reply": "ok", "actions": []}').reply == "ok"


async def test_a_file_block_with_spaces_saves_and_an_empty_write_is_refused(tmp_path):
    rt, hub = hubbed(tmp_path)
    rt.llm.push('{"reply": "saved", "actions": [{"type": "write_file", "path": "docs/my plan.md"}]}\n'
                '<<<FILE docs/my plan.md>>>\n# Plan\nmentions <<<END>>> inline\n<<<END>>>')
    out = await rt.dispatch("sofia", Event("dm", "write", sender=OWNER))
    assert "saved docs/my plan.md" in out and "inline" in (rt.ws.root / "docs/my plan.md").read_text()
    rt.llm.push({"reply": "saved", "actions": [{"type": "write_file", "path": "docs/my plan.md"}]})
    rt.llm.push({"reply": "sorry", "actions": []})
    out = await rt.dispatch("sofia", Event("dm", "again", sender=OWNER))
    assert "no content" in out and "# Plan" in (rt.ws.root / "docs/my plan.md").read_text()   # not wiped


# -- reports and small things -----------------------------------------------------------------------------
async def test_report_counts_and_ships_once(tmp_path):
    from james_monitoring.monitor import build_report, write_report
    rt, hub = hubbed(tmp_path)
    t = rt.tasks.create(title="Pricing", owner="sofia", created_by=OWNER)
    await rt.accept(t.id)
    first = build_report(rt.cfg, rt.ws, rt.tasks, rt.asks)
    assert "T-001 Pricing" in first and "James: not started" not in first
    write_report(rt.cfg, rt.ws, rt.tasks, rt.asks)
    assert "T-001 Pricing" not in build_report(rt.cfg, rt.ws, rt.tasks, rt.asks).split("## People")[0]


async def test_model_errors_from_an_old_model_dont_linger(tmp_path):
    rt, hub = hubbed(tmp_path)
    rt._heartbeat("sofia", error="claude CLI failed: expired")
    assert rt.model_error("sofia")
    rt.cfg.llm.model = "other"                              # switched model
    assert rt.model_error("sofia") == ""


def test_same_output_is_one_line_and_logs_have_one_date(tmp_path):
    rt = Runtime(parse_config(make_raw(tmp_path), base_dir=tmp_path), FakeLLM())
    t = rt.tasks.create(title="x", owner="sofia", created_by=OWNER)
    rt.tasks.set_output(t.id, "sofia", "docs/a.md")
    rt.tasks.set_output(t.id, "sofia", "docs/a.md")
    rt.tasks.add_log(t.id, "sofia", "2026-09-28 — drafted the spec")
    t = rt.tasks.get(t.id)
    assert t.doc.sections["Output"].count("docs/a.md") == 1
    assert "sofia: drafted the spec" in t.doc.sections["Log"]


def test_settings_save_keeps_a_custom_key_and_coding_tool(tmp_path):
    from james_monitoring.server import App
    app = App(tmp_path / "co", telegram=False)
    app.base.mkdir(parents=True)
    try:
        app.setup({"company": "A", "owner": "Maria", "provider": "fake"})
        raw = app.raw()
        raw["llm"].update(provider="openai", model="m", base_url="https://openrouter.ai/api/v1",
                          api_key_env="OPENROUTER_API_KEY")
        raw["executor"]["command"] = ["aider", "--yes", "{prompt}"]
        app.save_raw(raw)
        app.load()
        app.settings_save({"company": "A2", "provider": "openai", "coding_tool": "custom"})
        raw = app.raw()
        assert raw["llm"]["api_key_env"] == "OPENROUTER_API_KEY" and raw["executor"]["command"][0] == "aider"
        with pytest.raises(ValueError, match="already a project"):
            app.project_save({"name": "Mobile app"}) and app.project_save({"name": "Mobile App"})
        t = app.task_create({"title": "x", "owner": "maria" if app.rt.cfg.member("maria") else "james",
                             "due": "2099-01-01", "notify": False})
        app.task_action({"id": t["id"], "action": "edit", "due": ""})
        assert app.task_detail(t["id"])["due"] == ""
    finally:
        app.submit(app._stop_services(app.sched, app.gw, app.slack), timeout=10)
        app.loop.call_soon_threadsafe(app.loop.stop)


def test_non_text_personas_are_refused():
    from james_monitoring.skills import SkillError, from_bytes
    with pytest.raises(SkillError, match="not a text file"):
        from_bytes("deck.pdf", b"%PDF-1.7 ...")
    with pytest.raises(SkillError, match="not a text file"):
        from_bytes("notes.md", b"%PDF-1.7 ...")
    assert from_bytes("SKILL.md", b"# Bob - Dev\nbody").suggested_role() == "Dev"

