"""Telegram transport: one bot per team member, one HQ group, 1:1 DMs with the owner.

Telegram rule that shapes this design: bots never receive messages sent by other bots.
So agents talk to each other through the runtime (git + inbox), never through Telegram.
The monitor bot (James) is the only one that reads the group (privacy mode OFF for it);
the other bots only *post* there and read their own DMs.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from datetime import time as dtime
from zoneinfo import ZoneInfo

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters, Update
from telegram.constants import ChatAction, ChatType
from telegram.error import TelegramError
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from .asks import Ask, AskError
from .commands import HELP, run_command
from .config import Config
from .router import group_targets, is_status_request
from .runtime import Event, Runtime
from .tasks import TaskError
from .util import chunk, in_quiet_hours, parse_hhmm

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
        body = ask.card() if ask else text
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
                reply = await rt.dispatch(member_id, Event("dm", msg.text, sender=rt.owner_id))
                await self._reply(update, member_id, reply)
                return
            if not is_monitor or not self._in_our_group(update):
                return   # only James reads the group, so each message is handled exactly once
            is_all, targets = group_targets(msg.text, self.cfg, self.usernames)
            if is_all and is_status_request(msg.text):
                for m in self.cfg.team:
                    st, line = rt.tasks.person_status(m.id)
                    await self.post_group(m.id, f"{st} — {line}", reply_to=msg.message_id)
                return

            async def one(mid: str) -> None:
                await self._typing(mid, chat.id)
                reply = await rt.dispatch(mid, Event("group", msg.text, sender=rt.owner_id))
                await self.post_group(mid, reply, reply_to=msg.message_id)
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
                return   # in the group only James answers commands
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

    # -- scheduled jobs (on James's bot) -------------------------------------------
    async def _job_report(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await self.rt.run_daily_report()
        except Exception:
            log.exception("daily report failed")

    async def _job_checks(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await self.rt.run_checks()
        except Exception:
            log.exception("checks failed")

    # -- lifecycle ----------------------------------------------------------------
    def build(self) -> None:
        for m in self.cfg.team:
            token = m.bot_token
            if not token:
                log.warning("no bot token for %s (env %s) — %s will be offline", m.id, m.bot_token_env, m.name)
                continue
            app = ApplicationBuilder().token(token).concurrent_updates(True).build()
            app.add_handler(CallbackQueryHandler(self._on_callback, pattern=r"^ask\|"))
            app.add_handler(MessageHandler(filters.COMMAND, self._make_command_handler(m.id)))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._make_text_handler(m.id)))
            self.apps[m.id] = app
        if self.cfg.monitor.id not in self.apps:
            raise RuntimeError(f"The monitor ({self.cfg.monitor.name}) needs a bot token "
                               f"(env {self.cfg.monitor.bot_token_env}).")

    async def run(self, rt: Runtime) -> None:
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
        jq = self.apps[self.cfg.monitor.id].job_queue
        tz = ZoneInfo(self.cfg.timezone)
        rep = parse_hhmm(self.cfg.daily_report)
        jq.run_daily(self._job_report, time=dtime(rep.hour, rep.minute, tzinfo=tz), name="daily-report")
        jq.run_repeating(self._job_checks, interval=self.cfg.check_every_minutes * 60, first=60, name="checks")
        log.info("james-monitoring running: %d bot(s), daily report %s %s", len(self.apps),
                 self.cfg.daily_report, self.cfg.timezone)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # pragma: no cover (Windows)
                pass
        try:
            await stop.wait()
        finally:
            for app in self.apps.values():
                try:
                    await app.updater.stop()
                    await app.stop()
                    await app.shutdown()
                except Exception:
                    pass
            await rt.drain()
