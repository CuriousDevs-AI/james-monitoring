"""Safe writes. Every file the team depends on goes through here.

- atomic_write: temp file in the same folder → fsync → os.replace. A crash or a second writer never leaves a
  half-written file behind.
- path_lock: one lock per file, shared by every thread in the process *and* other processes (`jm chat` next to
  `jm run`) through an fcntl lock file.
- update_config: read → change → validate → keep config.yaml.bak → atomic replace, all under the lock, so two
  saves at once can't corrupt config.yaml or undo each other.
- set_env: add *or replace* KEY=VALUE lines in .env (a new API key or token must win over the old one).
"""
from __future__ import annotations

import contextlib
import os
import tempfile
import threading
from pathlib import Path
from typing import Callable, Iterator

import yaml

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _thread_lock(key: str) -> threading.RLock:
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.RLock()
        return _locks[key]


_held = threading.local()


@contextlib.contextmanager
def path_lock(path: Path | str) -> Iterator[None]:
    """Exclusive lock for `path` across threads and processes. Re-entrant within a thread."""
    key = str(Path(path).resolve())
    tl = _thread_lock(key)
    with tl:
        depth = getattr(_held, key, 0)
        setattr(_held, key, depth + 1)
        fh = None
        try:
            if depth == 0 and fcntl is not None:
                lock_file = Path(key + ".lock") if not Path(key).is_dir() else Path(key) / ".jm-lock"
                lock_file.parent.mkdir(parents=True, exist_ok=True)
                fh = open(lock_file, "a")
                fcntl.flock(fh, fcntl.LOCK_EX)
            yield
        finally:
            if fh is not None:
                fcntl.flock(fh, fcntl.LOCK_UN)
                fh.close()
            setattr(_held, key, depth)


def atomic_write(path: Path | str, text: str, mode: int | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        elif path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def dump_yaml(raw: dict) -> str:
    return yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)


def read_config(path: Path | str) -> dict:
    return yaml.safe_load(Path(path).read_text()) or {}


def write_config(path: Path | str, raw: dict) -> None:
    """Validate, back up the current file, then replace it atomically. Raises ConfigError on a bad config."""
    from .config import parse_config
    path = Path(path)
    parse_config(raw, base_dir=path.parent, path=path)          # never write a config we can't load back
    with path_lock(path):
        if path.exists():
            atomic_write(path.with_name(path.name + ".bak"), path.read_text())
        atomic_write(path, dump_yaml(raw))


def update_config(path: Path | str, fn: Callable[[dict], object]) -> dict:
    """The only safe way to change config.yaml: read-modify-write under one lock."""
    path = Path(path)
    with path_lock(path):
        raw = read_config(path) if path.exists() else {}
        fn(raw)
        write_config(path, raw)
        return raw


def set_env(env_path: Path | str, pairs: dict[str, str]) -> None:
    """Add or replace KEY=VALUE lines in .env (0600), and in this process's environment."""
    env_path = Path(env_path)
    pairs = {k: str(v) for k, v in pairs.items() if k and v}
    if not pairs:
        return
    with path_lock(env_path):
        lines = env_path.read_text().splitlines() if env_path.exists() else []
        done: set[str] = set()
        out = []
        for line in lines:
            key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
            if key in pairs:
                if key in done:
                    continue                                    # drop duplicates left by older versions
                out.append(f"{key}={pairs[key]}")
                done.add(key)
            else:
                out.append(line)
        out += [f"{k}={v}" for k, v in pairs.items() if k not in done]
        atomic_write(env_path, "\n".join(out).rstrip("\n") + "\n", mode=0o600)
    for k, v in pairs.items():
        os.environ[k] = v
