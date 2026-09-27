import subprocess

from james_monitoring.commands import run_command
from james_monitoring.runtime import Event


async def test_dm_creates_task_commits_and_captures_feedback(rt):
    rt.llm.push({"reply": "On it.", "actions": [
        {"type": "create_task", "title": "Hero section redesign", "priority": "P1", "due": "2099-10-03",
         "done_means": ["new hero live on branch"]}]})
    out = await rt.dispatch("sofia", Event("dm", "Redo the hero section", sender="pankaj"))
    assert "On it." in out and "T-001 created" in out
    t = rt.tasks.get("T-001")
    assert t.owner == "sofia"
    log = subprocess.run(["git", "log", "--oneline"], cwd=rt.ws.root, capture_output=True, text=True).stdout
    assert "Sofia: dm from Pankaj" in log

    rt.llm.push({"reply": "Got it, fixing.", "actions": [{"type": "remember", "note": "Hero must use brand blue"}]})
    await rt.dispatch("sofia", Event("dm", "T-001 not like this — use brand blue", sender="pankaj"))
    assert "use brand blue" in rt.tasks.get("T-001").doc.sections["Feedback"]
    assert "Hero must use brand blue" in rt.ws.memory("sofia")


async def test_permission_request_and_decision_flow(rt):
    rt.llm.push({"reply": "Need your OK.", "actions": [
        {"type": "ask_permission", "summary": "Buy domain curiousdevs.ai (₹6k)", "level": "red", "default": "approve"}]})
    await rt.dispatch("marcus", Event("dm", "get us the domain", sender="pankaj"))
    ask = rt.asks.get("ASK-001")
    assert ask.default == "wait"                       # red never auto-approves
    assert any("ASK-001" in text for ch, _, text in rt.bus.sent if ch == "owner")
    rt.llm.push({"reply": "Buying it now.", "actions": []})
    res = await rt.decide_ask("ASK-001", "approved", by="Pankaj")
    await rt.drain()
    assert "approved" in res and rt.asks.get("ASK-001").status == "approved"
    assert ("owner", "marcus", "Buying it now.") in rt.bus.sent
    # the requester was told
    assert "was approved by Pankaj" in rt.llm.calls[-1][1][-1]["content"]


async def test_agent_to_agent_handoff_and_loop_stop(rt):
    rt.cfg.max_agent_hops = 2
    # every reply pings the other agent -> would loop forever without the hop limit
    for _ in range(10):
        rt.llm.push({"reply": "ok", "actions": [{"type": "message_agent", "to": "marcus", "text": "ping"}]})
        rt.llm.push({"reply": "ok", "actions": [{"type": "message_agent", "to": "sofia", "text": "pong"}]})
    await rt.dispatch("sofia", Event("dm", "coordinate with marcus", sender="pankaj"))
    await rt.drain()
    assert any("Loop stopped" in text for _, _, text in rt.bus.sent)
    assert "from sofia: ping" in rt.ws.tail_log("marcus")


async def test_pause_and_budget(rt):
    rt.set_paused("sofia", True)
    assert "paused" in await rt.dispatch("sofia", Event("dm", "hi", sender="pankaj"))
    rt.set_paused("sofia", False)
    rt.cfg.daily_tokens_per_agent = 1
    await rt.dispatch("sofia", Event("dm", "hi", sender="pankaj"))
    assert "budget" in await rt.dispatch("sofia", Event("dm", "again", sender="pankaj"))


async def test_bad_action_is_reported_not_crashing(rt):
    rt.llm.push({"reply": "Starting.", "actions": [{"type": "update_task", "id": "T-999", "status": "doing"}]})
    out = await rt.dispatch("sofia", Event("dm", "start", sender="pankaj"))
    assert "Starting." in out and "⚠️ update_task: No task T-999" in out


async def test_review_notifies_owner(rt):
    t = rt.tasks.create(title="Pricing page", owner="sofia", created_by="pankaj", done_means=["page done"])
    rt.llm.push({"reply": "Done — ready for review.", "actions": [
        {"type": "update_task", "id": t.id, "status": "done", "output": "branch jm/T-001"}]})
    out = await rt.dispatch("sofia", Event("dm", "status?", sender="pankaj"))
    assert rt.tasks.get(t.id).status == "review"            # agents can't self-approve
    assert "Only pankaj can mark done" in out
    assert any("ready for your review" in text for _, _, text in rt.bus.sent)


async def test_commands(rt):
    out = await run_command(rt, "assign", 'sofia "Fix navbar on mobile" P0 due:2099-10-03', "james", True,
                            background=False)
    assert "T-001 [P0] Fix navbar on mobile — sofia" in out
    assert rt.tasks.get("T-001").project == "site"
    assert "Sofia" in await run_command(rt, "status", "", "james", True)
    await run_command(rt, "pause", "all", "james", False)
    assert rt.paused("marcus")
    assert any("paused" in text for ch, _, text in rt.bus.sent if ch == "group")
    await run_command(rt, "resume", "all", "james", False)
    assert not rt.paused("marcus")
    assert "done" in await run_command(rt, "accept", "T-001 looks good", "james", True)
    out = await run_command(rt, "onboard", "", "james", False)
    assert len([1 for ch, _, _ in rt.bus.sent if ch == "group"]) >= 3


async def test_monitor_context_has_board(rt):
    rt.tasks.create(title="ICP shortlist", owner="marcus", created_by="pankaj")
    await rt.dispatch("james", Event("dm", "what's the delivery status?", sender="pankaj"))
    system = rt.llm.calls[-1][0]
    assert "## Board (all tasks)" in system and "ICP shortlist" in system
    assert "run_code" not in system                          # James coordinates, doesn't code
