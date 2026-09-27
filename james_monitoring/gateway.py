"""Telegram transport: one bot per team member, one HQ group, 1:1 DMs with the owner.

Telegram rule that shapes this design: bots never receive messages sent by other bots.
So agents talk to each other through the runtime (git + inbox), never through Telegram.
The monitor bot (the manager, e.g. James) is the only one that reads the group (privacy mode OFF for it);
the other bots only *post* there and read their own DMs.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters, Update
from telegram.constants import ChatAction, ChatType
from telegram.error import TelegramError
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler, ContextTypes,
                          MessageHandler, filters)

from .asks import Ask, AskError
from .commands import HELP, run_command
from .config import Config
from .router import group_targets, is_status_request
from .runtime import Event, Runtime
from .tasks import TaskError
from .util import chunk, in_quiet_hours

log = logging.getLogger("jm.telegram")


def _rp(message_id: int | None) -> ReplyParameters | None:
    return ReplyParameters(message_id=message_id, allow_sending_without_reply=True) if message_id else None

COMMANDS = HELP


class TelegramGateway:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.rt: Runtime | None = None
        self.apps: dict[str, Application] = {}
        self.usernames: dict[str, str] = {}
        self._stop: asyncio.Event | None = None
        self.chat = None                  # optional ChatStore: Telegram conversations also show in the web console

    def stop(self) -> None:
        if self._stop:
            self._stop.set()

    # -- Bus implementation -------------------------------------------------------
    def _bot(self, member_id: str):
        app = self.apps.get(member_id) or self.apps.get(self.cfg.monitor.id)
        if not app:
            raise RuntimeError("no Telegram bot available")
        return app.bot

    async def send_owner(self, from_id: str, text: str, ask: Ask | None = None, urgent: bool = False) -> None:
        if not self.cfg.owner_user_id:
            log.warning("owner.telegram_user_id not set; dropping DM: %s", text[:80])
            return
        bot = self._bot(from_id)
        silent = in_quiet_hours(self.cfg.timezone, self.cfg.quiet_hours) and not urgent
        body = ask.card(self.cfg.monitor.name) if ask else text
        markup = None
        if ask:
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"ask|{ask.id}|approved"),
                InlineKeyboardButton("❌ Reject", callback_data=f"ask|{ask.id}|rejected"),
            ]])
        parts = chunk(body)
        for i, part in enumerate(parts):
            try:
                await bot.send_message(self.cfg.owner_user_id, part, disable_notification=silent,
                                       reply_markup=markup if i == len(parts) - 1 else None)
            except TelegramError as e:
                log.error("DM to owner via %s failed: %s (has the owner pressed /start on this bot?)", from_id, e)

    async def post_group(self, from_id: str, text: str, reply_to: int | None = None) -> None:
        if not self.cfg.group_chat_id:
            log.warning("telegram.group_chat_id not set; dropping group post: %s", text[:80])
            return
        bot = self._bot(from_id)
        for part in chunk(text):
            try:
                await bot.send_message(self.cfg.group_chat_id, part, reply_parameters=_rp(reply_to))
                reply_to = None
            except TelegramError as e:
                log.error("group post via %s failed: %s (is the bot in the group?)", from_id, e)

    # -- helpers ------------------------------------------------------------------
    def _authorized(self, update: Update) -> bool:
        u = update.effective_user
        return bool(u and self.cfg.is_authorized(u.id))

    def _in_our_group(self, update: Update) -> bool:
        c = update.effective_chat
        return bool(c and (not self.cfg.group_chat_id or c.id == self.cfg.group_chat_id))

    async def _reply(self, update: Update, member_id: str, text: str) -> None:
        chat = update.effective_chat
        bot = self._bot(member_id)
        reply_to = update.effective_message.message_id if update.effective_message else None
        for part in chunk(text):
            await bot.send_message(chat.id, part, reply_parameters=_rp(reply_to))
            reply_to = None

    async def _typing(self, member_id: str, chat_id: int) -> None:
        try:
            await self._bot(member_id).send_chat_action(chat_id, ChatAction.TYPING)
        except TelegramError:
            pass

    def _mirror(self, room: str, who: str, text: str) -> None:
        if self.chat is not None:
            try:
                self.chat.append(room, who, text, via="telegram")
            except Exception:  # noqa: BLE001
                pass

    # -- handlers -----------------------------------------------------------------
    def _make_text_handler(self, member_id: str):
        is_monitor = member_id == self.cfg.monitor.id

        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            msg = update.effective_message
            if not msg or not msg.text or not self._authorized(update):
                return
            chat = update.effective_chat
            rt = self.rt
            if chat.type == ChatType.PRIVATE:
                await self._typing(member_id, chat.id)
                self._mirror(member_id, rt.owner_id, msg.text)
                reply = await rt.dispatch(member_id, Event("dm", msg.text, sender=rt.owner_id))
                await self._reply(update, member_id, reply)
                self._mirror(member_id, member_id, reply)
                return
            if not is_monitor or not self._in_our_group(update):
                return   # only the manager's bot reads the group, so each message is handled exactly once
            is_all, targets = group_targets(msg.text, self.cfg, self.usernames)
            if is_all and is_status_request(msg.text):
                for m in self.cfg.team:
                    st, line = rt.tasks.person_status(m.id)
                    await self.post_group(m.id, f"{st} — {line}", reply_to=msg.message_id)
                return

            self._mirror("team", rt.owner_id, msg.text)

            async def one(mid: str) -> None:
                await self._typing(mid, chat.id)
                reply = await rt.dispatch(mid, Event("group", msg.text, sender=rt.owner_id))
                await self.post_group(mid, reply, reply_to=msg.message_id)
                self._mirror("team", mid, reply)
            await asyncio.gather(*(one(t) for t in targets))
        return handler

    def _make_command_handler(self, member_id: str):
        is_monitor = member_id == self.cfg.monitor.id

        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            msg = update.effective_message
            chat = update.effective_chat
            if not msg or not msg.text:
                return
            if chat.type != ChatType.PRIVATE and not is_monitor:
                return   # in the group only the manager's bot answers commands
            cmd, _, args = msg.text.partition(" ")
            cmd = cmd.lstrip("/").split("@")[0].lower()
            if cmd == "whoami":
                await self._reply(update, member_id, f"Your Telegram user id: {update.effective_user.id}")
                return
            if cmd == "groupid":
                await self._reply(update, member_id, f"This chat id: {chat.id}")
                return
            if not self._authorized(update):
                return
            try:
                text = await run_command(self.rt, cmd, args.strip(), member_id, chat.type == ChatType.PRIVATE)
            except (ValueError, TaskError, AskError) as e:
                text = f"⚠️ {e}"
            if text:
                await self._reply(update, member_id, text)
        return handler

    async def _on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not q:
            return
        if not self._authorized(update):
            await q.answer("Not allowed", show_alert=True)
            return
        try:
            _, ask_id, decision = (q.data or "").split("|")
            result = await self.rt.decide_ask(ask_id, decision, by=self.cfg.owner_name)
            await q.answer(result[:190])
            await q.edit_message_text((q.message.text or "") + f"\n\n→ {result}")
        except (ValueError, AskError) as e:
            await q.answer(str(e)[:190], show_alert=True)

    # -- lifecycle ----------------------------------------------------------------
    def build(self) -> None:
        for m in self.cfg.team:
            token = m.bot_token
            if not token:
                log.warning("no bot token for %s (env %s) — %s will be offline", m.id, m.bot_token_env, m.name)
                continue
            builder = ApplicationBuilder().token(token).concurrent_updates(True)
            if self.cfg.telegram_api_base:      # self-hosted Bot API server (or a test server)
                builder = builder.base_url(f"{self.cfg.telegram_api_base}/bot").base_file_url(
                    f"{self.cfg.telegram_api_base}/file/bot")
            app = builder.build()
            app.add_handler(CallbackQueryHandler(self._on_callback, pattern=r"^ask\|"))
            app.add_handler(MessageHandler(filters.COMMAND, self._make_command_handler(m.id)))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._make_text_handler(m.id)))
            self.apps[m.id] = app
        if self.cfg.monitor.id not in self.apps:
            raise RuntimeError(f"The monitor ({self.cfg.monitor.name}) needs a bot token "
                               f"(env {self.cfg.monitor.bot_token_env}).")

    async def start(self, rt: Runtime) -> None:
        """Connect every bot and start polling. Scheduling lives in scheduler.py, not here."""
        self.rt = rt
        if not self.apps:
            self.build()
        for mid, app in self.apps.items():
            await app.initialize()
            me = await app.bot.get_me()
            self.usernames[mid] = me.username or mid
            try:
                await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
            except TelegramError:
                pass
            await app.start()
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            log.info("%s online as @%s", mid, me.username)

    async def shutdown(self) -> None:
        for app in self.apps.values():
            try:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()
            except Exception:  # noqa: BLE001
                pass

    async def run(self, rt: Runtime, scheduler=None) -> None:
        """Headless mode (no web console): bots + scheduler until Ctrl-C / SIGTERM."""
        await self.start(rt)
        if scheduler:
            scheduler.start()
        stop = self._stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # pragma: no cover (Windows)
                pass
        try:
            await stop.wait()
        finally:
            if scheduler:
                await scheduler.stop()
            await self.shutdown()
            await rt.drain()
