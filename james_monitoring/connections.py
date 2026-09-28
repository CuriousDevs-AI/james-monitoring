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

LOGIN = {"claude-code": ["auth", "login", "--claudeai"], "codex-cli": ["login"]}
LABEL = {"claude-code": "Claude subscription (claude CLI)", "codex-cli": "ChatGPT / Codex subscription (codex CLI)",
         "opencode": "OpenCode (any model, incl. free)",
         "anthropic": "Anthropic API", "openai": "OpenAI-compatible API", "fake": "Fake (tests)"}
INSTALL = {"claude-code": "npm install -g @anthropic-ai/claude-code", "codex-cli": "npm install -g @openai/codex",
           "opencode": "npm install -g opencode-ai"}
# Logins that need a terminal (an interactive picker): run them with `jm connection login <provider>`.
TERMINAL_LOGIN = {"opencode": ["providers", "login"]}


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
    if p in ("openai", "codex", "ollama", "openrouter", "openai-compatible"):
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
    """Fast readiness check for one model setup. `ok` = ready to use; `fix` = what to do if not."""
    kind = _kind(conf.provider)
    out = {"provider": kind, "label": LABEL.get(kind, conf.provider), "model": conf.model or "default",
           "ok": False, "installed": True, "logged_in": None, "detail": "", "fix": "", "can_login": kind in LOGIN}
    if kind == "fake":
        return {**out, "ok": True, "logged_in": True, "detail": "no model (test mode)"}
    if kind == "opencode":
        b = _bin(kind)
        if not b:
            return {**out, "installed": False, "can_login": False, "detail": "CLI not installed",
                    "fix": f"Install it on this machine: {INSTALL[kind]}"}
        prov = (conf.model or "").split("/")[0]
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
        return {**out, "ok": logged, "logged_in": logged, "can_login": False,
                "detail": f"{prov} credential found in OpenCode" if logged else f"no {prov} login in OpenCode",
                "fix": "" if logged else "In a terminal on this machine: jm connection login opencode"}
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
                "fix": "" if logged else "Click Log in — your browser opens to sign in with your subscription."}
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


# -- login -------------------------------------------------------------------------------------
_logins: dict[str, dict] = {}
_login_lock = threading.Lock()


def login(provider: str, log_dir: Path) -> dict:
    """Start the CLI's own sign-in in the background. It opens the browser on this machine; we also capture the
    sign-in link it prints, in case the browser didn't open (e.g. the console runs on a server)."""
    kind = _kind(provider)
    if kind not in LOGIN:
        return {"started": False, "error": "This provider uses an API key — paste it in the AI model settings."}
    b = _bin(kind)
    if not b:
        return {"started": False, "error": f"Install the CLI first: {INSTALL[kind]}"}
    with _login_lock:                                     # two clicks never start two logins
        cur = _logins.get(kind)
        if cur and cur["proc"].poll() is None:
            return {"started": True, "running": True, "url": cur.get("url", "")}
        return _start_login(kind, b, log_dir)


def _start_login(kind: str, b: str, log_dir: Path) -> dict:
    log_dir.mkdir(parents=True, exist_ok=True)
    logf = log_dir / f"login-{kind}.log"
    fh = open(logf, "w")
    proc = subprocess.Popen([b, *LOGIN[kind]], stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            start_new_session=True)
    entry = {"proc": proc, "log": logf, "url": ""}
    _logins[kind] = entry

    def watch():
        end = time.time() + 600                           # give up after 10 minutes
        while time.time() < end and proc.poll() is None:
            m = re.search(r"https://\S+", logf.read_text(errors="replace")) if logf.exists() else None
            if m and not entry["url"]:
                entry["url"] = m.group(0).rstrip(").,")
            time.sleep(0.5)
        if proc.poll() is None:                           # abandoned: the whole process group, no orphans
            import signal
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.wait()
        fh.close()
    threading.Thread(target=watch, daemon=True).start()
    time.sleep(1.5)                                       # usually enough for the link to be printed
    return {"started": True, "url": entry["url"]}


def login_state(provider: str) -> dict:
    e = _logins.get(_kind(provider))
    if not e:
        return {"running": False}
    rc = e["proc"].poll()
    tail = e["log"].read_text(errors="replace")[-400:] if e["log"].exists() else ""
    return {"running": rc is None, "exit": rc, "url": e.get("url", ""), "output": tail}


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
    return list(groups.values()) or [{"config": cfg.llm, "people": [], "default": True}]


def is_cli(provider: str) -> bool:
    return _kind(provider) in ("claude-code", "codex-cli") or provider in CLI_PROVIDERS
