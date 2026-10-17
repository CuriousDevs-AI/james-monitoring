import stat
import zipfile
from pathlib import Path

import pytest

from james_monitoring.backups import restore, snapshot
from james_monitoring.cli import main


def test_private_snapshot_round_trip_includes_receipts_but_not_links_or_env(rt, tmp_path):
    root = rt.ws.root / ".jm"
    rt.chat.append("sofia", "owner", "private message")
    rt.ws.update_state(lambda s: s.update(paused_all=True))
    (root / "schedule-runs").mkdir()
    (root / "schedule-runs/run.json").write_text('{"status":"interrupted"}')
    (root / ".env").write_text("TOKEN=never-copy")
    (root / ".env.bak").write_text("TOKEN=never-copy")
    outside = tmp_path / "outside.txt"
    outside.write_text("never-copy-this")
    (root / "outside-link").symlink_to(outside)
    (root / "outside-folder").symlink_to(tmp_path, target_is_directory=True)
    archive = Path(rt.backup_runtime())
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert stat.S_IMODE(archive.parent.stat().st_mode) == 0o700
    target = tmp_path / "recovered-runtime"
    count = restore(archive, target)
    assert count >= 3
    assert "private message" in (target / "chat/sofia.jsonl").read_text()
    assert (target / "schedule-runs/run.json").read_text() == '{"status":"interrupted"}'
    assert not (target / ".env").exists() and not (target / "outside-link").exists()
    assert not (target / "outside-folder").exists()
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in target.rglob("*") if p.is_file())
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    with pytest.raises(ValueError, match="never overwritten"):
        restore(archive, target)


def test_failed_snapshot_preserves_previous_archive(rt, monkeypatch):
    path = Path(rt.backup_runtime())
    original = path.read_bytes()
    (rt.ws.root / ".jm/new.txt").write_text("new")

    def fail(*args, **kwargs):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr("james_monitoring.backups.shutil.copyfileobj", fail)
    with pytest.raises(OSError):
        rt.backup_runtime()
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".snapshot-*.tmp"))


def test_retention_keeps_other_zip_names_and_validates_count(rt):
    root = rt.ws.root / ".jm"
    first = snapshot(root, "2026-10-01")
    unrelated = first.parent / "manual-evidence.zip"
    unrelated.write_bytes(b"keep this")
    snapshot(root, "2026-10-02", keep=1)
    assert not first.exists() and unrelated.read_bytes() == b"keep this"
    with pytest.raises(ValueError):
        snapshot(root, "2026-10-03", keep=0)
    assert (first.parent / "2026-10-02.zip").exists()


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a/../../escape", "a\\escape", "C:escape",
                                  "sandbox/auth.json", ".env", "backups/old.zip", "job.lock", "a//b", "a/./b"])
def test_restore_rejects_unsafe_entries_before_creating_destination(tmp_path, name):
    archive, target = tmp_path / "bad.zip", tmp_path / "restored"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(name, "payload")
    with pytest.raises(ValueError):
        restore(archive, target)
    assert not target.exists()


def test_restore_rejects_zip_symlink_size_and_conflicting_paths(tmp_path):
    archive, target = tmp_path / "bad.zip", tmp_path / "restored"
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(link, "../../outside")
    with pytest.raises(ValueError):
        restore(archive, target)
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("big", "x" * 1024)
    with pytest.raises(ValueError, match="limit"):
        restore(archive, target, max_bytes=100)
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("file", "x")
        z.writestr("file/child", "x")
    with pytest.raises(ValueError, match="conflicting"):
        restore(archive, target)
    assert not target.exists()


def test_restore_cli_requires_acknowledgment_and_starts_no_runtime(rt, tmp_path, capsys, monkeypatch):
    archive = rt.backup_runtime()
    target = tmp_path / "recovered"
    def forbidden(*args, **kwargs):
        raise AssertionError("restore must not load config/runtime")
    monkeypatch.setattr("james_monitoring.cli._cfg", forbidden)
    monkeypatch.setattr("james_monitoring.cli._runtime", forbidden)
    with pytest.raises(SystemExit):
        main(["restore-runtime", archive, str(target)])
    assert not target.exists()
    main(["restore-runtime", archive, str(target), "--acknowledge-private-data"])
    assert "No services started" in capsys.readouterr().out


def test_snapshot_refuses_symlink_destination(rt, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (rt.ws.root / ".jm/backups").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        rt.backup_runtime()
    assert not list(outside.iterdir())


def test_restore_write_failure_leaves_explicit_incomplete_marker(rt, tmp_path, monkeypatch):
    rt.chat.append("sofia", "owner", "message")
    archive = Path(rt.backup_runtime())
    target = tmp_path / "partial"
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr("james_monitoring.backups.shutil.copyfileobj", fail)
    with pytest.raises(OSError):
        restore(archive, target)
    assert (target / "RESTORE-INCOMPLETE").exists()


def test_offline_backup_cli_requires_stopped_confirmation(rt, monkeypatch, capsys):
    monkeypatch.setattr("james_monitoring.cli._cfg", lambda args: rt.cfg)
    def forbidden(*args, **kwargs):
        raise AssertionError("offline backup must not start runtime")
    monkeypatch.setattr("james_monitoring.cli._runtime", forbidden)
    with pytest.raises(SystemExit):
        main(["backup-runtime"])
    assert not (rt.ws.root / ".jm/backups").exists()
    main(["backup-runtime", "--confirm-stopped"])
    assert "Not encrypted" in capsys.readouterr().out
    assert len(list((rt.ws.root / ".jm/backups").glob("*.zip"))) == 1


def test_duplicate_and_corrupt_archives_are_refused_before_restore(tmp_path):
    path, target = tmp_path / "bad.zip", tmp_path / "restored"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("state.json", "first")
        with pytest.warns(UserWarning, match="Duplicate"):
            archive.writestr("state.json", "second")
    with pytest.raises(ValueError):
        restore(path, target)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("state.json", "original-payload")
    path.write_bytes(path.read_bytes().replace(b"original-payload", b"modified-payload"))
    with pytest.raises((ValueError, zipfile.BadZipFile)):
        restore(path, target)
    assert not target.exists()


def test_incomplete_recovery_cannot_be_snapshotted(rt):
    (rt.ws.root / ".jm/RESTORE-INCOMPLETE").touch()
    with pytest.raises(ValueError, match="incomplete"):
        rt.backup_runtime()
    from james_monitoring.runtime import Runtime
    from james_monitoring.llm.fake import FakeLLM
    with pytest.raises(ValueError, match="incomplete"):
        Runtime(rt.cfg, FakeLLM())
