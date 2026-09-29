"""A public link to the console through a Cloudflare Tunnel — `jm run` stays on this machine (127.0.0.1); cloudflared
connects out to Cloudflare and gives the console an https URL you can open anywhere.

Two ways:
- Quick tunnel (no account): `cloudflared tunnel --url http://127.0.0.1:<port>` → https://<random>.trycloudflare.com,
  a new URL each time it starts.
- Your own domain (a named tunnel you created in Cloudflare): put its token in CLOUDFLARE_TUNNEL_TOKEN and its URL in
  config `public.url` → `cloudflared tunnel run --token …`, the same URL every time.

The console key (and sign-in links) still protect every request; failed keys are rate-limited per visitor.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time

QUICK_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
MIN_KEY = 16                       # a public console needs a key nobody can guess


class PublicLink:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.url = ""
        self.kind = ""             # quick | named
        self.error = ""
        self.lines: list[str] = []
        self._lock = threading.Lock()

    @property
    def on(self) -> bool:
        return bool(self.proc and self.proc.poll() is None and self.url)

    def status(self) -> dict:
        running = bool(self.proc and self.proc.poll() is None)
        return {"on": self.on, "starting": running and not self.url, "url": self.url if running else "",
                "kind": self.kind, "error": self.error, "installed": bool(binary())}

    def start(self, port: int, key: str, named_url: str = "", wait: float = 40) -> dict:
        """Start the tunnel and wait (up to `wait` seconds) for its URL."""
        with self._lock:
            if self.proc and self.proc.poll() is None:
                return self.status()
            self.error, self.url, self.lines = "", "", []
            exe = binary()
            if not exe:
                self.error = ("cloudflared isn't installed — macOS: brew install cloudflared · Linux/Windows: "
                              "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/")
                return self.status()
            if len(key or "") < MIN_KEY:
                self.error = (f"The console key is too short to go public (at least {MIN_KEY} characters) — set a long "
                              f"JM_CONSOLE_KEY, or leave it unset so a random one is made.")
                return self.status()
            token = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN", "")
            if token and named_url:
                self.kind = "named"
                cmd = [exe, "tunnel", "--no-autoupdate", "run", "--token", token]
            else:
                self.kind = "quick"
                cmd = [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"]
            env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "LANG", "USER", "SYSTEMROOT")}
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, env=env, start_new_session=True)
            threading.Thread(target=self._read, args=(self.proc, named_url if self.kind == "named" else ""),
                             daemon=True, name="jm-tunnel").start()
        end = time.time() + wait
        while time.time() < end and not self.url and self.proc and self.proc.poll() is None:
            time.sleep(0.2)
        if not self.url:
            if self.proc and self.proc.poll() is not None:
                tail = " ".join(self.lines[-3:])[-300:]
                self.error = f"cloudflared stopped: {tail or 'no output'}"
            elif not self.error:
                self.error = "cloudflared started but hasn't given a URL yet — check again in a moment."
        return self.status()

    def _read(self, proc: subprocess.Popen, named_url: str) -> None:
        for line in proc.stdout or []:
            line = line.strip()
            self.lines = (self.lines + [line])[-50:]
            if not self.url:
                m = QUICK_URL.search(line)
                if m:
                    self.url = m.group(0)
                elif named_url and re.search(r"Registered tunnel connection|Connection .* registered", line):
                    self.url = named_url.rstrip("/")

    def stop(self) -> dict:
        with self._lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc, self.url, self.error = None, "", ""
        return self.status()


def binary() -> str:
    return os.environ.get("JM_CLOUDFLARED_BIN") or shutil.which("cloudflared") or ""


class FailedKeys:
    """Someone guessing keys (many *different* wrong keys from one visitor) is locked out for a while.

    Only different keys count: a browser tab left open from an earlier run keeps polling with its old key, and
    that must never lock anyone out (it did: the console then refused even the right link). Callers only use this
    for visitors from outside — through the tunnel or the network — never for this machine."""

    def __init__(self, limit: int = 20, window: float = 600):
        self.limit, self.window = limit, window
        self.hits: dict[str, dict[str, float]] = {}      # visitor → {hash of a wrong key: when last seen}
        self._lock = threading.Lock()

    def blocked(self, who: str) -> bool:
        now = time.time()
        with self._lock:
            keys = {k: t for k, t in self.hits.get(who, {}).items() if now - t < self.window}
            self.hits[who] = keys
            return len(keys) >= self.limit

    def fail(self, who: str, key: str = "") -> None:
        import hashlib
        with self._lock:
            self.hits.setdefault(who, {})[hashlib.sha256(key.encode()).hexdigest()[:16]] = time.time()
            if len(self.hits) > 5000:                     # never grows without bound
                for k in list(self.hits)[:1000]:
                    self.hits.pop(k, None)
