from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.backup_scheduler import _next_run, select_retained_backups


def _backup(tmp_path: Path, stamp: str) -> Path:
    path = tmp_path / f"docintel-{stamp}.dcbak"
    path.touch()
    return path


def test_retention_keeps_daily_weekly_and_monthly_representatives(tmp_path):
    # 100 consecutive days spanning multiple weeks and months.
    from datetime import timedelta
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    paths = [
        _backup(tmp_path, (start + timedelta(days=i)).strftime("%Y%m%dT020000Z"))
        for i in range(100)
    ]
    kept = select_retained_backups(paths)
    assert len(kept) <= 14
    assert all(path in kept for path in sorted(paths, reverse=True)[:7])
    # Three most recent calendar months must each have a retained representative.
    retained_months = {
        datetime.strptime(path.stem.removeprefix("docintel-"), "%Y%m%dT%H%M%SZ").month
        for path in kept
    }
    assert {2, 3, 4}.issubset(retained_months)


def test_retention_ignores_unrecognized_files(tmp_path):
    valid = _backup(tmp_path, "20261126T020000Z")
    invalid = tmp_path / "manual-copy.dcbak"
    invalid.touch()
    assert select_retained_backups([valid, invalid]) == {valid}


def test_next_run_uses_utc_and_rolls_to_tomorrow_when_time_passed():
    now = datetime(2026, 11, 26, 20, 1, tzinfo=timezone.utc)
    assert _next_run(20, 0, now) == datetime(2026, 11, 27, 20, 0, tzinfo=timezone.utc)


def test_next_run_returns_today_when_time_is_still_ahead():
    now = datetime(2026, 11, 26, 19, 59, tzinfo=timezone.utc)
    assert _next_run(20, 0, now) == datetime(2026, 11, 26, 20, 0, tzinfo=timezone.utc)


def test_retention_verifies_kept_backups_before_deleting_expired(tmp_path, monkeypatch):
    import app.backup_scheduler as scheduler
    from datetime import timedelta

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    paths = [
        _backup(tmp_path, (start + timedelta(days=i)).strftime("%Y%m%dT020000Z"))
        for i in range(100)
    ]
    verified = []
    monkeypatch.setattr(scheduler, "_verify_encrypted", lambda path, password, scratch: verified.append(path))
    removed = scheduler.apply_retention(tmp_path, "not-a-real-password", tmp_path)
    assert set(verified) == scheduler.select_retained_backups(paths)
    assert set(removed) == set(paths) - set(verified)
    assert all(path.exists() for path in verified)
    assert all(not path.exists() for path in removed)


def test_retention_deletes_nothing_if_any_retained_backup_fails_verification(tmp_path, monkeypatch):
    import app.backup_scheduler as scheduler
    from datetime import timedelta

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    paths = [
        _backup(tmp_path, (start + timedelta(days=i)).strftime("%Y%m%dT020000Z"))
        for i in range(100)
    ]
    def fail_verification(path, password, scratch):
        raise RuntimeError("corrupt retained backup")
    monkeypatch.setattr(scheduler, "_verify_encrypted", fail_verification)
    with pytest.raises(RuntimeError, match="corrupt retained backup"):
        scheduler.apply_retention(tmp_path, "not-a-real-password", tmp_path)
    assert all(path.exists() for path in paths)


def test_request_gate_blocks_new_requests_until_resumed():
    import asyncio
    from app.backup_scheduler import BackupRequestGate

    async def exercise():
        gate = BackupRequestGate()
        assert await gate.enter()

        pause = asyncio.create_task(gate.pause_and_drain())
        await asyncio.sleep(0)
        assert not pause.done()

        await gate.leave()
        await pause

        assert not await gate.enter()
        await gate.resume()
        assert await gate.enter()
        await gate.leave()

    asyncio.run(exercise())


def test_request_gate_cancellation_during_drain_resumes_gate():
    import asyncio
    from app.backup_scheduler import BackupRequestGate

    async def exercise():
        gate = BackupRequestGate()
        assert await gate.enter()

        pause = asyncio.create_task(gate.pause_and_drain())
        await asyncio.sleep(0)
        pause.cancel()

        with pytest.raises(asyncio.CancelledError):
            await pause

        assert await gate.enter()
        await gate.leave()
        await gate.leave()

    asyncio.run(exercise())


def test_scheduled_backup_creates_encrypted_verified_copy_and_cleans_plaintext(tmp_path, monkeypatch):
    import app.backup_scheduler as scheduler
    from app.backup_scheduler import create_scheduled_backup
    from app.db import init_db

    data_dir = tmp_path / "appdata"
    backup_dir = tmp_path / "backups"
    monkeypatch.setenv("DOCINTEL_PERSISTENT_DISK_MOUNT", str(tmp_path))
    monkeypatch.setenv("DOCINTEL_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DOCINTEL_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("DOCINTEL_BACKUP_PASSWORD", "test-password-123")
    monkeypatch.setattr(scheduler.os.path, "ismount", lambda _path: True)
    init_db()

    result = create_scheduled_backup()
    assert result.is_file()
    assert result.suffix == ".dcbak"
    assert list(backup_dir.glob("*.dcbak")) == [result]
    assert not list((backup_dir / ".work").iterdir())


def test_render_backup_config_refuses_unmounted_persistent_disk(tmp_path, monkeypatch):
    import app.backup_scheduler as scheduler
    from app.backup import BackupError

    mount = tmp_path / "disk"
    mount.mkdir()
    monkeypatch.setenv("DOCINTEL_PERSISTENT_DISK_MOUNT", str(mount))
    monkeypatch.setenv("DOCINTEL_DATA_DIR", str(mount / "appdata"))
    monkeypatch.setenv("DOCINTEL_BACKUP_DIR", str(mount / "backups"))
    monkeypatch.setenv("DOCINTEL_BACKUP_PASSWORD", "test-password-123")
    monkeypatch.setattr(scheduler.os.path, "ismount", lambda _path: False)
    with pytest.raises(BackupError, match="not mounted"):
        scheduler._backup_config(require_mount=True)


def test_render_backup_config_accepts_mounted_disk_and_nested_paths(tmp_path, monkeypatch):
    import app.backup_scheduler as scheduler

    mount = tmp_path / "disk"
    mount.mkdir()
    monkeypatch.setenv("DOCINTEL_PERSISTENT_DISK_MOUNT", str(mount))
    monkeypatch.setenv("DOCINTEL_DATA_DIR", str(mount / "appdata"))
    monkeypatch.setenv("DOCINTEL_BACKUP_DIR", str(mount / "backups"))
    monkeypatch.setenv("DOCINTEL_BACKUP_PASSWORD", "test-password-123")
    monkeypatch.setattr(scheduler.os.path, "ismount", lambda _path: True)
    assert scheduler._backup_config(require_mount=True)[0] == mount / "backups"


def test_retention_respects_quota_and_keeps_newest(tmp_path, monkeypatch):
    import app.backup_scheduler as scheduler
    from datetime import timedelta

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    paths = [
        _backup(tmp_path, (start + timedelta(days=i)).strftime("%Y%m%dT020000Z"))
        for i in range(3)
    ]
    paths[0].write_bytes(b"a" * 3)
    paths[1].write_bytes(b"b" * 4)
    paths[2].write_bytes(b"c" * 6)

    monkeypatch.setattr(
        scheduler, "_verify_encrypted",
        lambda path, password, scratch: None,
    )
    removed = scheduler.apply_retention(
        tmp_path, "test-password", tmp_path, limit_bytes=10
    )

    assert paths[2].exists()
    assert paths[1].exists()
    assert not paths[0].exists()
    assert removed == [paths[0]]


def test_retention_does_not_delete_if_newest_cannot_fit(tmp_path):
    import app.backup_scheduler as scheduler
    from app.backup import BackupError

    newest = _backup(tmp_path, "20261126T020000Z")
    newest.write_bytes(b"x" * 20)

    with pytest.raises(BackupError, match="newest encrypted backup"):
        scheduler.apply_retention(
            tmp_path, "test-password", tmp_path, limit_bytes=10
        )
    assert newest.exists()


def test_retention_preserves_unrecognized_encrypted_files(tmp_path, monkeypatch):
    import app.backup_scheduler as scheduler

    valid = _backup(tmp_path, "20261126T020000Z")
    unknown = tmp_path / "manual-copy.dcbak"
    valid.write_bytes(b"valid")
    unknown.write_bytes(b"unknown")

    monkeypatch.setattr(
        scheduler, "_verify_encrypted",
        lambda path, password, scratch: None,
    )
    scheduler.apply_retention(
        tmp_path, "test-password", tmp_path, limit_bytes=100
    )
    assert valid.exists()
    assert unknown.exists()


def test_scheduled_backup_stages_before_quota_retention(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import app.backup_scheduler as scheduler

    data_dir = tmp_path / "appdata"
    backup_dir = tmp_path / "backups"
    data_dir.mkdir()
    backup_dir.mkdir()

    old = _backup(backup_dir, "20261125T020000Z")
    old.write_bytes(b"o" * 8)

    monkeypatch.setattr(
        scheduler,
        "_backup_config",
        lambda: (backup_dir, "test-password-123", 20, 0, 10),
    )
    monkeypatch.setattr(
        scheduler,
        "get_settings",
        lambda: SimpleNamespace(data_dir=data_dir),
    )
    monkeypatch.setattr(
        scheduler,
        "create_backup",
        lambda destination, source: Path(destination).mkdir(parents=True),
    )
    monkeypatch.setattr(scheduler, "verify_backup", lambda path: None)

    staged_quota_dirs = []

    def fake_encrypt(source, destination, password, quota_dir, max_bytes):
        staged_quota_dirs.append(Path(quota_dir))
        target = Path(destination)
        target.write_bytes(b"n" * 6)
        return target

    monkeypatch.setattr(scheduler, "encrypt_backup", fake_encrypt)
    monkeypatch.setattr(
        scheduler, "_verify_encrypted",
        lambda path, password, scratch: None,
    )

    result = scheduler.create_scheduled_backup()

    assert staged_quota_dirs == [backup_dir / ".work"]
    assert result.is_file()
    assert result.stat().st_size == 6
    assert not old.exists()
    assert list(backup_dir.glob("*.dcbak")) == [result]
    assert not list((backup_dir / ".work").iterdir())


def test_retention_rejects_oversized_unknown_only_backups(tmp_path):
    import app.backup_scheduler as scheduler
    from app.backup import BackupError

    unknown = tmp_path / "manual-copy.dcbak"
    unknown.write_bytes(b"x" * 20)

    with pytest.raises(BackupError, match="Unrecognized encrypted backups"):
        scheduler.apply_retention(
            tmp_path, "test-password", tmp_path, limit_bytes=10
        )

    assert unknown.exists()
    assert unknown.stat().st_size == 20


def test_worker_shutdown_wait_retries_until_worker_stops():
    import asyncio
    from app.backup_scheduler import _wait_for_worker_shutdown

    class FakeWorker:
        def __init__(self):
            self.attempts = 0

        def stop(self, timeout=None):
            self.attempts += 1
            if self.attempts < 3:
                raise TimeoutError("still running")

    worker = FakeWorker()
    asyncio.run(_wait_for_worker_shutdown(worker))
    assert worker.attempts == 3


def _run_scheduler_one_cycle(monkeypatch, tmp_path, scheduler):
    import asyncio
    from datetime import datetime, timedelta, timezone

    class FakeGate:
        def __init__(self):
            self.paused = False
            self.resume_count = 0

        async def pause_and_drain(self):
            self.paused = True

        async def resume(self):
            self.paused = False
            self.resume_count += 1

    gate = FakeGate()
    monkeypatch.setattr(scheduler, "backup_gate", gate)
    monkeypatch.setattr(
        scheduler, "_backup_config",
        lambda: (tmp_path, "test-password-123", 0, 0, 10),
    )
    monkeypatch.setattr(
        scheduler, "_next_run",
        lambda *_: datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    sleep_calls = 0

    async def stop_after_one_cycle(_delay):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise asyncio.CancelledError()

    monkeypatch.setattr(scheduler.asyncio, "sleep", stop_after_one_cycle)
    return gate


def test_scheduler_restarts_worker_and_resumes_gate_after_backup(
    monkeypatch, tmp_path
):
    import asyncio
    import app.backup_scheduler as scheduler
    from app import worker

    gate = _run_scheduler_one_cycle(monkeypatch, tmp_path, scheduler)
    calls = []
    monkeypatch.setattr(worker, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(worker, "start", lambda: calls.append("start"))
    monkeypatch.setattr(
        scheduler, "create_scheduled_backup", lambda: calls.append("backup")
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler.scheduler_loop())

    assert calls == ["stop", "backup", "start"]
    assert gate.resume_count == 1
    assert gate.paused is False


def test_scheduler_keeps_gate_closed_when_worker_restart_fails(
    monkeypatch, tmp_path
):
    import asyncio
    import app.backup_scheduler as scheduler
    from app import worker

    gate = _run_scheduler_one_cycle(monkeypatch, tmp_path, scheduler)
    monkeypatch.setattr(worker, "stop", lambda: None)

    def fail_start():
        raise RuntimeError("simulated worker restart failure")

    monkeypatch.setattr(worker, "start", fail_start)
    monkeypatch.setattr(scheduler, "create_scheduled_backup", lambda: None)

    with pytest.raises(RuntimeError, match="simulated worker restart failure"):
        asyncio.run(scheduler.scheduler_loop())

    assert gate.resume_count == 0
    assert gate.paused is True

def test_scheduler_keeps_gate_closed_after_unexpected_worker_stop_error(
    monkeypatch, tmp_path
):
    import app.backup_scheduler as scheduler
    from app import worker

    gate = _run_scheduler_one_cycle(monkeypatch, tmp_path, scheduler)

    def fail_stop(timeout=None):
        raise RuntimeError("simulated worker shutdown failure")

    monkeypatch.setattr(worker, "stop", fail_stop)
    monkeypatch.setattr(
        worker, "start",
        lambda: pytest.fail("worker must not restart after uncertain shutdown"),
    )

    with pytest.raises(RuntimeError, match="simulated worker shutdown failure"):
        import asyncio
        asyncio.run(scheduler.scheduler_loop())

    assert gate.resume_count == 0
    assert gate.paused is True
