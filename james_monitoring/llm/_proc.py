"""Running model CLIs safely: a minimal environment (no API keys, bot tokens or other secrets from this process),
the whole process tree killed on timeout (the CLIs are node shims that spawn the real binary), nothing left behind."""
from __future__ import annotations

import os
import signal
import subprocess

from . import LLMError

# Only what a CLI needs to find itself, its login and a temp dir. Everything else (ANTHROPIC_API_KEY, TG_TOKEN_*,
# SLACK_*, GH tokens, …) stays out of reach of a model that might be talked into printing it.
PASS = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM", "SHELL", "TZ",
        "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "__CF_USER_TEXT_ENCODING",
        "CODEX_HOME", "CLAUDE_CONFIG_DIR", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "HTTPS_PROXY", "HTTP_PROXY",
        "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")


def safe_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in PASS}
    env.update({k: v for k, v in extra.items() if v is not None})
    return env


def run(cmd: list[str], *, input: str, cwd: str, env: dict, timeout: float, what: str) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            cwd=cwd, env=env, start_new_session=True)
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill(proc)
        raise LLMError(f"{what} timed out after {int(timeout)}s") from None
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)          # the shim *and* the real binary it started
    except (ProcessLookupError, PermissionError):
        proc.kill()
    try:
        proc.communicate(timeout=5)
    except Exception:  # noqa: BLE001
        pass
