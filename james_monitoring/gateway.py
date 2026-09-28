"""Telegram transport: one bot per team member, one HQ group, 1:1 DMs with the owner.

Telegram rule that shapes this design: bots never receive messages sent by other bots.
So agents talk to each other through the runtime (git + inbox), never through Telegram.
The monitor bot (the manager, e.g. James) is the only one that reads groups (privacy mode OFF for it);
the other bots only *post* there and read their own DMs.

Everything goes through the hub (hub.py): a Telegram message lands in the same room as the console and Slack,
and whatever happens in those rooms elsewhere is delivered here.

    DM with <member>'s bot      ↔ room <member>   (a member without a bot: via the manager's bot, "@name …")
    the HQ group                ↔ room team
    a project's own group       ↔ room p-<project> (projects.<id>.telegram_chat_id)
"""
from __future__ import annotations

import asyncio
import logging
import re
import signal

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters, Update
from telegram.constants import ChatAction, ChatType
from telegram.error import BadRequest, ChatMigrated, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler, ContextTypes,
                          MessageHandler, filters)

from .asks import Ask, AskError
from .chat import PROJECT_ROOM, TEAM_ROOM
from .commands import HELP
from .config import Config
from .hub import Hub
from .runtime import Runtime
from .util import chunk, in_quiet_hours

log = logging.getLogger("jm.telegram")

_AT = re.compile(r"^@(\w+)[\s,:]+", re.S)


def _rp(message_id: int | None) -> ReplyParameters | None:
    return ReplyParameters(message_id=message_id, allow_sending_without_reply=True) if message_id else None

COMMANDS = HELP


class TelegramGateway:
    name = "telegram"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.rt: Runtime | None = None
        self.hub: Hub | None = None
        self.apps: dict[str, Application] = {}
        self.usernames: dict[str, str] = {}
        self._stop: asyncio.Event | None = None
        self.health: dict[str, str] = {}
        self.cards: dict[str, tuple[str, int, int]] = {}      # ask id → (member bot, chat, message) to edit later
        self.extra_chats: dict[str, int] = {}                  # room → private chat of an extra user who wrote there
        self._seen: dict[tuple[int, int], float] = {}         # (chat, message) already handled — no double answers

    def stop(self) -> None:
        if self._stop:
            self._stop.set()

    def bind(self, rt: Runtime, hub: Hub) -> None:
        self.rt, self.hub = rt, hub
        hub.add(self)

    # -- helpers ------------------------------------------------------------------
    def _bot(self, member_id: str):
        app = self.apps.get(member_id) or self.apps.get(self.cfg.monitor.id)
        if not app:
            raise RuntimeError("no Telegram bot available")
        return app.bot

    def _name(self, who: str) -> str:
        if self.rt and who == self.rt.owner_id:
            return self.cfg.owner_name
        m = self.cfg.member(who)
        return m.name if m else who

    def _room_chat(self, room: str) -> tuple[int, str | None]:
        """(chat id, member id whose bot speaks in the owner's DM) for a room; chat id 0 = not on Telegram."""
        if room == TEAM_ROOM:
            return self.cfg.group_chat_id, None
        if room.startswith(PROJECT_ROOM):
            p = self.cfg.projects.get(room[len(PROJECT_ROOM):])
            # its own group if it has one, otherwise the HQ group with a "#project ·" label
            return ((p.telegram_chat_id or self.cfg.group_chat_id) if p else 0), None
        m = self.cfg.member(room)
        return (self.cfg.owner_user_id if m else 0), (m.id if m else None)

    def _chat_room(self, chat_id: int) -> str | None:
        if self.cfg.group_chat_id and chat_id == self.cfg.group_chat_id:
            return TEAM_ROOM
        for pid, p in self.cfg.projects.items():
            if p.telegram_chat_id and chat_id == p.telegram_chat_id:
                return PROJECT_ROOM + pid
        return None

    async def _send(self, bot, chat_id: int, text: str, reply_markup=None, silent: bool = False,
                    reply_to: int | None = None):
        """Send (in parts if long), as formatted HTML with a plain-text fallback. Rate limits (429) are waited
        out and retried; a group that became a supergroup is followed to its new id. Returns the last message."""
        parts = [x for x in chunk(text or "") if x.strip()]
        sent = None
        for i, part in enumerate(parts):
            kw = dict(disable_notification=silent, reply_parameters=_rp(reply_to),
                      reply_markup=reply_markup if i == len(parts) - 1 else None)
            for attempt in range(5):
                try:
                    try:
                        sent = await bot.send_message(chat_id, to_html(part), parse_mode="HTML", **kw)
                    except BadRequest as e:
                        if "parse" not in str(e).lower() and "entit" not in str(e).lower():
                            raise
                        sent = await bot.send_message(chat_id, part, **kw)          # formatting problem → plain
                    break
                except RetryAfter as e:
                    if attempt == 4:
                        raise                                  # still rate-limited: fail loudly, never silently
                    wait = e.retry_after.total_seconds() if hasattr(e.retry_after, "total_seconds") else float(e.retry_after)
                    await asyncio.sleep(min(wait, 60) + 0.5)
                except ChatMigrated as e:
                    chat_id = self._migrated(chat_id, e.new_chat_id)
                except (BadRequest, Forbidden, TimedOut):
                    raise      # permanent — or it may have been delivered already: retrying would send it twice
                except NetworkError:
                    if attempt >= 2:
                        raise
                    await asyncio.sleep(1 + attempt * 2)
            reply_to = None
        return sent

    def _migrated(self, old: int, new: int) -> int:
        """A group upgraded to a supergroup gets a new id: follow it, in memory and in config.yaml."""
        log.warning("telegram: chat %s moved to %s — updating config", old, new)
        if self.cfg.group_chat_id == old:
            self.cfg.group_chat_id = new
        for p in self.cfg.projects.values():
            if p.telegram_chat_id == old:
                p.telegram_chat_id = new
        if self.cfg.path:
            from .fileio import update_config

            def fn(raw):
                if int((raw.get("telegram") or {}).get("group_chat_id") or 0) == old:
                    raw.setdefault("telegram", {})["group_chat_id"] = new
                for pr in (raw.get("projects") or {}).values():
                    if isinstance(pr, dict) and int(pr.get("telegram_chat_id") or 0) == old:
                        pr["telegram_chat_id"] = new
            try:
                update_config(self.cfg.path, fn)
            except Exception:  # noqa: BLE001
                log.exception("could not save the new chat id")
        return new

    def _health(self, member_id: str, error: str = "") -> None:
        """Remember when a bot is blocked, kicked or broken, so People and the checks can show it."""
        self.health[member_id] = error
        if self.rt:
            self.rt.ws.update_state(lambda s: s.setdefault("telegram_health", {}).__setitem__(member_id, error))

    # -- Transport: something was said in a room → show it on Telegram ------------------------
    async def deliver(self, room: str, msg: dict, ask: Ask | None = None) -> None:
        kind, who, text = msg.get("kind", "msg"), msg.get("who", ""), msg.get("text", "")
        if kind == "internal":
            return                                    # teammates' hand-offs stay in the console / Slack
        chat_id, dm_member = self._room_chat(room)
        if not chat_id:
            return
        owner = self.rt.owner_id if self.rt else ""
        if who == owner:
            text = f"🗨 {self.cfg.owner_name} (via {msg.get('via') or 'console'}): {text}"
        if room.startswith(PROJECT_ROOM):
            p = self.cfg.projects.get(room[len(PROJECT_ROOM):])
            if p and not p.telegram_chat_id:
                text = f"#{p.id} · {text}"
        if dm_member:
            speaker = dm_member                          # the DM with <member> is that member's bot
            if dm_member not in self.apps:               # no own bot → through the manager's, clearly labelled
                text = f"[{self._name(dm_member)}] {text}"
        else:
            speaker = who if who in self.apps else self.cfg.monitor.id
            if speaker != who and who != owner:
                text = f"[{self._name(who)}] {text}"
        markup = None
        if ask is not None and kind == "ask":
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"ask|{ask.id}|approved"),
                InlineKeyboardButton("❌ Reject", callback_data=f"ask|{ask.id}|rejected"),
            ]])
        try:
            silent = in_quiet_hours(self.cfg.timezone, self.cfg.quiet_hours) and kind not in ("ask", "notice")
        except Exception:  # noqa: BLE001 - a malformed quiet_hours must never drop messages
            silent = False
        try:
            sent = await self._send(self._bot(speaker), chat_id, text, reply_markup=markup, silent=silent)
            if ask is not None and sent is not None:
                self.cards[ask.id] = (speaker, chat_id, sent.message_id)
                if self.rt:                                   # kept across reloads, so the card can still be updated
                    self.rt.ws.update_state(lambda s: s.setdefault("tg_cards", {}).__setitem__(
                        ask.id, [speaker, chat_id, sent.message_id]))
            extra = self.extra_chats.get(room)
            if dm_member and extra and extra != chat_id and who != owner:
                await self._send(self._bot(speaker), extra, text, silent=silent)   # the teammate-user who asked
            if self.health.get(speaker):
                self._health(speaker, "")
        except Forbidden as e:
            self._health(speaker, f"blocked or removed: {e}")
            log.error("telegram: %s can't reach %s: %s (bot blocked, or not in the chat?)", speaker, room, e)
        except TelegramError as e:
            log.error("telegram: %s → %s failed: %s (bot in the chat? owner pressed Start?)", speaker, room, e)

    async def ask_decided(self, ask: Ask, result: str) -> None:
        """Decided somewhere else (console, Slack, a typed "approve ASK-3"): update the card here too."""
        where = self.cards.pop(ask.id, None)
        if not where and self.rt:
            where = (self.rt.ws.state().get("tg_cards") or {}).get(ask.id)
        if self.rt:
            self.rt.ws.update_state(lambda s: (s.get("tg_cards") or {}).pop(ask.id, None))
        if not where:
            return
        speaker, chat_id, message_id = where
        try:
            await self._bot(speaker).edit_message_text(chat_id=chat_id, message_id=message_id,
                                                       text=ask.card(self.cfg.monitor.name) + f"\n\n→ {result}")
        except TelegramError:
            pass

    async def typing(self, room: str, member_id: str) -> None:
        chat_id, dm_member = self._room_chat(room)
        if not chat_id:
            return
        try:
            await self._bot(dm_member or member_id).send_chat_action(chat_id, ChatAction.TYPING)
        except (TelegramError, RuntimeError):
            pass

    # -- handlers -----------------------------------------------------------------
    def _authorized(self, update: Update) -> bool:
        u = update.effective_user
        return bool(u and self.cfg.is_authorized(u.id))

    def _inbound_room(self, member_id: str, update: Update, text: str) -> tuple[str | None, str]:
        """Which room a Telegram message belongs to (None = not ours), and the text to record."""
        chat = update.effective_chat
        if chat.type == ChatType.PRIVATE:
            m = _AT.match(text)                          # "@riya …" in a DM → Riya, if Riya has no bot of their own
            if m:
                target = self.cfg.member(m.group(1))
                if target and target.id != member_id and target.id not in self.apps:
                    return target.id, text[m.end():].strip()
            return member_id, text
        if member_id != self.cfg.monitor.id:
            return None, text                             # only the manager's bot reads groups: handled once
        room = self._chat_room(chat.id)
        m = re.match(r"^#([\w-]+)[\s:·-]+(.*)$", text, re.S)
        if room == TEAM_ROOM and m:                       # "#site …" in the HQ group → the site project's room
            pid = next((k for k, v in self.cfg.projects.items()
                        if m.group(1).lower() in (k.lower(), v.name.lower().replace(" ", "-"))), None)
            if pid and not self.cfg.projects[pid].telegram_chat_id:
                return PROJECT_ROOM + pid, m.group(2).strip()
        return room, text

    def _first_time(self, update: Update, member: str = "") -> bool:
        """Each Telegram message is handled once (edits and redeliveries don't make agents act twice)."""
        import time
        msg, chat = update.effective_message, update.effective_chat
        if not msg or not chat:
            return True
        key = (member, chat.id, msg.message_id)          # message ids are per bot in private chats
        now_ = time.monotonic()
        if len(self._seen) > 2000:
            self._seen = {k: v for k, v in self._seen.items() if now_ - v < 3600}
        if key in self._seen:
            return False
        self._seen[key] = now_
        return True

    def _reply_target(self, update: Update, text: str) -> str:
        """Replying (swipe-reply) to Marcus's message in a group talks to Marcus."""
        msg = update.effective_message
        r = getattr(msg, "reply_to_message", None)
        u = getattr(r, "from_user", None) if r else None
        if not u or not getattr(u, "is_bot", False) or re.search(r"(?<!\w)@\w", text):
            return text
        by_username = {v.lower(): k for k, v in self.usernames.items()}
        mid = by_username.get((u.username or "").lower())
        return f"@{mid} {text}" if mid and mid != self.cfg.monitor.id else text

    def _make_text_handler(self, member_id: str):
        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            msg = update.effective_message
            if not msg or not msg.text or not msg.text.strip() or not self._authorized(update):
                return
            room, text = self._inbound_room(member_id, update, msg.text)
            if not room or not text or not self._first_time(update, member_id):   # only the handling bot records it
                return
            u = update.effective_user
            if update.effective_chat.type == ChatType.PRIVATE and u and u.id != self.cfg.owner_user_id:
                self.extra_chats[room] = update.effective_chat.id   # replies reach this extra user too
            if update.effective_chat.type != ChatType.PRIVATE:
                text = self._reply_target(update, text)
            r = getattr(msg, "reply_to_message", None)          # a swipe-reply keeps what it answers
            quote = ""
            if r is not None and isinstance(getattr(r, "text", None), str) and r.text:
                who = getattr(getattr(r, "from_user", None), "first_name", "") or ""
                quote = (f"{who}: " if isinstance(who, str) and who else "") + " ".join(r.text.split())[:120]
            await self.hub.inbound(room, text, via="telegram", reply_quote=quote)
        return handler

    def _make_other_handler(self, member_id: str):
        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            chat = update.effective_chat
            if not chat or chat.type != ChatType.PRIVATE or not self._authorized(update) \
                    or not self._first_time(update, member_id):
                return
            await self._send(self._bot(member_id), chat.id, "I can only read text for now — send it as a message "
                                                            "(or paste a link to the file).")
        return handler

    async def _on_migrate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg and msg.migrate_to_chat_id:
            self._migrated(msg.chat.id, msg.migrate_to_chat_id)

    def _make_command_handler(self, member_id: str):
        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            msg = update.effective_message
            chat = update.effective_chat
            if not msg or not msg.text:
                return
            cmd, _, args = msg.text.partition(" ")
            cmd, _, addressed = cmd.lstrip("/").partition("@")
            cmd = cmd.lower()
            if chat.type != ChatType.PRIVATE and member_id != self.cfg.monitor.id:
                return   # in groups only the manager's bot answers commands
            by_username = {v.lower(): k for k, v in self.usernames.items()}
            target = by_username.get(addressed.lower()) if addressed else None
            if addressed and not target:
                return   # "/stop@some_other_bot": a command for someone else's bot, not for the team
            if target and target != self.cfg.monitor.id and not args.strip() and \
                    cmd in ("pause", "stop", "resume", "start", "log"):
                args = target        # /pause@marcus_bot in the group pauses Marcus, not everyone
            if cmd == "whoami":
                await self._send(self._bot(member_id), chat.id, f"Your Telegram user id: {update.effective_user.id}")
                return
            if cmd == "groupid":
                await self._send(self._bot(member_id), chat.id, f"This chat id: {chat.id}")
                return
            if not self._authorized(update):
                return
            room, _ = self._inbound_room(member_id, update, msg.text)
            if not room or not self._first_time(update, member_id):
                return
            await self.hub.inbound(room, f"/{cmd} {args.strip()}".strip(), via="telegram")
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
        except ValueError:
            return
        try:
            await q.answer("Working on it…")          # answer at once: Telegram gives callbacks ~15 seconds
        except TelegramError:
            pass
        try:
            result = await self.rt.decide_ask(ask_id, decision, by=self.cfg.owner_name, via="telegram")
            self.cards.pop(ask_id, None)
        except (ValueError, AskError) as e:
            result = f"⚠️ {e}"
        try:
            await q.edit_message_text((getattr(q.message, "text", None) or "") + f"\n\n→ {result}")
        except (TelegramError, AttributeError):
            pass                                          # a card older than 48h can't be edited — the decision stands

    # -- lifecycle ----------------------------------------------------------------
    def build(self) -> None:
        for m in self.cfg.team:
            token = m.bot_token
            if not token:
                log.warning("no bot token for %s (env %s) — %s is reachable through %s's bot",
                            m.id, m.bot_token_env, m.name, self.cfg.monitor.name)
                continue
            builder = ApplicationBuilder().token(token).concurrent_updates(True)
            if self.cfg.telegram_api_base:      # self-hosted Bot API server (or a test server)
                builder = builder.base_url(f"{self.cfg.telegram_api_base}/bot").base_file_url(
                    f"{self.cfg.telegram_api_base}/file/bot")
            app = builder.build()
            app.add_handler(CallbackQueryHandler(self._on_callback, pattern=r"^ask\|"))
            new = filters.UpdateType.MESSAGE                     # new messages only — edits never re-trigger work
            app.add_handler(MessageHandler(new & filters.StatusUpdate.MIGRATE, self._on_migrate))
            app.add_handler(MessageHandler(new & filters.COMMAND, self._make_command_handler(m.id)))
            app.add_handler(MessageHandler(new & filters.TEXT & ~filters.COMMAND, self._make_text_handler(m.id)))
            app.add_handler(MessageHandler(new & ~filters.TEXT & ~filters.StatusUpdate.ALL, self._make_other_handler(m.id)))
            self.apps[m.id] = app
        if self.cfg.monitor.id not in self.apps:
            raise RuntimeError(f"The monitor ({self.cfg.monitor.name}) needs a bot token "
                               f"(env {self.cfg.monitor.bot_token_env}).")

    async def start(self, rt: Runtime, hub: Hub | None = None) -> None:
        """Connect every bot and start polling. Scheduling lives in scheduler.py, not here."""
        self.bind(rt, hub or self.hub or _hub_of(rt))
        if not self.apps:
            self.build()
        started: list[str] = []
        for mid, app in list(self.apps.items()):
            try:
                await app.initialize()
                me = await app.bot.get_me()
                self.usernames[mid] = me.username or mid
                try:
                    await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
                except TelegramError:
                    pass
                await app.start()
                await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
                started.append(mid)
                self._health(mid, "")
                if mid == self.cfg.monitor.id and not me.can_read_all_group_messages:
                    self._health(mid, "privacy mode is ON — it can't read the group (@BotFather → /setprivacy → Disable)")
                log.info("%s online as @%s", mid, me.username)
            except Exception as e:  # noqa: BLE001
                log.error("telegram: %s's bot failed to start: %s", mid, e)
                self._health(mid, f"failed to start: {e}")
                await self._stop_app(app)
                self.apps.pop(mid, None)
                if mid == self.cfg.monitor.id:
                    # Without the manager's bot nothing works: stop the ones already polling (no stray copies → no 409s).
                    for other in started:
                        await self._stop_app(self.apps.pop(other))
                    raise

    @staticmethod
    async def _stop_app(app) -> None:
        for step in (lambda: app.updater.stop() if app.updater and app.updater.running else None,
                     lambda: app.stop() if app.running else None, app.shutdown):
            try:
                r = step()
                if r is not None:
                    await r
            except Exception:  # noqa: BLE001
                pass

    async def shutdown(self) -> None:
        if self.hub:
            self.hub.remove(self.name)
        for app in self.apps.values():
            await self._stop_app(app)

    async def run(self, rt: Runtime, scheduler=None, hub: Hub | None = None) -> None:
        """Headless mode (no web console): bots + scheduler until Ctrl-C / SIGTERM."""
        await self.start(rt, hub)
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


def to_html(text: str) -> str:
    """The team's light markdown → Telegram HTML: **bold**, `code`, ```blocks```. Everything else is escaped."""
    import html as _h
    out, parts = [], re.split(r"```(?:\w+)?\n?(.*?)```", text, flags=re.S)
    for i, part in enumerate(parts):
        if i % 2:
            out.append(f"<pre>{_h.escape(part.rstrip())}</pre>")
            continue
        t = _h.escape(part)
        t = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
        out.append(t)
    return "".join(out)


def _hub_of(rt: Runtime) -> Hub:
    """The runtime's hub, creating one if the runtime still has a plain bus."""
    if isinstance(rt.bus, Hub):
        return rt.bus
    hub = Hub().attach(rt)
    rt.bus = hub
    return hub
