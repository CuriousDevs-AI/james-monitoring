"""Slack transport with a fake Slack Web API (no network)."""
import asyncio

from james_monitoring.config import parse_config
from james_monitoring.hub import Hub
from james_monitoring.llm.fake import FakeLLM
from james_monitoring.runtime import Runtime
from james_monitoring.slack import SlackTransport, channel_name, provision

from .conftest import make_raw

CHANNELS = {"team": "C_HQ", "p-site": "C_SITE", "sofia": "G_SOFIA", "backchannel": "G_BACK"}


class FakeWeb:
    def __init__(self):
        self.posts, self.updates, self.created, self.invited = [], [], [], []
        self.existing = [{"name": "jm-p-site", "id": "C_SITE"}]

    def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ok": True, "ts": str(len(self.posts))}

    def chat_update(self, **kw):
        self.updates.append(kw)

    def conversations_open(self, users):
        return {"channel": {"id": "D_OWNER"}}

    def conversations_list(self, **kw):
        return {"channels": self.existing, "response_metadata": {"next_cursor": ""}}

    def conversations_create(self, name, is_private):
        self.created.append((name, is_private))
        return {"channel": {"id": "C_" + name.upper()}}

    def conversations_join(self, channel):
        pass

    def conversations_invite(self, channel, users):
        self.invited.append((channel, users))


def make(tmp_path, channels=CHANNELS):
    raw = make_raw(tmp_path, slack={"owner_user_ids": ["U0OWNER1"], "channels": channels})
    cfg = parse_config(raw, base_dir=tmp_path)
    hub = Hub()
    rt = Runtime(cfg, FakeLLM(), bus=hub)
    hub.attach(rt)
    web = FakeWeb()
    sl = SlackTransport(cfg, web=web)
    sl.bind(rt, hub)
    sl.bot_user_id = "UBOT"
    return rt, hub, sl, web


def test_events_map_to_rooms_and_only_the_owner_commands(tmp_path):
    rt, hub, sl, web = make(tmp_path)
    ev = lambda **k: {"type": "message", "user": "U0OWNER1", "text": "hi", "channel": "C_HQ", **k}  # noqa: E731
    assert sl.event_room(ev()) == ("team", "hi")
    assert sl.event_room(ev(channel="C_SITE", text="<@UBOT> @all status")) == ("p-site", "@all status")
    assert sl.event_room(ev(channel="G_SOFIA", text="use &lt;b&gt; tags <https://x.dev|the doc>")) == \
        ("sofia", "use <b> tags the doc")
    assert sl.event_room(ev(text="!board")) == ("team", "/board")
    assert sl.event_room(ev(channel="D1", channel_type="im", text="what's blocked?")) == ("james", "what's blocked?")
    assert sl.event_room(ev(channel="D1", channel_type="im", text="@sofia ship it")) == ("sofia", "ship it")
    assert sl.event_room(ev(channel="G_BACK")) == (None, "")                     # read-only
    assert sl.event_room(ev(channel="C_OTHER")) == (None, "")                     # not ours
    assert sl.event_room(ev(bot_id="B1")) == (None, "")                           # our own posts echo back
    assert sl.event_room(ev(subtype="channel_join")) == (None, "")
    assert sl.event_room(ev(user="U0STRANGER")) == (None, "") and sl.last_unknown_user == ""   # strangers aren't owners
    sl.detect_code = "482913"
    assert sl.event_room(ev(user="U0STRANGER", text="hi")) == (None, "") and sl.last_unknown_user == ""
    sl.event_room(ev(user="U0ME", text="482913"))
    assert sl.last_unknown_user == "U0ME"                                     # only who sends the code


async def test_messages_and_cards_post_as_each_person(tmp_path):
    rt, hub, sl, web = make(tmp_path)
    rt.llm.push({"reply": "Hero redesign in review tomorrow.", "actions": []})
    await hub.inbound("sofia", "status of the hero?", via="telegram")
    who = [(p["channel"], p["username"], p["text"]) for p in web.posts]
    assert who == [("G_SOFIA", "Pankaj (via telegram)", "status of the hero?"),
                   ("G_SOFIA", "Sofia", "Hero redesign in review tomorrow.")]

    ask = rt.asks.create(requester="sofia", summary="Pay for Figma seat ($15/mo)", level="red")
    await hub.send_owner("sofia", ask.summary, ask=ask)
    card = web.posts[-1]
    buttons = card["blocks"][1]["elements"]
    assert card["channel"] == "G_SOFIA" and [b["value"] for b in buttons] == ["ASK-001", "ASK-001"]

    # Approve in Slack → decided, card updated, the requester answers
    sl.loop = asyncio.get_running_loop()
    rt.llm.push({"reply": "Buying the seat.", "actions": []})
    fut = sl.on_action({"type": "block_actions", "user": {"id": "U0OWNER1"},
                        "actions": [{"action_id": "jm_approve", "value": "ASK-001"}],
                        "channel": {"id": "G_SOFIA"}, "message": {"ts": "9.1", "text": card["text"]}})
    result = await asyncio.wrap_future(fut)
    await rt.drain()
    assert "approved" in result and rt.asks.get("ASK-001").status == "approved"
    assert web.updates and "approved" in web.updates[0]["text"]
    assert any(p["text"] == "Buying the seat." for p in web.posts)
    # a stranger's click does nothing
    assert sl.on_action({"type": "block_actions", "user": {"id": "U0X"}, "actions": [{"action_id": "jm_reject"}]}) is None


async def test_unmapped_dm_rooms_fall_back_to_the_app_dm(tmp_path):
    rt, hub, sl, web = make(tmp_path, channels={"team": "C_HQ"})
    await hub.send_owner("marcus", "Schema is ready for review.")
    assert (web.posts[-1]["channel"], web.posts[-1]["username"]) == ("D_OWNER", "Marcus")
    n = len(web.posts)
    await hub.post_room("p-site", "sofia", "not mapped")                          # project rooms: only if mapped
    assert len(web.posts) == n


def test_channel_names_and_provisioning(tmp_path):
    rt, hub, sl, web = make(tmp_path, channels={})
    assert channel_name("Curious Devs!", "team") == "jm-hq"
    assert channel_name("x", "p-site") == "jm-p-site" and channel_name("x", "sofia") == "jm-dm-sofia"
    made = provision(web, rt.cfg, ["team", "p-site", "sofia"])
    assert made["p-site"] == "C_SITE"                                             # reused, not recreated
    assert ("jm-hq", False) in web.created and ("jm-dm-sofia", True) in web.created
    assert {c for c, _ in web.invited} == set(made.values())


async def test_here_threads_and_slash_commands(tmp_path):
    rt, hub, sl, web = make(tmp_path)
    ev = lambda **k: {"type": "message", "user": "U0OWNER1", "channel": "C_SITE", **k}  # noqa: E731
    assert sl.event_room(ev(text="<!here> standup in 5")) == ("p-site", "@here standup in 5")
    from james_monitoring.router import room_targets
    assert room_targets("p-site", "@here standup in 5", rt.cfg)[0]                  # everyone in the room
    # a thread reply goes to whoever wrote the parent message
    await hub.post_room("p-site", "sofia", "Pricing page is ready")
    parent = web.posts[-1]
    sl.authors[("C_SITE", "1.5")] = "sofia"
    assert sl.event_room(ev(text="looks great, ship it", ts="2.0", thread_ts="1.5")) == \
        ("p-site", "@sofia looks great, ship it")
    assert parent["username"] == "Sofia"
    # /jm in a channel runs in that room; strangers get nothing
    assert sl.slash_room({"user_id": "U0OWNER1", "channel_id": "C_HQ", "text": "status"}) == ("team", "/status")
    assert sl.slash_room({"user_id": "U0OWNER1", "channel_id": "D9", "text": ""}) == ("james", "/help")
    assert sl.slash_room({"user_id": "U0X", "channel_id": "C_HQ", "text": "pause all"}) == (None, "")
