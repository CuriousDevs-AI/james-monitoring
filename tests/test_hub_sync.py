"""The hub: one conversation across console, Telegram and Slack; agents share what was said; approvals gate actions."""
import asyncio

from james_monitoring.chat import BACKCHANNEL, TEAM_ROOM
from james_monitoring.config import parse_config
from james_monitoring.hub import Hub
from james_monitoring.llm.fake import FakeLLM
from james_monitoring.router import room_targets
from james_monitoring.runtime import Event, Runtime

from .conftest import make_raw


class FakeTransport:
    def __init__(self, name):
        self.name, self.got = name, []

    async def deliver(self, room, msg, ask=None):
        self.got.append((room, msg["who"], msg["text"], msg.get("kind"), ask.id if ask else None))


def make(tmp_path, **over):
    raw = make_raw(tmp_path, **over)
    cfg = parse_config(raw, base_dir=tmp_path)
    hub = Hub()
    rt = Runtime(cfg, FakeLLM(), bus=hub)
    hub.attach(rt)
    tg, sl = FakeTransport("telegram"), FakeTransport("slack")
    hub.add(tg)
    hub.add(sl)
    return rt, hub, tg, sl


async def test_one_conversation_every_channel(tmp_path):
    rt, hub, tg, sl = make(tmp_path)          # both connected, no choice made → rooms live on Telegram
    rt.llm.push({"reply": "Sticky navbar is live on the branch.", "actions": []})
    await hub.inbound("sofia", "make the navbar sticky", via="telegram")
    msgs = rt.chat.since("sofia")
    assert [(m["who"], m.get("via")) for m in msgs] == [("pankaj", "telegram"), ("sofia", None)]
    assert [g[2] for g in tg.got] == ["Sticky navbar is live on the branch."]     # no echo of its own message
    assert sl.got == []                                                           # one room, one channel
    # from the console: shown on the room's channel too (mirror), unless switched off
    rt.llm.push({"reply": "ok", "actions": []})
    await hub.inbound("sofia", "thanks", via="console")
    assert [g[2] for g in tg.got][-2:] == ["thanks", "ok"]
    rt.cfg.mirror_owner = False
    rt.llm.push({"reply": "sure", "actions": []})
    await hub.inbound("sofia", "one more", via="console")
    assert [g[2] for g in tg.got][-1] == "sure" and "one more" not in [g[2] for g in tg.got]


async def test_each_room_lives_on_exactly_one_channel(tmp_path):
    rt, hub, tg, sl = make(tmp_path, projects={"site": {"channel": "slack"}}, sync={"channel": "telegram"})
    rt.llm.push({"reply": "Plan: ship Friday.", "actions": []})
    await hub.inbound("p-site", "plan?", via="slack")
    assert [g[2] for g in sl.got] == ["Plan: ship Friday."] and tg.got == []        # the project is on Slack
    calls = len(rt.llm.calls)
    await hub.inbound("p-site", "plan?", via="telegram")                            # wrong place → pointed home
    assert len(rt.llm.calls) == calls and "lives on Slack" in tg.got[-1][2]
    assert [m["text"] for m in rt.chat.since("p-site")] == ["plan?", "Plan: ship Friday."]
    rt.llm.push({"reply": "Hi!", "actions": []})
    await hub.inbound("sofia", "hi", via="telegram")                               # 1:1s follow the company choice
    assert tg.got[-1][2] == "Hi!" and all(g[0] != "sofia" for g in sl.got)
    rt.cfg.projects["site"].channel = "console"
    await hub.post_room("p-site", "sofia", "console only now")
    assert "console only now" not in [g[2] for g in sl.got + tg.got]


async def test_backchannel_is_read_only_and_bad_rooms_fail(tmp_path):
    rt, hub, *_ = make(tmp_path)
    for room in (BACKCHANNEL, "nobody", "p-nope"):
        try:
            hub.receive(room, "hi")
        except ValueError:
            continue
        raise AssertionError(f"{room} should be refused")


async def test_project_people_see_what_was_said_in_the_room(tmp_path):
    raw_team = [
        {"id": "james", "name": "James", "role": "Manager", "monitor": True},
        {"id": "riya", "name": "Riya", "role": "Backend", "projects": ["api"]},
        {"id": "lena", "name": "Lena", "role": "Frontend", "projects": ["api"]},
    ]
    rt, hub, *_ = make(tmp_path, team=raw_team, projects={"api": {"lead": "riya"}})
    rt.llm.push({"reply": "Rate limits: 100 req/min per key, shipping Thursday.", "actions": []})
    await hub.inbound("p-api", "@riya what's the rate limit plan?", via="console")
    rt.llm.push({"reply": "I'll show the limit in the dashboard.", "actions": []})
    await hub.inbound("p-api", "@lena can you surface it in the UI?", via="console")
    system = rt.llm.calls[-1][0]
    assert "Recent messages in #api room" in system
    assert "Riya: Rate limits: 100 req/min per key" in system            # Lena knows what Riya said
    assert "what's the rate limit plan?" in system                       # …and what the founder asked

    # a DM later: Riya's context shows her project room at a glance
    rt.llm.push({"reply": "Yes.", "actions": []})
    await hub.inbound("riya", "all good?", via="console")
    assert "Elsewhere lately" in rt.llm.calls[-1][0] and "surface it in the UI" in rt.llm.calls[-1][0]


async def test_teammate_handoffs_are_visible_in_the_project_room(tmp_path):
    raw_team = [
        {"id": "james", "name": "James", "role": "Manager", "monitor": True},
        {"id": "riya", "name": "Riya", "role": "Backend", "projects": ["api"]},
        {"id": "lena", "name": "Lena", "role": "Frontend", "projects": ["api"]},
    ]
    rt, hub, tg, sl = make(tmp_path, team=raw_team, projects={"api": {"lead": "riya", "channel": "slack"}})
    rt.llm.push({"reply": "Asked Lena.", "actions": [{"type": "message_agent", "to": "lena",
                                                       "text": "Need the 429 screen by Friday"}]})
    rt.llm.push({"reply": "Will do — Thursday.", "actions": []})
    await hub.inbound("p-api", "coordinate the 429 screen", via="console")
    await rt.drain()
    internal = [m for m in rt.chat.since("p-api") if m["kind"] == "internal"]
    assert [m["who"] for m in internal][:2] == ["riya", "lena"]
    assert "→ Lena: Need the 429 screen" in internal[0]["text"] and "Thursday" in internal[1]["text"]
    assert [g[2] for g in sl.got if g[3] == "internal"] == [m["text"] for m in internal]   # Slack shows team talk
    # without a project it goes to the backchannel
    rt.llm.push({"reply": "ok", "actions": [{"type": "message_agent", "to": "lena", "text": "lunch?"}]})
    rt.llm.push({"reply": "sure", "actions": []})
    await hub.inbound("riya", "sync with lena", via="console")
    await rt.drain()
    assert any("lunch?" in m["text"] for m in rt.chat.since(BACKCHANNEL))


async def test_red_actions_wait_for_approval_then_run(tmp_path):
    rt, hub, tg, sl = make(tmp_path, permissions={"post_group": "red", "write_file": "yellow"})
    rt.llm.push({"reply": "Announcing.", "actions": [
        {"type": "post_group", "text": "We launched the beta!"},
        {"type": "write_file", "path": "docs/launch.md", "content": "# Launch"}]})
    out = await rt.dispatch("marcus", Event("dm", "tell everyone", sender="pankaj"))
    assert "ASK-001: waiting for Pankaj to approve" in out
    assert not any(m["text"] == "We launched the beta!" for m in rt.chat.since(TEAM_ROOM))   # not yet
    card = [m for m in rt.chat.since("marcus") if m["kind"] == "ask"][0]
    assert "post in All hands: We launched the beta!" in card["text"] and card["ask_id"] == "ASK-001"
    assert any(g[4] == "ASK-001" for g in tg.got)                        # the card reached Telegram with its id
    # yellow: done, and the owner is told
    assert (rt.ws.root / "docs/launch.md").exists()
    assert any("🟡 FYI" in m["text"] and "docs/launch.md" in m["text"] for m in rt.chat.since("marcus"))

    rt.llm.push({"reply": "Posted.", "actions": []})
    res = await rt.decide_ask("ASK-001", "approved", by="Pankaj", via="slack")
    await rt.drain()
    assert "done" in res
    assert any(m["who"] == "marcus" and m["text"] == "We launched the beta!" for m in rt.chat.since(TEAM_ROOM))
    settled = [m for m in rt.chat.since("marcus") if m["kind"] == "system" and "ASK-001 approved" in m["text"]]
    assert settled and settled[0].get("via") == "slack"
    assert not any(g[3] == "system" and "ASK-001 approved" in g[2] for g in sl.got)   # Slack already shows it


async def test_rejected_action_never_runs(tmp_path):
    rt, hub, *_ = make(tmp_path, permissions={"create_task": "red"})
    rt.llm.push({"reply": "ok", "actions": [{"type": "create_task", "title": "Buy ads", "owner": "sofia"}]})
    await rt.dispatch("marcus", Event("dm", "plan ads", sender="pankaj"))
    rt.llm.push({"reply": "Understood.", "actions": []})
    await rt.decide_ask("ASK-001", "rejected", by="Pankaj", note="not this quarter")
    await rt.drain()
    assert rt.tasks.all() == []


async def test_each_person_can_run_on_their_own_model(tmp_path):
    team = [
        {"id": "james", "name": "James", "role": "Manager", "monitor": True},
        {"id": "marcus", "name": "Marcus", "role": "Backend", "llm": {"provider": "fake", "model": "codex-ish"}},
        {"id": "sofia", "name": "Sofia", "role": "Frontend"},
    ]
    rt, *_ = make(tmp_path, team=team)
    assert rt.llm_for(rt.cfg.member("sofia")) is rt.llm
    own = rt.llm_for(rt.cfg.member("marcus"))
    assert own is not rt.llm and rt.llm_for(rt.cfg.member("marcus")) is own
    assert rt.model_name(rt.cfg.member("marcus")) == "fake/codex-ish"
    assert rt.model_name(rt.cfg.member("sofia")) == "fake/fake"


def test_member_llm_inherits_and_switches(tmp_path):
    raw = make_raw(tmp_path, llm={"provider": "anthropic", "model": "m1", "max_tokens": 9000})
    raw["team"][1]["llm"] = {"provider": "anthropic", "model": "m2"}
    raw["team"][2]["llm"] = {"provider": "codex-cli"}
    cfg = parse_config(raw, base_dir=tmp_path)
    sofia, marcus = cfg.member("sofia"), cfg.member("marcus")
    assert (sofia.llm.model, sofia.llm.max_tokens, sofia.llm.api_key_env) == ("m2", 9000, "ANTHROPIC_API_KEY")
    assert (marcus.llm.provider, marcus.llm.model, marcus.llm.api_key_env) == ("codex-cli", "", "")


def test_room_targets(cfg):
    assert room_targets("sofia", "hi", cfg) == (False, ["sofia"])
    assert room_targets("team", "hello", cfg) == (False, ["james"])
    assert room_targets("team", "@all status", cfg)[0]
    assert room_targets("p-site", "@all plan?", cfg) == (True, ["sofia"])
    assert room_targets("p-site", "@marcus can you help?", cfg) == (False, ["marcus"])
    assert room_targets("p-site", "no mention", cfg) == (False, ["james"])     # no lead → the manager


async def test_report_shows_real_work_projects_and_decisions(tmp_path):
    from james_monitoring.monitor import build_report
    rt, hub, *_ = make(tmp_path)
    t = rt.tasks.create(title="Pricing page", owner="sofia", created_by="pankaj", project="site", done_means=["x"])
    rt.llm.push({"reply": "Draft saved.", "actions": [
        {"type": "write_file", "path": "docs/site/pricing.md", "content": "# Pricing", "task": t.id},
        {"type": "update_task", "id": t.id, "status": "doing", "log": "first draft of the tiers"}]})
    await rt.dispatch("sofia", Event("dm", "go", sender="pankaj"))
    ask = rt.asks.create(requester="marcus", summary="Buy a domain", level="red")
    rt.llm.push({"reply": "ok", "actions": []})
    await rt.decide_ask(ask.id, "approved", by="Pankaj")
    await rt.drain()
    text = build_report(rt.cfg, rt.ws, rt.tasks, rt.asks)
    assert "## Today's work" in text and "first draft of the tiers" in text and "wrote docs/site/pricing.md" in text
    assert "Marcus: no recorded work" in text
    assert "## Projects" in text and "site [active]: 0/1 done" in text
    assert "ASK-001 approved by Pankaj: Buy a domain" in text
    assert "## Model use today" in text


def test_codex_usage_parser():
    from james_monitoring.llm.codex_cli_llm import _usage
    out = "\n".join(['{"type":"thread.started"}', 'not json',
                     '{"type":"turn.completed","usage":{"input_tokens":1200,"cached_input_tokens":800,"output_tokens":90}}'])
    assert _usage(out) == (1200, 90, 800)


async def test_hub_command_and_status_fast_path(tmp_path):
    rt, hub, tg, sl = make(tmp_path)
    await hub.inbound("team", "/status", via="console")
    sysmsg = [m for m in rt.chat.since("team") if m["kind"] == "system"]
    assert sysmsg and sysmsg[0]["who"] == "james" and "Sofia" in sysmsg[0]["text"]
    calls = len(rt.llm.calls)
    await hub.inbound("team", "@all status", via="console")
    assert len(rt.llm.calls) == calls and {m["who"] for m in rt.chat.since("team")} >= {"james", "sofia", "marcus"}
    await asyncio.sleep(0)
