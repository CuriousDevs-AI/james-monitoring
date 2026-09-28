"""The hub: every conversation goes through here, whichever channel it comes from.

    console ─┐                         ┌─ console (the chat store is the record)
    Telegram ├─► hub.inbound(room) ──► ├─ Telegram
    Slack  ──┘      │ route + agents   └─ Slack
                    ▼
               runtime.dispatch

One message lands in one room (see chat.py) and is mirrored to every other connected channel, so the
founder can start a thread on the laptop, continue it on the phone and finish it in Slack. Agents read the
same rooms, so everyone on a project knows what was said there — whoever said it, wherever.

The hub is also the runtime's Bus: what agents send (DMs to the owner, All hands posts, approval cards,
teammate hand-offs) is written to the room and delivered to every channel.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Protocol

from .chat import BACKCHANNEL, PROJECT_ROOM, TEAM_ROOM, ChatStore
from .router import is_status_request, room_targets

if TYPE_CHECKING:  # pragma: no cover
    from .asks import Ask
    from .runtime import Runtime

log = logging.getLogger("jm.hub")


def is_command(text: str) -> bool:
    """/status, /assign … — but not "/Users/me/app.log is broken" or a path someone pastes."""
    from .commands import HELP
    m = re.match(r"^/([a-zA-Z]+)(?:@\w+)?(?:\s|$)", text or "")
    return bool(m) and (m.group(1).lower() in {c for c, _ in HELP} | {"stop", "start", "rework", "changes"})


class Transport(Protocol):
    """A channel the team is reachable on (Telegram, Slack). The console needs none: it reads the chat store."""
    name: str

    async def deliver(self, room: str, msg: dict, ask: "Ask | None" = None) -> None: ...


class Hub:
    def __init__(self, chat: ChatStore | None = None):
        self.chat = chat
        self.rt: Runtime | None = None
        self.transports: list = []
        self.pending: dict[str, int] = {}          # room → agents still thinking
        self.successor: Hub | None = None          # after a reload: late messages go through the new hub

    def attach(self, rt: "Runtime") -> "Hub":
        self.rt = rt
        if self.chat is None:
            self.chat = rt.chat
        rt.chat = self.chat
        return self

    def add(self, transport) -> None:
        self.transports = [t for t in self.transports if t.name != transport.name] + [transport]

    def remove(self, name: str) -> None:
        self.transports = [t for t in self.transports if t.name != name]

    @property
    def usernames(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for t in self.transports:
            out.update(getattr(t, "usernames", {}) or {})
        return out

    # -- rooms ------------------------------------------------------------------------------
    def rooms(self) -> list[str]:
        cfg = self.rt.cfg
        return ([PROJECT_ROOM + p for p in cfg.projects] + [TEAM_ROOM] + [m.id for m in cfg.team] + [BACKCHANNEL])

    def check_room(self, room: str) -> None:
        cfg = self.rt.cfg
        if room.startswith(PROJECT_ROOM):
            if room[len(PROJECT_ROOM):] not in cfg.projects:
                raise ValueError(f"no such project {room[len(PROJECT_ROOM):]}")
        elif room == BACKCHANNEL:
            raise ValueError("The backchannel is where teammates talk to each other — it's read-only.")
        elif room != TEAM_ROOM and not cfg.member(room):
            raise ValueError(f"no such room {room}")

    def owner_room_for(self, member_id: str) -> str:
        """Where a DM from an agent to the owner lands: that agent's 1:1 room."""
        m = self.rt.cfg.member(member_id) if member_id else None
        return m.id if m else self.rt.cfg.monitor.id

    # -- output: write to the room, deliver everywhere -----------------------------------------
    async def post(self, room: str, who: str, text: str, kind: str = "msg", ask: "Ask | None" = None,
                   via: str = "") -> dict:
        if self.successor is not None:
            return await self.successor.post(room, who, text, kind=kind, ask=ask, via=via)
        msg = self.chat.append(room, who, text, kind=kind, via=via, ask_id=ask.id if ask else None)
        await self.fanout(room, msg, ask=ask, skip=via)
        return msg

    def delivers_to(self, room: str, transport_name: str) -> bool:
        """Each room lives on exactly one outside channel (Telegram *or* Slack), so a conversation is never split.
        Other transports (e.g. `jm chat` printing) always see everything."""
        if transport_name not in ("telegram", "slack"):
            return True
        return self.home(room) == transport_name

    def home(self, room: str) -> str:
        connected = tuple(t.name for t in self.transports if t.name in ("telegram", "slack"))
        return self.rt.cfg.channel_for(room, connected)

    async def fanout(self, room: str, msg: dict, ask: "Ask | None" = None, skip: str = "") -> None:
        for t in list(self.transports):
            if (skip and t.name == skip) or not self.delivers_to(room, t.name):
                continue
            try:
                await t.deliver(room, msg, ask=ask)
            except Exception:  # noqa: BLE001 - one broken channel never blocks the others
                log.exception("%s could not deliver to %s", t.name, room)

    async def ask_decided(self, ask, result: str, via: str = "") -> None:
        """Update the approval card in every channel that showed it (except where it was decided)."""
        if self.successor is not None:
            return await self.successor.ask_decided(ask, result, via)
        for t in list(self.transports):
            fn = getattr(t, "ask_decided", None)
            if fn and t.name != via and self.delivers_to(ask.requester, t.name):
                try:
                    await fn(ask, result)
                except Exception:  # noqa: BLE001
                    log.exception("%s could not update the card for %s", t.name, ask.id)

    # Bus (what the runtime calls)
    async def send_owner(self, from_id: str, text: str, ask: "Ask | None" = None, urgent: bool = False) -> None:
        room = self.owner_room_for(from_id)
        if ask is not None:
            await self.post(room, from_id, ask.card(self.rt.cfg.monitor.name), kind="ask", ask=ask)
        else:
            await self.post(room, from_id, text, kind="notice" if urgent else "msg")

    async def post_group(self, from_id: str, text: str, reply_to: int | None = None) -> None:
        await self.post(TEAM_ROOM, from_id, text)

    async def post_room(self, room: str, from_id: str, text: str, kind: str = "msg", via: str = "") -> None:
        await self.post(room, from_id, text, kind=kind, via=via)

    # -- input: the owner said something somewhere ----------------------------------------------
    def receive(self, room: str, text: str, via: str = "console") -> dict | None:
        """Record the owner's message now (so every screen shows it at once). Raises ValueError for a bad room."""
        text = (text or "").strip()
        if not text:
            raise ValueError("empty message")
        self.check_room(room)
        return self.chat.append(room, self.rt.owner_id, text, via=via)

    async def handle(self, room: str, msg: dict, via: str = "console") -> None:
        """Mirror the owner's message to the other channels, then get the right people to answer."""
        if self.rt.cfg.mirror_owner:
            await self.fanout(room, msg, skip=via)
        await self.route(room, msg["text"])

    async def inbound(self, room: str, text: str, via: str) -> None:
        """For transports: receive + handle in one go. A room that lives on the other channel says where to go."""
        if self.successor is not None:                    # arrived during a reload: the new hub handles it
            return await self.successor.inbound(room, text, via)
        if via in ("telegram", "slack") and not self.delivers_to(room, via):
            home = self.home(room) or "the console"
            name = self.rt.room_title(room)
            for t in self.transports:
                if t.name == via:
                    await t.deliver(room, {"who": self.rt.cfg.monitor.id, "kind": "notice", "force": True,
                                           "text": f"ℹ️ {name} lives on {home.title() if home != 'the console' else home} "
                                                   f"— please write there, so the whole conversation stays in one place."})
            return
        msg = self.receive(room, text, via=via)
        await self.handle(room, msg, via=via)

    async def route(self, room: str, text: str) -> None:
        from .runtime import Event
        rt, cfg = self.rt, self.rt.cfg
        if is_command(text):
            return await self._command(room, text)
        m = re.match(r"^\s*(approve|approved|reject|rejected)\s+(ASK-\d+)\b[\s:,.-]*(.*)$", text, re.I | re.S)
        if m:                                   # "approve ASK-3 go ahead" typed anywhere = the button
            cmd = "approve" if m.group(1).lower().startswith("approve") else "reject"
            return await self._command(room, f"/{cmd} {m.group(2).upper()} {m.group(3).strip()}".strip())
        pid = room[len(PROJECT_ROOM):] if room.startswith(PROJECT_ROOM) else ""
        is_all, targets = room_targets(room, text, cfg, self.usernames)
        if not targets:
            await self.post(room, cfg.monitor.id, "Nobody is on this project yet — add people on the project page.",
                            kind="notice")
            return
        if is_all and is_status_request(text):
            for mid in targets:          # instant, from the board — no model call
                if pid:
                    mine = [t for t in rt.tasks.for_owner(mid) if t.project == pid]
                    line = "; ".join(t.line(cfg.timezone) for t in mine[:3]) or "nothing open on this project"
                else:
                    st, line = rt.tasks.person_status(mid)
                    line = f"{st} — {line}"
                await self.post(room, mid, line)
            return
        source = "dm" if room not in (TEAM_ROOM,) and not pid else "group"
        # One after the other, like people in a meeting: each reads what the previous ones just said
        # (the room's recent messages are in their context), instead of parallel monologues.
        for i, mid in enumerate(targets):                  # the task feedback in it is recorded once, not per person
            await self._answer(room, mid, Event(source, text, sender=rt.owner_id, project=pid, room=room,
                                                meta={"primary": i == 0}))

    async def _answer(self, room: str, mid: str, ev) -> None:
        self.pending[room] = self.pending.get(room, 0) + 1
        try:
            for t in self.transports:
                typing = getattr(t, "typing", None)
                if typing and self.delivers_to(room, t.name):
                    try:
                        await typing(room, mid)
                    except Exception:  # noqa: BLE001
                        pass
            reply = await self.rt.dispatch(mid, ev)
            await self.post(room, mid, reply)
        except Exception as e:  # noqa: BLE001 - show the problem in the room instead of losing the message
            log.exception("reply from %s failed", mid)
            await self.post(room, mid, f"⚠️ {e}", kind="notice")
        finally:
            self.pending[room] -= 1

    async def _command(self, room: str, text: str) -> None:
        from .asks import AskError
        from .commands import run_command
        from .tasks import TaskError
        cmd, _, args = text[1:].partition(" ")
        shared = room == TEAM_ROOM or room.startswith(PROJECT_ROOM)
        speaker = self.rt.cfg.monitor.id if shared else room
        try:
            out = await run_command(self.rt, cmd.lower().split("@")[0], args.strip(), speaker, private=not shared)
        except (ValueError, TaskError, AskError) as e:
            out = f"⚠️ {e}"
        if out:
            await self.post(room, speaker, out, kind="system")
