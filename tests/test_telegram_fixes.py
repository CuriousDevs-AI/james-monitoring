"""Telegram hardening (audit H29–H32 and the Telegram Medium items), with fake bots — no network."""
from types import SimpleNamespace

from telegram.error import ChatMigrated, Forbidden, RetryAfter

from james_monitoring.gateway import TelegramGateway, to_html
from james_monitoring.hub import Hub
from james_monitoring.llm.fake import FakeLLM
from james_monitoring.runtime import Runtime


class Bot:
    def __init__(self, name, sent, fail=None):
        self.name, self.sent, self.fail, self.edits = name, sent, list(fail or []), []

    async def send_message(self, chat_id, text, **kw):
        if self.fail:
            raise self.fail.pop(0)
        self.sent.append((self.name, chat_id, text))
        return SimpleNamespace(message_id=len(self.sent))

    async def edit_message_text(self, **kw):
        self.edits.append(kw)

    async def send_chat_action(self, *a, **k):
        pass


def setup(cfg, fails=None):
    sent = []
    gw = TelegramGateway(cfg)
    gw.apps = {m.id: SimpleNamespace(bot=Bot(m.id, sent, (fails or {}).get(m.id))) for m in cfg.team}
    gw.usernames = {m.id: f"{m.id}_cd_bot" for m in cfg.team}
    hub = Hub()
    rt = Runtime(cfg, FakeLLM(), bus=hub)
    hub.attach(rt)
    gw.bind(rt, hub)
    return gw, rt, sent


def upd(text, chat_type="private", chat_id=111, msg_id=5, reply_to_bot=None):
    reply = SimpleNamespace(from_user=SimpleNamespace(is_bot=True, username=reply_to_bot)) if reply_to_bot else None
    return SimpleNamespace(effective_message=SimpleNamespace(text=text, message_id=msg_id, reply_to_message=reply),
                           effective_chat=SimpleNamespace(type=chat_type, id=chat_id), effective_user=SimpleNamespace(id=111))


async def test_same_message_twice_is_answered_once(cfg):
    gw, rt, sent = setup(cfg)
    rt.llm.push({"reply": "Task created.", "actions": [{"type": "create_task", "title": "Footer"}]})
    for _ in range(2):                                          # a redelivery / duplicate update
        await gw._make_text_handler("sofia")(upd("add a footer"), None)
    assert len(rt.tasks.all()) == 1 and [s[2] for s in sent] == ["Task created.\n\n📝 T-001 created → sofia"]


def test_only_new_messages_reach_handlers(cfg, monkeypatch):
    """H29: an edited message must not make an agent answer (and act) again."""
    from telegram import Update
    monkeypatch.setenv("TG_TOKEN_JAMES", "1:AAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    gw = TelegramGateway(cfg)
    gw.build()
    app = gw.apps["james"]
    msg = {"message_id": 7, "date": 0, "chat": {"id": 111, "type": "private"},
           "from": {"id": 111, "is_bot": False, "first_name": "P"}, "text": "create the footer task"}
    new = Update.de_json({"update_id": 1, "message": msg}, app.bot)
    edited = Update.de_json({"update_id": 2, "edited_message": {**msg, "edit_date": 1}}, app.bot)
    handlers = [h for h in app.handlers[0] if type(h).__name__ == "MessageHandler"]
    assert any(h.check_update(new) for h in handlers)
    assert not any(h.check_update(edited) for h in handlers)


async def test_rate_limit_is_waited_out_not_dropped(cfg):
    gw, rt, sent = setup(cfg, fails={"sofia": [RetryAfter(0)]})
    await rt.bus.send_owner("sofia", "Deployed.")
    assert sent == [("sofia", 111, "Deployed.")]


async def test_group_upgrade_is_followed(cfg, tmp_path):
    import yaml
    from .conftest import make_raw
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(make_raw(tmp_path)))
    cfg.path = path
    gw, rt, sent = setup(cfg, fails={"james": [ChatMigrated(-1009)]})
    await rt.bus.post_group("james", "Daily report")
    assert sent == [("james", -1009, "Daily report")] and cfg.group_chat_id == -1009
    assert yaml.safe_load(path.read_text())["telegram"]["group_chat_id"] == -1009


async def test_blocked_bot_is_reported(cfg):
    gw, rt, sent = setup(cfg, fails={"marcus": [Forbidden("bot was blocked by the user")]})
    await rt.bus.send_owner("marcus", "hello")
    assert "blocked" in rt.ws.state()["telegram_health"]["marcus"]


async def test_pause_addressed_to_one_bot_pauses_only_them(cfg):
    gw, rt, sent = setup(cfg)
    await gw._make_command_handler("james")(upd("/pause@marcus_cd_bot", chat_type="supergroup", chat_id=-100), None)
    assert rt.paused("marcus") and not rt.paused("sofia") and not rt.ws.state().get("paused_all")


async def test_swipe_reply_goes_to_that_person(cfg):
    gw, rt, sent = setup(cfg)
    rt.llm.push({"reply": "Yes, by Friday.", "actions": []})
    await gw._make_text_handler("james")(upd("is that still on?", chat_type="supergroup", chat_id=-100,
                                             reply_to_bot="marcus_cd_bot"), None)
    assert sent[-1][0] == "marcus" and sent[-1][2] == "Yes, by Friday."


async def test_console_decision_updates_the_telegram_card(cfg):
    gw, rt, sent = setup(cfg)
    ask = rt.asks.create(requester="marcus", summary="Pay ₹900/mo VPS", level="red")
    await rt.bus.send_owner("marcus", ask.summary, ask=ask)
    rt.llm.push({"reply": "Paying.", "actions": []})
    await rt.decide_ask(ask.id, "approved", by="Pankaj", via="console")
    edits = gw.apps["marcus"].bot.edits
    assert edits and "→ ASK-001 approved by Pankaj" in edits[0]["text"]


def test_markdown_becomes_telegram_html():
    assert to_html("**Done** — see `api.py` <b>\n```\nx < 1\n```") == \
        "<b>Done</b> — see <code>api.py</code> &lt;b&gt;\n<pre>x &lt; 1</pre>"


async def test_owner_detection_needs_the_code(tmp_path):
    from james_monitoring.telegram_setup import TelegramProbe
    from .fake_telegram import FakeTelegram
    tg = FakeTelegram({"111:AAA": "james_cd_bot"})
    try:
        tg.user_says("111:AAA", "hi, I'm not the owner", chat_id=999, user_id=999)
        tg.user_says("111:AAA", "482913", chat_id=111, user_id=111)
        async with TelegramProbe("111:AAA", tg.url, poll_seconds=0.05) as p:
            who = await p.wait_for_owner(5, code="482913")
        assert who and who.id == 111
    finally:
        tg.close()


async def test_project_on_telegram_without_its_own_group_uses_hq_with_a_label(cfg):
    gw, rt, sent = setup(cfg)
    await rt.bus.post_room("p-site", "sofia", "Pricing page shipped")
    assert sent[-1] == ("sofia", -100, "#site · Pricing page shipped")
    rt.llm.push({"reply": "On it.", "actions": []})
    await gw._make_text_handler("james")(upd("#site what's next?", chat_type="supergroup", chat_id=-100, msg_id=9), None)
    assert [m["text"] for m in rt.chat.since("p-site")][-2:] == ["what's next?", "On it."]
