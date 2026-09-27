"""Gateway logic with fake Telegram objects (no network)."""
from types import SimpleNamespace

from james_monitoring.gateway import TelegramGateway
from james_monitoring.llm.fake import FakeLLM
from james_monitoring.runtime import Runtime


class FakeBot:
    def __init__(self, name, sent):
        self.name, self.sent = name, sent

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((self.name, chat_id, text, kw.get("reply_markup")))

    async def send_chat_action(self, *a, **k):
        pass


def setup(cfg):
    sent = []
    gw = TelegramGateway(cfg)
    gw.apps = {m.id: SimpleNamespace(bot=FakeBot(m.id, sent)) for m in cfg.team}
    gw.usernames = {m.id: f"{m.id}_cd_bot" for m in cfg.team}
    rt = Runtime(cfg, FakeLLM(), bus=gw)
    gw.rt = rt
    return gw, rt, sent


def update(text, chat_type="private", chat_id=111, user_id=111, msg_id=5):
    return SimpleNamespace(
        effective_message=SimpleNamespace(text=text, message_id=msg_id),
        effective_chat=SimpleNamespace(type=chat_type, id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
    )


async def test_dm_goes_to_that_member_and_reply_comes_from_their_bot(cfg):
    gw, rt, sent = setup(cfg)
    rt.llm.push({"reply": "Changing the navbar now.", "actions": []})
    await gw._make_text_handler("sofia")(update("make the navbar sticky"), None)
    assert sent == [("sofia", 111, "Changing the navbar now.", None)]


async def test_strangers_are_ignored(cfg):
    gw, rt, sent = setup(cfg)
    await gw._make_text_handler("sofia")(update("hi", user_id=999, chat_id=999), None)
    assert sent == [] and rt.llm.calls == []


async def test_group_all_status_is_instant_and_from_each_bot(cfg):
    gw, rt, sent = setup(cfg)
    await gw._make_text_handler("james")(update("@all give status", chat_type="supergroup", chat_id=-100), None)
    assert {s[0] for s in sent} == {"james", "sofia", "marcus"}
    assert all(s[1] == -100 for s in sent) and rt.llm.calls == []   # from the board, no model call


async def test_group_mention_routes_to_person_and_only_james_listens(cfg):
    gw, rt, sent = setup(cfg)
    await gw._make_text_handler("sofia")(update("@sofia_cd_bot hi", chat_type="supergroup", chat_id=-100), None)
    assert sent == []                                            # sofia's bot ignores group traffic
    rt.llm.push({"reply": "On it.", "actions": []})
    await gw._make_text_handler("james")(update("@sofia_cd_bot fix footer", chat_type="supergroup", chat_id=-100), None)
    assert sent == [("sofia", -100, "On it.", None)]


async def test_other_groups_are_ignored(cfg):
    gw, rt, sent = setup(cfg)
    await gw._make_text_handler("james")(update("@all status", chat_type="group", chat_id=-555), None)
    assert sent == []


async def test_commands_and_ask_buttons(cfg):
    gw, rt, sent = setup(cfg)
    await gw._make_command_handler("james")(update('/assign sofia "Pricing page" P1 due:2099-10-05'), None)
    assert "T-001" in sent[0][2]
    await rt.drain()
    # sofia acknowledged the assignment in the owner's DM, from her own bot
    assert any(s[0] == "sofia" and s[1] == 111 for s in sent)

    ask = rt.asks.create(requester="marcus", summary="Pay ₹900/mo VPS", level="red")
    sent.clear()
    await gw.send_owner("marcus", ask.summary, ask=ask)
    name, chat, text, markup = sent[0]
    assert name == "marcus" and "ASK-001" in text
    assert markup.inline_keyboard[0][0].callback_data == "ask|ASK-001|approved"

    answers = []

    async def answer(t="", show_alert=False):
        answers.append(t)

    async def edit(t):
        answers.append(("edit", t))
    q = SimpleNamespace(data="ask|ASK-001|approved", answer=answer, edit_message_text=edit,
                        message=SimpleNamespace(text=text))
    upd = SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=111))
    rt.llm.push({"reply": "Paying now.", "actions": []})
    await gw._on_callback(upd, None)
    await rt.drain()
    assert rt.asks.get("ASK-001").status == "approved"
    assert any(s[0] == "marcus" and s[2] == "Paying now." for s in sent)
