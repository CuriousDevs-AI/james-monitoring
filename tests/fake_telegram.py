"""A tiny fake Telegram Bot API server, so the real python-telegram-bot polling code can be tested end to end."""
from __future__ import annotations

import itertools
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs


class FakeTelegram:
    def __init__(self, bots: dict[str, str]):
        """bots: token -> username"""
        self.bots = bots
        self.updates: dict[str, list[dict]] = {t: [] for t in bots}
        self.sent: list[dict] = []           # every outgoing call we care about
        self._ids = itertools.count(1)
        self._msg_ids = itertools.count(1000)
        self._lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()

    # -- what a user does ---------------------------------------------------------
    def _base_msg(self, chat_id, chat_type, text, user_id):
        msg = {"message_id": next(self._msg_ids), "date": int(time.time()),
               "chat": {"id": chat_id, "type": chat_type, **({"title": "HQ"} if chat_type != "private" else {})},
               "from": {"id": user_id, "is_bot": False, "first_name": "Pankaj"}, "text": text}
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        return msg

    def user_says(self, token, text, chat_id=111, chat_type="private", user_id=111):
        with self._lock:
            self.updates[token].append({"update_id": next(self._ids),
                                        "message": self._base_msg(chat_id, chat_type, text, user_id)})

    def user_taps(self, token, data, message_text="card", user_id=111):
        with self._lock:
            self.updates[token].append({"update_id": next(self._ids), "callback_query": {
                "id": str(next(self._ids)), "chat_instance": "x", "data": data,
                "from": {"id": user_id, "is_bot": False, "first_name": "Pankaj"},
                "message": {**self._base_msg(user_id, "private", message_text, user_id),
                            "from": {"id": 1, "is_bot": True, "first_name": "bot"}}}})

    def wait_for(self, pred, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                hits = [s for s in self.sent if pred(s)]
            if hits:
                return hits
            time.sleep(0.05)
        raise AssertionError(f"timed out; sent so far: {self.sent}")

    # -- HTTP -----------------------------------------------------------------------
    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode() if n else ""
                ctype = self.headers.get("Content-Type", "")
                if "json" in ctype and body:
                    params = json.loads(body)
                else:
                    params = {k: v[0] for k, v in parse_qs(body).items()}
                _, bot, method = self.path.split("/", 2)
                token = bot[3:]
                result = fake.handle(token, method, params)
                data = json.dumps({"ok": True, "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass   # client went away during shutdown

            do_GET = do_POST
        return H

    def handle(self, token, method, p):
        uname = self.bots.get(token, "unknown")
        if method == "getMe":
            return {"id": abs(hash(token)) % 10**9, "is_bot": True, "first_name": uname, "username": uname,
                    "can_join_groups": True, "can_read_all_group_messages": uname.startswith("james"),
                    "supports_inline_queries": False}
        if method == "getUpdates":
            offset = int(p.get("offset") or 0)
            with self._lock:
                ups = [u for u in self.updates[token] if u["update_id"] >= offset]
                self.updates[token] = ups
            if not ups:
                time.sleep(0.05)
            return ups
        if method in ("sendMessage", "editMessageText"):
            chat_id = int(p.get("chat_id") or 0)
            markup = p.get("reply_markup")
            if isinstance(markup, str):
                markup = json.loads(markup)
            with self._lock:
                self.sent.append({"bot": uname, "method": method, "chat_id": chat_id, "text": p.get("text", ""),
                                  "markup": markup})
            return {"message_id": next(self._msg_ids), "date": int(time.time()),
                    "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
                    "from": {"id": 1, "is_bot": True, "first_name": uname}, "text": p.get("text", "")}
        if method == "answerCallbackQuery":
            with self._lock:
                self.sent.append({"bot": uname, "method": method, "text": p.get("text", "")})
            return True
        return True    # deleteWebhook, setMyCommands, sendChatAction, ...
