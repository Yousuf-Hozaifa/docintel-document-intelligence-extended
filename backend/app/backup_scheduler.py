"""Single-instance scheduled encrypted backups for Render persistent disks."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.responses import JSONResponse

from app.backup import BackupError, _reject_symlink_components, _reject_symlinks, create_backup, verify_backup
from app.backup_encryption import decrypt_backup, encrypt_backup
from app.config import get_settings

logger = logging.getLogger("docintel.backup")
_NAME = re.compile(r"^docintel-(\d{8}T\d{6}Z)\.dcbak$")


class BackupRequestGate:
    """Reject new requests and drain active requests during filesystem snapshot."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._paused = False
        self._active = 0

    async def enter(self) -> bool:
        async with self._condition:
            if self._paused:
                return False
            self._active += 1
            return True

    async def leave(self) -> None:
        async with self._condition:
            self._active -= 1
            if self._active == 0:
                self._condition.notify_all()

    async def pause_and_drain(self) -> None:
        async with self._condition:
            self._paused = True
            try:
                await self._condition.wait_for(lambda: self._active == 0)
            except BaseException:
                self._paused = False
                self._condition.notify_all()
                raise

    async def resume(self) -> None:
        async with self._condition:
            self._paused = False
            self._condition.notify_all()


backup_gate = BackupRequestGate()


class BackupGateMiddleware:
    def __init__(self, app, gate: BackupRequestGate):
        self.app = app
        self.gate = gate

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == "/api/health":
            await self.app(scope, receive, send)
            return
        if not await self.gate.enter():
            response = JSONResponse(
                {"detail": "A scheduled backup is in progress. Retry shortly."},
                status_code=503,
                headers={"Retry-After": "60"},
            )
            await response(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            await self.gate.leave()


def _backup_config(require_mount: bool = False) -> tuple[Path, str, int, int, int]:
    password = os.environ.get("DOCINTEL_BACKUP_PASSWORD", "")
    if len(password) < 12:
        raise BackupError("DOCINTEL_BACKUP_PASSWORD must contain at least 12 characters.")
    backup_dir = Path(os.environ.get("DOCINTEL_BACKUP_DIR", "/var/data/backups")).expanduser()
    mount_dir = Path(os.environ.get("DOCINTEL_PERSISTENT_DISK_MOUNT", "/var/data")).expanduser()
    if not backup_dir.is_absolute() or not mount_dir.is_absolute():
        raise BackupError("Backup and persistent-disk mount paths must be absolute.")
    if require_mount:
        if not mount_dir.is_dir() or not os.path.ismount(mount_dir):
            raise BackupError(f"Render persistent disk is not mounted at {mount_dir}; refusing to back up to ephemeral storage.")
        data_dir = get_settings().data_dir.expanduser().resolve()
        backup_path = backup_dir.resolve()
        mount_path = mount_dir.resolve()
        if mount_path not in data_dir.parents or mount_path not in backup_path.parents:
            raise BackupError("DOCINTEL_DATA_DIR and DOCINTEL_BACKUP_DIR must both be inside the persistent-disk mount.")
    hour = int(os.environ.get("DOCINTEL_BACKUP_UTC_HOUR", "20"))
    minute = int(os.environ.get("DOCINTEL_BACKUP_UTC_MINUTE", "0"))
    limit_gib = float(os.environ.get("DOCINTEL_BACKUP_LIMIT_GIB", "8.5"))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59 or not 0 < limit_gib <= 8.5:
        raise BackupError("Invalid backup schedule or allocation configuration.")
    return backup_dir, password, hour, minute, int(limit_gib * 1024**3)


def _parse_backup(path: Path) -> datetime | None:
    match = _NAME.match(path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def select_retained_backups(paths: list[Path]) -> set[Path]:
    """Keep 7 newest daily, 4 newest weekly representatives, 3 monthly."""
    dated = [(path, _parse_backup(path)) for path in paths]
    dated = [(path, stamp) for path, stamp in dated if stamp is not None]
    dated.sort(key=lambda item: item[1], reverse=True)
    keep = {path for path, _ in dated[:7]}

    weekly: dict[tuple[int, int], Path] = {}
    monthly: dict[tuple[int, int], Path] = {}
    for path, stamp in dated:
        iso = stamp.isocalendar()
        weekly.setdefault((iso.year, iso.week), path)
        monthly.setdefault((stamp.year, stamp.month), path)
    keep.update(list(weekly.values())[:4])
    keep.update(list(monthly.values())[:3])
    return keep


def _verify_encrypted(path: Path, password: str, scratch: Path) -> None:
    restored = scratch / f"verify-{path.stem}"
    decrypt_backup(path, restored, password)
    shutil.rmtree(restored)


def apply_retention(
    backup_dir: Path,
    password: str,
    scratch: Path,
    limit_bytes: int | None = None,
) -> list[Path]:
    """Verify retained backups before deleting expired copies; preserve unknown files."""
    all_encrypted = list(backup_dir.glob("*.dcbak"))
    backups = [path for path in all_encrypted if _parse_backup(path)]
    unknown = [path for path in all_encrypted if not _parse_backup(path)]

    desired = select_retained_backups(backups)
    if not desired:
        if limit_bytes is not None:
            unknown_bytes = sum(path.stat().st_size for path in unknown)
            if unknown_bytes > limit_bytes:
                raise BackupError(
                    "Unrecognized encrypted backups exceed the configured allocation; "
                    "they were preserved."
                )
        return []

    keep = set(desired)
    if limit_bytes is not None:
        unknown_bytes = sum(path.stat().st_size for path in unknown)
        available = limit_bytes - unknown_bytes
        newest = max(desired, key=lambda path: _parse_backup(path))

        if newest.stat().st_size > available:
            raise BackupError(
                "The newest encrypted backup cannot fit within the configured allocation."
            )

        keep = set()
        used = unknown_bytes
        for path in sorted(desired, key=lambda item: _parse_backup(item), reverse=True):
            size = path.stat().st_size
            if used + size <= limit_bytes:
                keep.add(path)
                used += size

        if newest not in keep:
            raise BackupError(
                "The newest encrypted backup cannot fit within the configured allocation."
            )

    # Verify every selected recovery point before deleting anything.
    for path in sorted(keep):
        _verify_encrypted(path, password, scratch)

    removed = []
    for path in backups:
        if path not in keep:
            size_bytes = path.stat().st_size
            path.unlink()
            removed.append(path)
            logger.info(
                "Expired encrypted backup removed",
                extra={
                    "event": "backup_retention_removed",
                    "backup": path.name,
                    "size_bytes": size_bytes,
                },
            )
    return removed


def create_scheduled_backup() -> Path:
    """Create, encrypt, round-trip verify, then apply retention."""
    backup_dir, password, _hour, _minute, limit_bytes = _backup_config()
    settings = get_settings()
    source = settings.data_dir.expanduser().absolute().resolve()
    _reject_symlink_components(backup_dir.absolute())
    backup_dir.mkdir(parents=True, exist_ok=True)
    root = backup_dir.resolve()
    if root == source or source in root.parents or root in source.parents:
        raise BackupError("Backup directory and live data directory must be separate siblings on the persistent disk.")
    work = root / ".work"
    _reject_symlink_components(work.absolute())
    work.mkdir(exist_ok=True)
    _reject_symlinks(work)
    # Clear only the private work directory; never follow symlinks.
    for stale in work.iterdir():
        if stale.is_dir():
            shutil.rmtree(stale)
        else:
            stale.unlink()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    plain = work / f"plain-{stamp}"
    encrypted_name = f"docintel-{stamp}.dcbak"
    staged_encrypted = work / encrypted_name
    encrypted = root / encrypted_name
    published = False
    fully_verified = False
    try:
        create_backup(plain, source)
        verify_backup(plain)
        # Stage and verify the candidate separately. This lets retention
        # make room in the live backup directory before quota is enforced there.
        encrypt_backup(plain, staged_encrypted, password, work, limit_bytes)
        _verify_encrypted(staged_encrypted, password, work)
        fully_verified = True

        # Publish atomically without overwriting an existing timestamp.
        os.link(staged_encrypted, encrypted)
        staged_encrypted.unlink()
        published = True
        removed = apply_retention(root, password, work, limit_bytes=limit_bytes)
        logger.info("Scheduled backup verified", extra={"event": "backup_success", "backup": encrypted.name, "retention_removed": len(removed)})
        return encrypted
    except Exception:
        if published and not fully_verified:
            encrypted.unlink(missing_ok=True)
        logger.exception("Scheduled backup cycle failed; inspect retention logs for recovery-point changes", extra={"event": "backup_failure"})
        raise
    finally:
        shutil.rmtree(plain, ignore_errors=True)
        # Remove interrupted decrypt/zip scratch files, but retain the directory.
        for item in work.iterdir():
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            elif item.is_file():
                item.unlink(missing_ok=True)


def _next_run(hour: int, minute: int, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


async def _finish_thread_call(function):
    """Do not abandon filesystem/worker operations when the scheduler is cancelled."""
    task = asyncio.create_task(asyncio.to_thread(function))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # The thread cannot be force-cancelled; wait for safe completion.
        # Preserve cancellation even if the background operation also fails.
        try:
            await task
        except Exception:
            logger.exception(
                "Background operation failed while waiting for cancellation",
                extra={"event": "backup_operation_cancelled"},
            )
        raise


async def _wait_for_worker_shutdown(worker) -> None:
    """Keep waiting until every worker thread has actually stopped."""
    attempts = 0
    while True:
        try:
            await _finish_thread_call(lambda: worker.stop(timeout=1))
            return
        except TimeoutError:
            attempts += 1
            if attempts == 1 or attempts % 30 == 0:
                logger.warning(
                    "Waiting for worker shutdown",
                    extra={"event": "backup_worker_shutdown_wait", "attempts": attempts},
                )
            await asyncio.sleep(1)


async def scheduler_loop() -> None:
    _backup_dir, _password, hour, minute, _limit = _backup_config()

    while True:
        target = _next_run(hour, minute)
        await asyncio.sleep(
            max(1.0, (target - datetime.now(timezone.utc)).total_seconds())
        )

        gate_paused = False
        worker_stop_started = False
        worker_stopped = False
        worker_restarted = True

        try:
            gate_paused = True
            await backup_gate.pause_and_drain()

            from app import worker
            worker_stop_started = True
            worker_restarted = False

            try:
                await _finish_thread_call(worker.stop)
                worker_stopped = True
            except TimeoutError:
                logger.warning(
                    "Worker shutdown timed out; backup skipped",
                    extra={"event": "backup_worker_shutdown_timeout"},
                )
                await _wait_for_worker_shutdown(worker)
                await _finish_thread_call(worker.start)
                worker_restarted = True
                worker_stop_started = False
                logger.info(
                    "Worker restarted after delayed shutdown; backup skipped",
                    extra={"event": "backup_worker_restarted"},
                )
                continue

            await _finish_thread_call(create_scheduled_backup)

        except asyncio.CancelledError:
            if worker_stop_started and not worker_restarted:
                try:
                    await _wait_for_worker_shutdown(worker)
                    await _finish_thread_call(worker.start)
                    worker_restarted = True
                except Exception:
                    logger.exception(
                        "Worker recovery failed during scheduler cancellation; "
                        "request gate will remain closed",
                        extra={"event": "backup_worker_restart_failure"},
                    )
            raise

        except Exception:
            logger.exception(
                "Scheduled backup cycle failed",
                extra={"event": "backup_cycle_failure"},
            )
            if worker_stop_started and not worker_restarted:
                try:
                    await _wait_for_worker_shutdown(worker)
                    await _finish_thread_call(worker.start)
                    worker_restarted = True
                except Exception:
                    logger.exception(
                        "Worker recovery failed; request gate will remain closed",
                        extra={"event": "backup_worker_restart_failure"},
                    )
                    raise

        finally:
            if worker_stopped and not worker_restarted:
                try:
                    from app import worker
                    await _finish_thread_call(worker.start)
                    worker_restarted = True
                except Exception:
                    logger.exception(
                        "Worker failed to restart after backup; "
                        "request gate will remain closed and scheduler will stop",
                        extra={"event": "backup_worker_restart_failure"},
                    )
                    raise

            if gate_paused and worker_restarted:
                await backup_gate.resume()

def backup_enabled() -> bool:
    return os.environ.get("DOCINTEL_BACKUP_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
