"""Live Telegram checks used while setting up a team — nothing is assumed, everything is verified:
the token works, the owner is detected, the group is detected, each bot is in the group, each DM works."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from telegram import Bot
from telegram.error import Forbidden, TelegramError

POLL_SECONDS = 2.0


@dataclass
class BotInfo:
    id: int
    username: str
    reads_groups: bool      # privacy mode off → can read all group messages


@dataclass
class Person:
    id: int
    name: str
    username: str


@dataclass
class Group:
    id: int
    title: str


class TelegramProbe:
    def __init__(self, token: str, api_base: str = "", poll_seconds: float | None = None):
        kw = {"base_url": f"{api_base}/bot", "base_file_url": f"{api_base}/file/bot"} if api_base else {}
        self.bot = Bot(token, **kw)
        self.poll = POLL_SECONDS if poll_seconds is None else poll_seconds
        self._offset = 0
        self._buffer: list = []       # updates fetched but not yet used by a waiter

    async def __aenter__(self) -> "TelegramProbe":
        await self.bot.initialize()
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            await self.bot.shutdown()
        except TelegramError:
            pass

    async def info(self) -> BotInfo:
        me = await self.bot.get_me()
        return BotInfo(id=me.id, username=me.username or "", reads_groups=bool(me.can_read_all_group_messages))

    async def _updates(self):
        ups = await self.bot.get_updates(offset=self._offset, timeout=0,
                                         allowed_updates=["message", "my_chat_member"])
        if ups:
            self._offset = ups[-1].update_id + 1
            self._buffer.extend(ups)
        return list(self._buffer)

    def _take(self, u) -> None:
        self._buffer = [x for x in self._buffer if x.update_id > u.update_id]

    async def wait_for_owner(self, timeout: float = 300) -> Person | None:
        """The first private message the bot receives identifies the owner."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for u in await self._updates():
                m = u.message
                if m and m.chat.type == "private" and m.from_user and not m.from_user.is_bot:
                    self._take(u)
                    fu = m.from_user
                    return Person(fu.id, fu.full_name, fu.username or "")
            await asyncio.sleep(self.poll)
        return None

    async def wait_for_group(self, owner_id: int, timeout: float = 300) -> Group | None:
        """Detect the group the owner added this bot to (a message there, or the 'added' event)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for u in await self._updates():
                if u.message and u.message.chat.type in ("group", "supergroup"):
                    m = u.message
                    self._take(u)
                    if m.migrate_to_chat_id:             # group upgraded to supergroup → new id
                        chat = await self.bot.get_chat(m.migrate_to_chat_id)
                        return Group(chat.id, chat.title or "")
                    if m.from_user and m.from_user.id == owner_id:
                        return Group(m.chat.id, m.chat.title or "")
                cm = u.my_chat_member
                if (cm and cm.chat.type in ("group", "supergroup") and cm.from_user.id == owner_id
                        and cm.new_chat_member.status in ("member", "administrator")):
                    self._take(u)
                    return Group(cm.chat.id, cm.chat.title or "")
            await asyncio.sleep(self.poll)
        return None

    async def in_group(self, group_id: int) -> bool:
        try:
            me = await self.bot.get_me()
            member = await self.bot.get_chat_member(group_id, me.id)
            return member.status in ("member", "administrator", "creator")
        except TelegramError:
            return False

    async def can_dm(self, user_id: int, text: str) -> bool:
        """True once the user has pressed Start on this bot (Telegram forbids bots from messaging first)."""
        try:
            await self.bot.send_message(user_id, text)
            return True
        except Forbidden:
            return False
        except TelegramError:
            return False

    async def post(self, chat_id: int, text: str) -> None:
        try:
            await self.bot.send_message(chat_id, text)
        except TelegramError:
            pass

    async def wait_until(self, check, timeout: float = 300) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if await check():
                return True
            await asyncio.sleep(self.poll)
        return False
