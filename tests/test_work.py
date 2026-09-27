
from james_monitoring.llm.fake import FakeLLM
from james_monitoring.runtime import ConsoleBus, Event, Runtime


async def test_write_file_saves_work_and_links_task(rt):
    t = rt.tasks.create(title="Runtime survey", owner="sofia", created_by="pankaj", done_means=["note"])
    rt.llm.push({"reply": "Survey written.", "actions": [
        {"type": "write_file", "path": "docs/ojas/runtime-survey.md", "content": "# Survey\nROS 2 wins.", "task": t.id},
        {"type": "update_task", "id": t.id, "status": "review"}]})
    out = await rt.dispatch("sofia", Event("dm", "write the survey", sender="pankaj"))
    assert "saved docs/ojas/runtime-survey.md" in out
    assert (rt.ws.root / "docs/ojas/runtime-survey.md").read_text() == "# Survey\nROS 2 wins.\n"
    assert "docs/ojas/runtime-survey.md" in rt.tasks.get(t.id).doc.sections["Output"]


async def test_write_file_cannot_escape_docs(rt):
    for bad in ["../evil.md", "docs/../../evil.md", "/etc/passwd", "tasks/../../x"]:
        rt.llm.push({"reply": "x", "actions": [{"type": "write_file", "path": bad, "content": "x"}]})
        out = await rt.dispatch("sofia", Event("dm", "go", sender="pankaj"))
        assert "saved" not in out or "docs/" in out.split("saved ")[1]
    assert not (rt.ws.root.parent / "evil.md").exists()


async def test_read_file_round_trip(rt):
    (rt.ws.root / "docs").mkdir(exist_ok=True)
    (rt.ws.root / "docs/spec.md").write_text("API: GET /tenants")
    rt.llm.push({"reply": "", "actions": [{"type": "read_file", "path": "docs/spec.md"},
                                         {"type": "read_file", "path": ".jm/state.json"}]})
    rt.llm.push({"reply": "Spec says GET /tenants.", "actions": []})
    out = await rt.dispatch("marcus", Event("dm", "what's in the spec?", sender="pankaj"))
    assert out == "Spec says GET /tenants."
    fed = rt.llm.calls[-1][1][-1]["content"]
    assert "API: GET /tenants" in fed and "cannot read" in fed          # .jm/ is off limits


async def test_history_survives_restart(cfg):
    rt1 = Runtime(cfg, FakeLLM(), bus=ConsoleBus(quiet=True))
    rt1.llm.push({"reply": "Noted: brand blue.", "actions": []})
    await rt1.dispatch("sofia", Event("dm", "our brand colour is blue", sender="pankaj"))
    rt2 = Runtime(cfg, FakeLLM(), bus=ConsoleBus(quiet=True))           # "restart"
    await rt2.dispatch("sofia", Event("dm", "what colour?", sender="pankaj"))
    msgs = rt2.llm.calls[-1][1]
    assert any("brand colour is blue" in m["content"] for m in msgs)


async def test_work_session_moves_open_tasks_only(rt):
    rt.tasks.create(title="Pricing page", owner="sofia", created_by="pankaj", priority="P0", done_means=["x"])
    rt.set_paused("marcus", True)
    rt.tasks.create(title="Schema", owner="marcus", created_by="pankaj", done_means=["x"])
    rt.llm.push({"reply": "Drafted the pricing page copy; next: layout.", "actions": []})
    digest = await rt.run_work_session()
    assert "Sofia (T-001): Drafted the pricing page copy" in digest
    assert "Marcus" not in digest                                        # paused
    assert len(rt.llm.calls) == 1                                        # James and idle people cost nothing
