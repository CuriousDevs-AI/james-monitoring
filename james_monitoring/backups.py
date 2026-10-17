"""Private local runtime snapshots, plus an offline restore into a new directory only.

ZIP is not encryption and a live snapshot is not a cross-file transaction. These are never cloud uploads.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from .fileio import path_lock


def included(parts: tuple[str, ...]) -> bool:
    return not any(p in ("backups", "sandbox", ".env", "RESTORE-INCOMPLETE")
                   or p.startswith(".env.") or p.endswith(".lock") for p in parts)


def snapshot(root: Path, day: str, keep: int = 14) -> Path:
    if not isinstance(keep, int) or keep < 1 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("Choose a valid snapshot date and retain at least one backup.")
    dest = root / "backups"
    if root.is_symlink() or dest.is_symlink():
        raise ValueError("Runtime and backup directories must not be symbolic links.")
    if (root / "RESTORE-INCOMPLETE").exists():
        raise ValueError("Refusing to snapshot an incomplete recovery.")
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest.chmod(0o700)
    path = dest / (day + ".zip")
    with path_lock(path):
        fd, temporary = tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=dest)
        try:
            with os.fdopen(fd, "w+b") as handle:
                with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as archive:
                    for source in sorted(root.rglob("*")):
                        relative = source.relative_to(root)
                        if not included(relative.parts) or source.is_symlink() or not source.is_file():
                            continue
                        # Cooperate with chat/state writers, and refuse a last-moment file symlink swap.
                        with path_lock(source):
                            source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
                            with os.fdopen(source_fd, "rb") as reader:
                                if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
                                    raise ValueError("Runtime backup contains a non-regular file.")
                                info = zipfile.ZipInfo(relative.as_posix())
                                info.external_attr = (stat.S_IFREG | 0o600) << 16
                                info.compress_type = zipfile.ZIP_DEFLATED
                                with archive.open(info, "w") as writer:
                                    shutil.copyfileobj(reader, writer)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)  # the previous valid snapshot survives a failed ZIP write
            if os.name == "posix":
                directory_fd = os.open(dest, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            # Only our date-named snapshots are eligible; never remove arbitrary ZIP files.
            dated = sorted(p for p in dest.glob("*.zip") if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.zip", p.name))
            for old in dated[:-keep]:
                old.unlink()
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return path


def restore(archive_path: Path, destination: Path, max_bytes: int = 250_000_000) -> int:
    """Validate everything before creating a new private directory; never overwrite existing data.

    Destination is the recovered runtime directory itself (later installed as <workspace>/.jm while stopped).
    This does not load a config, start a model, run a scheduler, send a message or contact any service.
    """
    if destination.exists() or destination.is_symlink():
        raise ValueError("Restore requires a new destination; existing data is never overwritten.")
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > 10_000 or sum(m.file_size for m in members) > max_bytes:
            raise ValueError("Archive exceeds the offline restore size/file limit.")
        seen = set()
        for member in members:
            name = member.filename
            parts = PurePosixPath(name).parts
            mode = member.external_attr >> 16
            if not parts or name.startswith("/") or "\\" in name or ":" in name or ".." in parts \
                    or name != "/".join(parts) or name in seen or member.is_dir() \
                    or not included(parts) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                raise ValueError("Archive contains an unsafe, duplicate or unsupported entry.")
            seen.add(name)
        if any("/".join(PurePosixPath(name).parts[:i]) in seen
               for name in seen for i in range(1, len(PurePosixPath(name).parts))):
            raise ValueError("Archive contains conflicting file and directory names.")
        if archive.testzip() is not None:
            raise ValueError("Archive integrity check failed.")
        destination.mkdir(mode=0o700)  # exclusive: an existing empty directory is refused too
        try:
            for member in members:
                path = destination.joinpath(*PurePosixPath(member.filename).parts)
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with archive.open(member) as reader, path.open("xb") as writer:
                    path.chmod(0o600)
                    shutil.copyfileobj(reader, writer)
                    writer.flush()
                    os.fsync(writer.fileno())
        except BaseException:
            # Preserve any partial output for inspection; never delete user-visible recovery data on error.
            marker = destination / "RESTORE-INCOMPLETE"
            marker.touch(mode=0o600, exist_ok=True)
            raise
    return len(members)
