"""Connections: is every model the team uses installed, logged in and answering? Fix it from the console.

check()  — fast, no model call: CLI installed? logged in? API key present?
test()   — one tiny model call ("pong").
login()  — starts `claude auth login` / `codex login` on this machine; the CLI opens the browser to sign in.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .config import CLI_PROVIDERS, LLMConfig

LOGIN = {"claude-code": ["auth", "login", "--claudeai"], "codex-cli": ["login", "--device-auth"]}   # (for jm connection)
LABEL = {"claude-code": "Claude subscription (claude CLI)", "codex-cli": "ChatGPT / Codex subscription (codex CLI)",
         "opencode": "OpenCode (any model, incl. free)",
         "anthropic": "Anthropic API", "openai": "OpenAI-compatible API", "fake": "Fake (tests)"}
INSTALL = {"claude-code": "npm install -g @anthropic-ai/claude-code", "codex-cli": "npm install -g @openai/codex",
           "opencode": "npm install -g opencode-ai"}


def _kind(provider: str) -> str:
    p = (provider or "").lower()
    if p in ("claude-code", "claude_code", "claude-cli", "subscription"):
        return "claude-code"
    if p in ("codex-cli", "codex_cli", "chatgpt"):
        return "codex-cli"
    if p in ("opencode", "open-code"):
        return "opencode"
    if p in ("anthropic", "claude"):
        return "anthropic"
    if p == "ollama":
        return "ollama"
    if p in ("openai", "codex", "openrouter", "openai-compatible"):
        return "openai"
    return p


def _bin(kind: str) -> str:
    if kind == "claude-code":
        return os.environ.get("JM_CLAUDE_BIN") or shutil.which("claude") or ""
    if kind == "codex-cli":
        return os.environ.get("JM_CODEX_BIN") or shutil.which("codex") or ""
    if kind == "opencode":
        return os.environ.get("JM_OPENCODE_BIN") or shutil.which("opencode") or ""
    return ""


def _run(cmd: list[str], timeout: float = 20) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as e:
        return 127, str(e)


def check(conf: LLMConfig) -> dict:
    if _kind(conf.provider) == "ollama" or (conf.provider == "ollama"):
        running, models = ollama_models(conf.base_url or "http://localhost:11434")
        ok = running and (not conf.model or conf.model in models)
        return {"provider": "ollama", "label": "Ollama (local)", "model": conf.model or "default", "ok": ok,
                "installed": running, "logged_in": running, "can_login": False, "key_env": "", "login_target": "",
                "detail": "running" if running else "not running",
                "fix": "" if ok else ("Start Ollama (ollama serve)" if not running else f"ollama pull {conf.model}")}
    return _check(conf)


def _check(conf: LLMConfig) -> dict:
    """Fast readiness check for one model setup. `ok` = ready to use; `fix` = what to do if not."""
    kind = _kind(conf.provider)
    out = {"provider": kind, "label": LABEL.get(kind, conf.provider), "model": conf.model or "default",
           "ok": False, "installed": True, "logged_in": None, "detail": "", "fix": "",
           "can_login": kind in ("claude-code", "codex-cli", "opencode"), "key_env": conf.api_key_env,
           "login_target": (conf.model or "").split("/")[0] if kind == "opencode" else ""}
    if kind == "fake":
        return {**out, "ok": True, "logged_in": True, "detail": "no model (test mode)"}
    if kind == "opencode":
        b = _bin(kind)
        if not b:
            return {**out, "installed": False, "can_login": False, "detail": "CLI not installed",
                    "fix": f"Install it on this machine: {INSTALL[kind]}"}
        prov = (conf.model or "").split("/")[0]
        out["can_login"] = bool(prov) and prov != "opencode"
        if not prov:
            return {**out, "can_login": False, "detail": "no model chosen",
                    "fix": "Pick a model like opencode/big-pickle (free) or zhipuai/glm-4.6 — see the list."}
        if prov == "opencode":
            if not conf.allow_free:
                return {**out, "can_login": False, "detail": "free model — not allowed yet",
                        "fix": "Free models run OpenCode's own agent (isolated). Allow them in the person's profile "
                               "or AI model settings, or choose a paid model."}
            return {**out, "ok": True, "logged_in": True, "can_login": False,
                    "detail": "free OpenCode model — no login (runs isolated)"}
        creds = opencode_credentials()
        logged = prov.lower() in creds
        return {**out, "ok": logged, "logged_in": logged,
                "detail": f"{prov} credential found in OpenCode" if logged else f"no {prov} login in OpenCode",
                "fix": "" if logged else f"Click Log in — sign in to {prov} (paste your key or code when asked)."}
    if kind in ("claude-code", "codex-cli"):
        b = _bin(kind)
        if not b:
            return {**out, "installed": False, "can_login": False, "detail": "CLI not installed",
                    "fix": f"Install it on this machine: {INSTALL[kind]}"}
        if kind == "claude-code":
            rc, text = _run([b, "auth", "status", "--json"])
            try:
                data = json.loads(text[text.find("{"):text.rfind("}") + 1])
                logged, how = bool(data.get("loggedIn")), data.get("authMethod", "")
            except (ValueError, TypeError):
                logged, how = False, ""
            detail = f"logged in ({how})" if logged else "not logged in"
        else:
            rc, text = _run([b, "login", "status"])
            logged = rc == 0 and "logged in" in text.lower() and "not logged in" not in text.lower()
            detail = text.strip().splitlines()[0][:120] if logged and text.strip() else "not logged in"
        return {**out, "ok": logged, "logged_in": logged, "detail": detail,
                "fix": "" if logged else "Click Log in — sign in with your subscription (paste the code if asked)."}
    # API providers
    if not conf.model:
        return {**out, "detail": "no model id set", "fix": "Set a model id (e.g. from your provider's model list)."}
    local = bool(re.match(r"https?://(localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0)(:|/|$)", conf.base_url or ""))
    if kind == "openai" and local and not conf.api_key:
        return {**out, "ok": True, "logged_in": True, "detail": f"local endpoint {conf.base_url}"}
    if not conf.api_key_env:
        return {**out, "logged_in": False, "detail": "no API key variable set",
                "fix": "Set the API key in AI model settings (e.g. OPENROUTER_API_KEY for OpenRouter)."}
    if not conf.api_key:
        return {**out, "logged_in": False, "detail": f"{conf.api_key_env or 'API key'} is not set",
                "fix": "Paste the API key in AI model settings and save."}
    return {**out, "ok": True, "logged_in": True, "detail": f"key set ({conf.api_key_env})"}


def test(conf: LLMConfig) -> dict:
    """One tiny real call. The truth when check() looks fine but the model still fails."""
    from .llm import LLMError, make_llm
    if _kind(conf.provider) == "fake":
        return {"ok": True, "seconds": 0.0, "tokens": 0}
    t0 = time.time()
    try:
        r = make_llm(conf).complete('Reply with exactly: {"reply": "pong", "actions": []}',
                                    [{"role": "user", "content": "ping"}])
    except LLMError as e:
        return {"ok": False, "error": " ".join(str(e).split())[:300]}
    ok = "pong" in r.text.lower()
    return {"ok": ok, "seconds": round(time.time() - t0, 1), "tokens": r.total_tokens,
            **({} if ok else {"error": f"odd reply: {r.text[:120]}"})}


def opencode_credentials() -> list[str]:
    """Provider ids OpenCode holds a login for — exact ids from its auth.json (e.g. ['anthropic', 'zai'])."""
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    path = Path(os.environ.get("JM_OPENCODE_AUTH") or os.path.join(data_home, "opencode", "auth.json"))
    try:
        return sorted(k.lower() for k in json.loads(path.read_text()))
    except (OSError, ValueError, AttributeError):
        return []


def opencode_models() -> list[str]:
    b = _bin("opencode")
    if not b:
        return []
    rc, text = _run([b, "models"], timeout=60)
    return [x.strip() for x in text.splitlines() if re.fullmatch(r"[\w.-]+/[\w.:/-]+", x.strip())]


# -- login: the CLI's own sign-in, driven from the console -------------------------------------------------------
# Each login runs in a pseudo-terminal, so the CLI behaves exactly as in a terminal: it prints its sign-in link or
# one-time code, and waits for a pasted code ("Paste code here if prompted >") or an API key. The console shows the
# live screen and sends what you type — nothing you paste is stored or echoed back.
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[=>78]")
KEYS = {"enter": "\r", "up": "\x1b[A", "down": "\x1b[B", "space": " ", "tab": "\t", "esc": "\x1b"}


def render_terminal(raw: str, width: int = 120) -> str:
    """A tiny terminal: applies carriage returns, cursor moves and erases, so a menu that redraws itself shows
    once — the way it looks in a real terminal — instead of every redraw stacked up."""
    lines: list[list[str]] = [[]]
    row = col = 0
    i, n = 0, len(raw)
    tok = re.compile(r"\x1b\[([0-9;?]*)([ -/]*)([@-~])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[=>78DEM]")
    while i < n:
        m = tok.match(raw, i) if raw[i] == "\x1b" else None
        if m:
            i = m.end()
            if m.group(3) is None:
                continue
            args = [int(x) for x in (m.group(1) or "").lstrip("?").split(";") if x.isdigit()]
            k, a = m.group(3), (args[0] if args else 1)
            if k == "A":
                row = max(0, row - a)
            elif k == "B":
                row += a
            elif k == "C":
                col += a
            elif k == "D":
                col = max(0, col - a)
            elif k == "G":
                col = max(0, a - 1)
            elif k in "Hf":
                row, col = max(0, (args[0] if args else 1) - 1), max(0, (args[1] if len(args) > 1 else 1) - 1)
            elif k == "J":
                while len(lines) <= row:
                    lines.append([])
                lines[row] = lines[row][:col]
                if (args[0] if args else 0) == 0:
                    del lines[row + 1:]
            elif k == "K":
                while len(lines) <= row:
                    lines.append([])
                mode = args[0] if args else 0
                lines[row] = lines[row][:col] if mode == 0 else [" "] * col + lines[row][col + 1:] if mode == 1 else []
            continue
        c = raw[i]
        i += 1
        if c == "\r":
            col = 0
        elif c == "\n":
            row += 1
            col = 0
        elif c == "\b":
            col = max(0, col - 1)
        elif c >= " " or c == "\t":
            while len(lines) <= row:
                lines.append([])
            line = lines[row]
            while len(line) < col:
                line.append(" ")
            if col < len(line):
                line[col] = c
            else:
                line.append(c)
            col += 1
    return "\n".join("".join(x).rstrip() for x in lines).strip("\n")


def login_command(kind: str, target: str = "") -> list[str] | None:
    if kind == "claude-code":
        return ["auth", "login", "--claudeai"]
    if kind == "codex-cli":
        return ["login", "--device-auth"]                 # a link + a one-time code: works on servers too
    if kind == "opencode":
        return ["--pure", "providers", "login"] + (["-p", target] if target else [])
    return None


class LoginSession:
    def __init__(self, kind: str, cmd: list[str], log_dir: Path):
        import fcntl
        import pty
        import struct
        import termios
        self.kind, self.started = kind, time.time()
        self.buf = b""
        self.lock = threading.Lock()
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))   # a real size: no 1-char lines
        env = {**os.environ, "TERM": "xterm-256color"}
        self.proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=str(log_dir),
                                     start_new_session=True, close_fds=True)
        os.close(slave)
        self.fd = master
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._expire, daemon=True).start()

    def _read(self) -> None:
        """The only owner of the terminal's file descriptor: it reads until the CLI is gone, then closes it —
        exactly once (closing a number twice could close something else that reused it)."""
        import select
        try:
            while True:
                try:
                    r, _, _ = select.select([self.fd], [], [], 0.5)
                    if r:
                        data = os.read(self.fd, 4096)
                        if not data:
                            break
                        with self.lock:
                            self.buf = (self.buf + data)[-20000:]
                    elif self.proc.poll() is not None:
                        break
                except OSError:
                    break
        finally:
            with self.lock:
                fd, self.fd = self.fd, -1
            if fd >= 0:
                os.close(fd)

    def _expire(self) -> None:
        while self.proc.poll() is None and time.time() - self.started < 900:      # 15 minutes to finish
            time.sleep(1)
        if self.proc.poll() is None:
            self.cancel()

    def text(self) -> str:
        with self.lock:
            raw = self.buf.decode(errors="replace")
        return render_terminal(raw)

    def state(self) -> dict:
        t = self.text()
        urls = re.findall(r"https://[^\s'\")<>]+", t)
        code = re.search(r"one-time code[^\n]*\n\s*([A-Z0-9]{4}-[A-Z0-9]{4,6})", t)
        tail = t[-2500:]
        low = tail.lower()
        wants = ("code" if re.search(r"paste (the )?code|authorization code|enter (the )?code", low) else
                 "key" if re.search(r"api key|token", low.rsplit("\n", 6)[-1] if "\n" in low else low) else "")
        rc = self.proc.poll()
        return {"running": rc is None, "exit": rc, "url": urls[-1].rstrip(".,") if urls else "",
                "device_code": code.group(1) if code else "", "wants": wants,
                "output": re.sub(r"(code=|state=)[A-Za-z0-9_\-]{6,}", r"\1…", tail)}

    def send(self, text: str = "", key: str = "") -> None:
        if self.proc.poll() is not None:
            raise ValueError("That sign-in has already finished — start it again.")
        data = KEYS.get(key, "") if key else text.replace("\n", "").strip() + "\r"
        with self.lock:
            if self.fd < 0:
                raise ValueError("That sign-in has already finished — start it again.")
            os.write(self.fd, data.encode())

    def cancel(self) -> None:
        if self.proc.poll() is None:
            import signal
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)       # the whole group: no orphaned login servers
            except (ProcessLookupError, PermissionError):
                self.proc.kill()
            self.proc.wait()                                   # the reader thread then closes the terminal


_logins: dict[str, LoginSession] = {}
_login_lock = threading.Lock()


def login(provider: str, log_dir: Path, target: str = "", force: bool = False) -> dict:
    """Start (or rejoin) the sign-in for a CLI provider. `target`: for OpenCode, the provider id to log in to.
    Claude and Codex sign out the current account as soon as a new sign-in starts — so if one is already signed
    in, nothing starts without `force` (the console asks first)."""
    kind = _kind(provider)
    cmd = login_command(kind, target)
    if not cmd:
        return {"started": False, "error": "This provider uses an API key — paste it in the key box."}
    b = _bin(kind)
    if not b:
        return {"started": False, "error": f"Install the CLI first: {INSTALL[kind]}"}
    running = _logins.get(kind)
    if kind in ("claude-code", "codex-cli") and not force and not (running and running.proc.poll() is None):
        if _check(LLMConfig(provider=kind)).get("logged_in"):
            return {"started": False, "confirm": True,
                    "error": "Already signed in. Signing in again replaces this account (it signs out first)."}
    with _login_lock:                                     # two clicks never start two logins
        cur = _logins.get(kind)
        if cur and cur.proc.poll() is None:
            return {"started": True, **cur.state()}
        if cur:
            cur.cancel()
        log_dir.mkdir(parents=True, exist_ok=True)
        _logins[kind] = LoginSession(kind, [b, *cmd], log_dir)
    time.sleep(1.5)                                       # usually enough for the link / code to be printed
    return {"started": True, **_logins[kind].state()}


def login_state(provider: str) -> dict:
    s = _logins.get(_kind(provider))
    return s.state() if s else {"running": False, "exit": None, "output": ""}


def login_input(provider: str, text: str = "", key: str = "") -> dict:
    s = _logins.get(_kind(provider))
    if not s:
        raise ValueError("No sign-in is running — click Log in first.")
    s.send(text=text, key=key)
    time.sleep(1.0)
    return s.state()


def login_cancel(provider: str) -> dict:
    s = _logins.pop(_kind(provider), None)
    if s:
        s.cancel()
    return {"cancelled": bool(s)}


# -- the catalogue behind Settings → Models ---------------------------------------------------------------------
_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def ollama_models(base: str = "http://localhost:11434") -> tuple[bool, list[str]]:
    """Is a local Ollama running, and which models does it have?"""
    import urllib.request
    try:
        with urllib.request.urlopen(base.rstrip("/").removesuffix("/v1") + "/api/tags", timeout=1.5) as r:
            data = json.loads(r.read())
        return True, sorted(m.get("name", "") for m in data.get("models", []) if m.get("name"))
    except Exception:  # noqa: BLE001
        return False, []


def opencode_info() -> dict:
    """OpenCode on this machine: installed? version, providers signed in, models by provider (free ones marked)."""
    b = _bin("opencode")
    if not b:
        return {"installed": False, "install": INSTALL["opencode"]}
    rc, ver = _run([b, "--version"], timeout=15)
    models = _cached("opencode-models", 300, opencode_models)
    creds = opencode_credentials()
    groups: dict[str, dict] = {}
    for mid in models:
        prov = mid.split("/", 1)[0]
        g = groups.setdefault(prov, {"provider": prov, "models": [], "free": prov == "opencode",
                                     "signed_in": prov == "opencode" or prov in creds})
        g["models"].append(mid)
    for c in creds:
        groups.setdefault(c, {"provider": c, "models": [], "free": False, "signed_in": True})
    order = sorted(groups.values(), key=lambda g: (not g["signed_in"], not g["free"], g["provider"]))
    return {"installed": True, "version": ver.strip().splitlines()[-1] if ver.strip() else "", "credentials": creds,
            "providers": order}


# Suggestions only — any model id the provider accepts works.
SUGGEST = {"claude-code": ["sonnet", "opus", "haiku"], "codex-cli": [],
           "anthropic": ["claude-sonnet-5", "claude-opus-5-5", "claude-haiku-4-5-20251001"],
           "openai": [], "openrouter": ["anthropic/claude-sonnet-5", "openai/gpt-5", "z-ai/glm-4.6"]}


def catalog() -> list[dict]:
    """Every way the team can think, with what's ready and what to do next."""
    out = []
    for pid, label, conf in (("claude-code", "Claude subscription (Claude Code CLI)", LLMConfig(provider="claude-code")),
                             ("codex-cli", "ChatGPT / Codex subscription (Codex CLI)", LLMConfig(provider="codex-cli"))):
        c = check(conf)
        out.append({"id": pid, "label": label, "kind": "cli", "ok": c["ok"], "installed": c["installed"],
                    "detail": c["detail"], "fix": c["fix"], "install": INSTALL[pid], "can_login": c["installed"],
                    "models": SUGGEST[pid], "sessions": True})
    oc = opencode_info()
    out.append({"id": "opencode", "label": "OpenCode — GLM, Claude, GPT, Gemini, free models…", "kind": "opencode",
                "ok": oc.get("installed", False) and any(g["signed_in"] for g in oc.get("providers", [])),
                "installed": oc.get("installed", False), "install": INSTALL["opencode"], "sessions": True,
                "detail": (f"v{oc.get('version', '')} · signed in: {', '.join(oc.get('credentials') or []) or 'none'} · "
                           f"free models available") if oc.get("installed") else "not installed",
                "fix": "" if oc.get("installed") else f"Install it on this machine: {INSTALL['opencode']}",
                "providers": oc.get("providers", [])})
    for pid, label, env, base in (("anthropic", "Anthropic API (pay per use)", "ANTHROPIC_API_KEY", ""),
                                  ("openai", "OpenAI API (pay per use)", "OPENAI_API_KEY", ""),
                                  ("openrouter", "OpenRouter — hundreds of models, one key", "OPENROUTER_API_KEY",
                                   "https://openrouter.ai/api/v1")):
        has = bool(os.environ.get(env))
        out.append({"id": pid, "label": label, "kind": "api", "ok": has, "installed": True, "key_env": env,
                    "base_url": base, "detail": f"{env} is set" if has else f"{env} not set",
                    "fix": "" if has else "Paste your API key.", "models": SUGGEST.get(pid, [])})
    running, models = ollama_models()
    out.append({"id": "ollama", "label": "Ollama — models on this machine (free, private)", "kind": "local",
                "ok": running and bool(models), "installed": running, "base_url": "http://localhost:11434/v1",
                "detail": (f"running · {len(models)} model(s)" if running else "not running"),
                "fix": "" if running and models else ("Pull a model: ollama pull llama3.1" if running else
                                                      "Install from ollama.com and start it (ollama serve)."),
                "models": models})
    return out


def providers_in_use(cfg) -> list[dict]:
    """Each distinct model setup and who uses it (company default + anyone with their own)."""
    groups: dict[tuple, dict] = {}
    for m in cfg.team:
        c = cfg.llm_for(m)
        key = (_kind(c.provider), c.model, c.base_url, c.api_key_env)
        g = groups.setdefault(key, {"config": c, "people": [], "default": False})
        g["people"].append(m.id)
        if not m.llm:
            g["default"] = True
    for pid, p in (getattr(cfg, "projects", None) or {}).items():       # project models are in use too
        for m in cfg.project_members(pid):
            if cfg.llm_source(m, pid) not in ("project", "person_project"):
                continue
            c = cfg.llm_for(m, pid)
            key = (_kind(c.provider), c.model, c.base_url, c.api_key_env)
            g = groups.setdefault(key, {"config": c, "people": [], "default": False})
            g["people"].append(f"{m.id}@{pid}")
    return list(groups.values()) or [{"config": cfg.llm, "people": [], "default": True}]


def is_cli(provider: str) -> bool:
    return _kind(provider) in ("claude-code", "codex-cli") or provider in CLI_PROVIDERS
