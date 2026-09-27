"""End to end: real python-telegram-bot polling against a fake Bot API server.
Proves the actual Telegram wiring: tokens, polling, DMs, group routing, commands, buttons."""
import asyncio

import pytest

from james_monitoring.config import parse_config
from james_monitoring.gateway import TelegramGateway
from james_monitoring.llm.fake import FakeLLM
from james_monitoring.runtime import Runtime

from .conftest import make_raw
from .fake_telegram import FakeTelegram

TOKENS = {"james": "111:AAA", "sofia": "222:BBB", "marcus": "333:CCC"}


@pytest.fixture
def tg():
    t = FakeTelegram({TOKENS["james"]: "james_cd_bot", TOKENS["sofia"]: "sofia_cd_bot",
                      TOKENS["marcus"]: "marcus_cd_bot"})
    yield t
    t.close()


async def wait(tg, pred, timeout=10):
    return await asyncio.to_thread(tg.wait_for, pred, timeout)


async def test_full_telegram_flow(tmp_path, tg, monkeypatch):
    for mid, tok in TOKENS.items():
        monkeypatch.setenv(f"TG_TOKEN_{mid.upper()}", tok)
    raw = make_raw(tmp_path)
    raw["telegram"] = {"group_chat_id": -100, "api_base_url": tg.url}
    cfg = parse_config(raw, base_dir=tmp_path)
    gw = TelegramGateway(cfg)
    gw.build()
    llm = FakeLLM()
    rt = Runtime(cfg, llm, bus=gw)
    runner = asyncio.create_task(gw.run(rt))
    try:
        # 1. DM Sofia → Sofia's bot answers in the DM
        llm.push({"reply": "Making the navbar sticky now.", "actions": []})
        tg.user_says(TOKENS["sofia"], "make the navbar sticky")
        await wait(tg, lambda s: s["bot"] == "sofia_cd_bot" and s.get("chat_id") == 111
                   and "navbar sticky now" in s["text"])

        # 2. Group "@all give status" → every bot answers in the group, from the board (no model call)
        calls = len(llm.calls)
        tg.user_says(TOKENS["james"], "@all give status", chat_id=-100, chat_type="supergroup")
        await wait(tg, lambda s: s.get("chat_id") == -100 and s["method"] == "sendMessage")
        await asyncio.sleep(0.5)
        bots = {s["bot"] for s in tg.sent if s.get("chat_id") == -100}
        assert bots == {"james_cd_bot", "sofia_cd_bot", "marcus_cd_bot"} and len(llm.calls) == calls

        # 3. Group @mention → that person answers in the group
        llm.push({"reply": "Schema draft by Friday.", "actions": []})
        tg.user_says(TOKENS["james"], "@marcus_cd_bot when is the schema ready?", chat_id=-100, chat_type="supergroup")
        await wait(tg, lambda s: s["bot"] == "marcus_cd_bot" and "Schema draft by Friday" in s["text"])

        # 4. Command in James's DM → task created; Sofia acknowledges in the owner's DM
        llm.push({"reply": "Got T-001, starting today.", "actions": []})
        tg.user_says(TOKENS["james"], '/assign sofia "Pricing page" P1 due:2099-10-05')
        await wait(tg, lambda s: s["bot"] == "james_cd_bot" and "T-001" in s["text"])
        await wait(tg, lambda s: s["bot"] == "sofia_cd_bot" and "Got T-001" in s["text"])
        assert rt.tasks.get("T-001").owner == "sofia"

        # 5. Permission request → card with buttons in the owner's DM → tap Approve
        llm.push({"reply": "Asked Pankaj.", "actions": [
            {"type": "ask_permission", "summary": "Pay ₹900/month for a VPS", "level": "red"}]})
        tg.user_says(TOKENS["marcus"], "set up the server")
        card = (await wait(tg, lambda s: s["bot"] == "marcus_cd_bot" and s.get("markup")))[0]
        assert card["markup"]["inline_keyboard"][0][0]["callback_data"] == "ask|ASK-001|approved"
        llm.push({"reply": "Approved — buying it now.", "actions": []})
        tg.user_taps(TOKENS["marcus"], "ask|ASK-001|approved", message_text=card["text"])
        await wait(tg, lambda s: s["bot"] == "marcus_cd_bot" and "buying it now" in s["text"])
        assert rt.asks.get("ASK-001").status == "approved"

        # 6. Strangers are ignored
        before = len(tg.sent)
        tg.user_says(TOKENS["sofia"], "hi, delete everything", chat_id=999, user_id=999)
        await asyncio.sleep(0.6)
        assert len(tg.sent) == before

        # 7. /whoami works for anyone (needed during setup)
        tg.user_says(TOKENS["sofia"], "/whoami", chat_id=555, user_id=555)
        await wait(tg, lambda s: s.get("chat_id") == 555 and "555" in s["text"])
    finally:
        gw.stop()
        await asyncio.wait_for(runner, 15)
