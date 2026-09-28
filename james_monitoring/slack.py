"""Slack transport: run the team from Slack, in sync with the console and Telegram.

One Slack app for the whole team (Socket Mode: no public URL, works behind NAT like Telegram polling).
Every teammate posts under their own name and icon (chat:write.customize).

    #jm-hq                     ↔ room team           (All hands)
    #jm-p-<project>            ↔ room p-<project>    (project room: team talk and hand-offs show here too)
    #jm-dm-<member> (private)  ↔ room <member>       (your 1:1 with that person)
    the app's DM               → the manager, or "@name …" for anyone

@name addresses someone, @here everyone in that room, and a thread reply goes to whoever wrote the message.
Commands: /jm status, /jm assign riya "…", /jm approve ASK-3 (or type them with "!": !status).
Approval cards come with Approve / Reject buttons.

`slack.channels` in config.yaml maps rooms to channel ids; the console's "Create channels" button fills it in.
Only the Slack members in `slack.owner_user_ids` can command the team.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import threading
from typing import TYPE_CHECKING

from .chat import BACKCHANNEL, PROJECT_ROOM, TEAM_ROOM
from .config import Config

if TYPE_CHECKING:  # pragma: no cover
    from .asks import Ask
    from .hub import Hub
    from .runtime import Runtime

log = logging.getLogger("jm.slack")

_USER = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")
_LINK = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>")
_AT = re.compile(r"^@(\w+)[\s,:]+", re.S)
MAX_BLOCK = 2900


class SlackError(Exception):
    pass


def _client(token: str):
    try:
        from slack_sdk import WebClient
    except ImportError as e:  # pragma: no cover
        raise SlackError("Slack support needs: pip install 'james-monitoring[slack]'") from e
    return WebClient(token=token)


def slack_text(text: str) -> str:
    """Our markdown → Slack mrkdwn, safely: &, <, > escaped (so text can never ping <!channel> or fake a link),
    **bold** → *bold*."""
    t = (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"\*\*([^*\n]+)\*\*", r"*\1*", t)


def channel_name(company: str, room: str) -> str:
    """Slack channel names: lowercase, no spaces, ≤ 80 chars."""
    def clean(s: str) -> str:
        return re.sub(r"[^a-z0-9_-]+", "-", s.lower()).strip("-")[:70] or "team"
    if room == TEAM_ROOM:
        return "jm-hq"
    if room == BACKCHANNEL:
        return "jm-backchannel"
    if room.startswith(PROJECT_ROOM):
        return f"jm-p-{clean(room[len(PROJECT_ROOM):])}"
    return f"jm-dm-{clean(room)}"


class SlackTransport:
    name = "slack"

    def __init__(self, cfg: Config, web=None):
        self.cfg = cfg
        self.web = web
        self.rt: Runtime | None = None
        self.hub: Hub | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.socket = None
        self.bot_user_id = ""
        self.team_name = ""
        self.error = ""
        self.last_unknown_user = ""                  # for "Detect me" in Settings
        self.detect_code = ""
        self._im: dict[str, str] = {}                 # owner user id → app DM channel
        self.cards: dict[str, tuple[str, str, str]] = {}  # ask id → (channel, ts, text) to update after a decision
        self.authors: dict[tuple[str, str], str] = {}     # (channel, ts) → member who posted it
        self._seen = threading.Event()

    # -- mapping ------------------------------------------------------------------------
    @property
    def channels(self) -> dict[str, str]:
        return self.cfg.slack.channels

    def room_of_channel(self, channel: str) -> str | None:
        for room, ch in self.channels.items():
            if ch == channel:
                return room
        return None

    def _name(self, who: str) -> str:
        if self.rt and who == self.rt.owner_id:
            return self.cfg.owner_name
        m = self.cfg.member(who)
        return m.name if m else who

    # -- Transport: something was said in a room → show it in Slack ---------------------------
    async def deliver(self, room: str, msg: dict, ask: "Ask | None" = None) -> None:
        if not self.web:
            return
        ch = self.channels.get(room)
        kind, who, text = msg.get("kind", "msg"), msg.get("who", ""), msg.get("text", "")
        if not ch:
            # Unmapped 1:1 rooms still reach the owner in the app's DM, so nothing that needs you gets lost.
            owner_echo = bool(self.rt and who == self.rt.owner_id)
            if not self.cfg.member(room) or not self.cfg.slack.owner_user_ids or owner_echo:
                return
            ch = await self._owner_im()
            if not ch:
                return
            text = f"*{self._name(room)}* · {text}" if who != room else text
        owner = self.rt.owner_id if self.rt else ""
        if who == owner:
            username, icon = f"{self.cfg.owner_name} (via {msg.get('via') or 'console'})", ":bust_in_silhouette:"
        else:
            m = self.cfg.member(who)
            username = m.name if m else (who or "team")
            icon = ":briefcase:" if m and m.monitor else ":robot_face:"
            if kind == "internal":
                text = f"_(teammates)_ {text}"
        text = slack_text(text)
        kw = {"channel": ch, "text": text[:39000], "username": username, "icon_emoji": icon}
        if ask is not None and kind == "ask":
            kw["blocks"] = [
                {"type": "section", "text": {"type": "mrkdwn", "text": text[:MAX_BLOCK]}},
                {"type": "actions", "block_id": f"ask:{ask.id}", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "✅ Approve"}, "style": "primary",
                     "action_id": "jm_approve", "value": ask.id},
                    {"type": "button", "text": {"type": "plain_text", "text": "❌ Reject"}, "style": "danger",
                     "action_id": "jm_reject", "value": ask.id}]}]
        try:
            r = await asyncio.to_thread(self.web.chat_postMessage, **kw)
            if ask is not None and kind == "ask":
                self.cards[ask.id] = (kw["channel"], (r or {}).get("ts", ""), text)
                if self.rt:
                    self.rt.ws.update_state(lambda s: s.setdefault("slack_cards", {}).__setitem__(
                        ask.id, [kw["channel"], (r or {}).get("ts", ""), text]))
            self._remember(kw["channel"], (r or {}).get("ts", ""), who)
        except Exception as e:  # noqa: BLE001
            self.error = str(e)[:200]
            log.error("slack: post to %s failed: %s", room, e)

    async def ask_decided(self, ask: "Ask", result: str) -> None:
        where = self.cards.pop(ask.id, None)
        if not where and self.rt:
            where = (self.rt.ws.state().get("slack_cards") or {}).get(ask.id)
        if self.rt:
            self.rt.ws.update_state(lambda s: (s.get("slack_cards") or {}).pop(ask.id, None))
        if not where or not where[1] or not self.web:
            return
        channel, ts, text = where
        try:
            await asyncio.to_thread(self.web.chat_update, channel=channel, ts=ts, text=f"{text}\n→ {result}",
                                    blocks=[{"type": "section", "text": {"type": "mrkdwn",
                                                                         "text": f"{text[:MAX_BLOCK - 200]}\n*→ {result}*"}}])
        except Exception as e:  # noqa: BLE001
            log.error("slack: couldn't update the card: %s", e)

    def _remember(self, channel: str, ts: str, who: str) -> None:
        """Which teammate wrote which message, so a thread reply goes to them."""
        if ts:
            self.authors[(channel, ts)] = who
            if len(self.authors) > 5000:
                for k in list(self.authors)[:1000]:
                    self.authors.pop(k, None)

    async def _owner_im(self) -> str:
        uid = self.cfg.slack.owner_user_ids[0]
        if uid not in self._im:
            try:
                r = await asyncio.to_thread(self.web.conversations_open, users=uid)
                self._im[uid] = r["channel"]["id"]
            except Exception as e:  # noqa: BLE001
                log.error("slack: can't open a DM with %s: %s", uid, e)
                return ""
        return self._im[uid]

    # -- inbound --------------------------------------------------------------------------------
    def clean(self, text: str) -> str:
        text = re.sub(r"<!(here|channel|everyone)(?:\|[^>]*)?>", r"@\1", text or "")      # @here = everyone in this room
        text = _USER.sub(lambda m: "" if m.group(1) == self.bot_user_id else f"@{m.group(1)}", text)
        text = _LINK.sub(lambda m: m.group(2) or m.group(1), text)
        return html.unescape(text).strip()

    def event_room(self, ev: dict) -> tuple[str | None, str]:
        """A Slack message event → (room, text) — None when it isn't ours to handle. Pure: easy to test."""
        if ev.get("type") != "message" or ev.get("bot_id") or ev.get("subtype") not in (None, "file_share", "thread_broadcast"):
            return None, ""
        user = ev.get("user", "")
        if user not in self.cfg.slack.owner_user_ids:
            if user and self.detect_code and self.detect_code in (ev.get("text") or ""):
                self.last_unknown_user = user          # only the person who sends the one-time code
                self._seen.set()
            return None, ""
        text = self.clean(ev.get("text", ""))
        if not text:
            return None, ""
        if re.match(r"^![a-zA-Z]", text):                  # "!status" = /status (but not "!!! server down")
            text = "/" + text[1:]
        if ev.get("channel_type") == "im":
            m = _AT.match(text)
            if m and self.cfg.member(m.group(1)):
                return self.cfg.member(m.group(1)).id, text[m.end():].strip()
            parent = ev.get("thread_ts")
            who = self.authors.get((ev.get("channel", ""), parent)) if parent and parent != ev.get("ts") else None
            if who and self.cfg.member(who):              # a thread reply in the app DM goes to that message's author
                return who, text
            return self.cfg.monitor.id, text
        room = self.room_of_channel(ev.get("channel", ""))
        if not room or room == BACKCHANNEL:
            return None, ""
        parent = ev.get("thread_ts")
        if parent and parent != ev.get("ts") and not re.search(r"(?<!\w)@\w", text):
            who = self.authors.get((ev.get("channel", ""), parent))     # replying in a thread talks to its author
            if who and self.cfg.member(who) and who != self.cfg.monitor.id:
                text = f"@{who} {text}"
        return room, text

    def _on_request(self, client, req) -> None:
        from slack_sdk.socket_mode.response import SocketModeResponse
        try:
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        except Exception:  # noqa: BLE001
            log.exception("slack ack failed")
        try:
            if req.type == "events_api":
                self.on_event((req.payload or {}).get("event") or {})
            elif req.type == "interactive":
                self.on_action(req.payload or {})
            elif req.type == "slash_commands":
                self.on_slash(req.payload or {})
        except Exception:  # noqa: BLE001
            log.exception("slack: event handling failed")

    def _watch(self, fut, what: str):
        """Futures from the Slack thread: errors are logged and shown, never lost silently."""
        def done(f):
            e = f.exception()
            if e:
                self.error = f"{what}: {e}"[:200]
                log.error("slack: %s failed: %s", what, e)
        fut.add_done_callback(done)
        return fut

    def on_event(self, ev: dict) -> None:
        room, text = self.event_room(ev)
        if room and self.loop and self.hub:
            self._watch(asyncio.run_coroutine_threadsafe(self.hub.inbound(room, text, via="slack"), self.loop),
                        f"message in {room}")

    def slash_room(self, payload: dict) -> tuple[str | None, str]:
        """/jm status · /jm assign riya "…" · /jm approve ASK-3 — in any of our channels or the app's DM."""
        if payload.get("user_id") not in self.cfg.slack.owner_user_ids:
            return None, ""
        text = (payload.get("text") or "").strip()
        if not text:
            text = "help"
        room = self.room_of_channel(payload.get("channel_id", "")) or self.cfg.monitor.id
        return (None, "") if room == BACKCHANNEL else (room, "/" + text.lstrip("/!"))

    def on_slash(self, payload: dict):
        room, text = self.slash_room(payload)
        if room and self.loop and self.hub:
            return self._watch(asyncio.run_coroutine_threadsafe(self.hub.inbound(room, text, via="slack"), self.loop),
                               "/jm command")
        return None

    def on_action(self, payload: dict):
        """Approve / Reject button pressed. Returns the future (tests wait on it)."""
        if payload.get("type") != "block_actions" or not payload.get("actions"):
            return None
        user = (payload.get("user") or {}).get("id", "")
        if user not in self.cfg.slack.owner_user_ids:
            return None
        act = payload["actions"][0]
        decision = {"jm_approve": "approved", "jm_reject": "rejected"}.get(act.get("action_id"))
        if not decision or not self.loop:
            return None
        return self._watch(asyncio.run_coroutine_threadsafe(self._decide(act.get("value", ""), decision, payload),
                                                            self.loop), "approval button")

    async def _decide(self, ask_id: str, decision: str, payload: dict) -> str:
        from .asks import AskError
        try:
            result = await self.rt.decide_ask(ask_id, decision, by=self.cfg.owner_name, via="slack")
            self.cards.pop(ask_id, None)
        except (AskError, ValueError) as e:
            result = f"⚠️ {e}"
        channel = (payload.get("channel") or {}).get("id") or (payload.get("container") or {}).get("channel_id")
        ts = (payload.get("message") or {}).get("ts") or (payload.get("container") or {}).get("message_ts")
        if channel and ts:
            old = ((payload.get("message") or {}).get("text") or "")[:MAX_BLOCK - 200]
            try:
                await asyncio.to_thread(self.web.chat_update, channel=channel, ts=ts, text=f"{old}\n→ {result}",
                                        blocks=[{"type": "section", "text": {"type": "mrkdwn",
                                                                             "text": f"{old}\n*→ {result}*"}}])
            except Exception as e:  # noqa: BLE001
                log.error("slack: couldn't update the card: %s", e)
        return result

    # -- lifecycle ---------------------------------------------------------------------------------
    def bind(self, rt: "Runtime", hub: "Hub") -> None:
        self.rt, self.hub = rt, hub
        hub.add(self)

    async def start(self, rt: "Runtime", hub: "Hub") -> None:
        self.bind(rt, hub)
        self.loop = asyncio.get_running_loop()
        if self.web is None:
            self.web = _client(self.cfg.slack.bot_token)
        auth = await asyncio.to_thread(self.web.auth_test)
        self.bot_user_id, self.team_name = auth.get("user_id", ""), auth.get("team", "")
        from slack_sdk.socket_mode import SocketModeClient
        self.socket = SocketModeClient(app_token=self.cfg.slack.app_token, web_client=self.web)
        self.socket.socket_mode_request_listeners.append(self._on_request)
        await asyncio.to_thread(self.socket.connect)
        log.info("slack online in %s as %s", self.team_name, self.bot_user_id)

    async def shutdown(self) -> None:
        if self.hub:
            self.hub.remove(self.name)
        if self.socket:
            try:
                await asyncio.to_thread(self.socket.close)
            except Exception:  # noqa: BLE001
                pass

    def wait_for_unknown_user(self, timeout: float = 90, code: str = "") -> str:
        """Settings → "Detect me": whoever sends the app the one-time code shown in the console."""
        self._seen.clear()
        self.last_unknown_user, self.detect_code = "", code
        try:
            self._seen.wait(timeout)
            return self.last_unknown_user
        finally:
            self.detect_code = ""


# -- setup helpers (called from the console, synchronous) ----------------------------------------
def check_tokens(bot_token: str, app_token: str) -> dict:
    if not bot_token.startswith("xoxb-"):
        return {"ok": False, "error": "The bot token starts with xoxb- (OAuth & Permissions → Bot User OAuth Token)."}
    if not app_token.startswith("xapp-"):
        return {"ok": False, "error": "The app token starts with xapp- (Basic Information → App-Level Tokens, "
                                      "scope connections:write)."}
    try:
        a = _client(bot_token).auth_test()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Slack didn't accept the bot token: {e}"}
    return {"ok": True, "team": a.get("team", ""), "bot": a.get("user", ""), "bot_user_id": a.get("user_id", "")}


def lookup_email(bot_token: str, email: str) -> str:
    """Your Slack member id from your email (needs users:read.email)."""
    try:
        r = _client(bot_token).users_lookupByEmail(email=email.strip())
    except Exception as e:  # noqa: BLE001
        raise SlackError(f"Slack couldn't find {email}: {e}") from None
    return r["user"]["id"]


def provision(web, cfg: Config, rooms: list[str]) -> dict[str, str]:
    """Create (or find) a channel for each room, invite the owner(s). Returns room → channel id."""
    existing: dict[str, str] = {}
    cursor = None
    while True:
        r = web.conversations_list(types="public_channel,private_channel", limit=1000, cursor=cursor,
                                   exclude_archived=True)
        for c in r.get("channels", []):
            existing[c["name"]] = c["id"]
        cursor = (r.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    out = {}
    for room in rooms:
        name = channel_name(cfg.company, room)
        private = not (room == TEAM_ROOM or room.startswith(PROJECT_ROOM))
        cid = existing.get(name)
        if not cid:
            c = web.conversations_create(name=name, is_private=private)
            cid = c["channel"]["id"]
        else:
            try:
                web.conversations_join(channel=cid)
            except Exception:  # noqa: BLE001 - private channels can't be joined, only invited to
                pass
        for uid in cfg.slack.owner_user_ids:
            try:
                web.conversations_invite(channel=cid, users=uid)
            except Exception:  # noqa: BLE001 - already_in_channel etc.
                pass
        out[room] = cid
    return out
