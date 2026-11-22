
"""Offline backup and restore utilities for DocIntel.

Stop the backend and worker before creating or restoring backups.

Backups include:
- SQLite database snapshot
- Uploaded documents
- SHA-256 checksums and a JSON manifest

Optional semantic models and application configuration are not included.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from app.config import get_settings

DB_NAME = "docintel.sqlite3"
UPLOADS_NAME = "uploads"
MANIFEST_NAME = "manifest.json"
FORMAT_VERSION = 1


class BackupError(RuntimeError):
    """Raised when a backup operation cannot safely continue."""


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _reject_symlink_components(path: Path) -> None:
    """Reject symlinks in every existing component of a path."""
    path = path.absolute()

    current = Path(path.anchor)

    for part in path.parts[1:]:
        current = current / part

        if current.is_symlink():
            raise BackupError(f"Symlinks are not supported: {current}")


def _reject_symlinks(root: Path) -> None:
    """Reject symlinks anywhere inside a directory tree."""
    _reject_symlink_components(root)

    if not root.is_dir():
        raise BackupError(f"Directory does not exist: {root}")

    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            candidate = Path(current) / name

            if candidate.is_symlink():
                raise BackupError(
                    f"Symlinks are not supported: {candidate}"
                )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def _integrity_check(database: Path) -> None:
    if not database.is_file():
        raise BackupError(f"Database not found: {database}")

    try:
        connection = sqlite3.connect(
            database.resolve().as_uri() + "?mode=ro&immutable=1",
            uri=True,
        )
        try:
            result = connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise BackupError(
            f"SQLite integrity check failed: {exc}"
        ) from exc

    if not result or result[0] != "ok":
        raise BackupError(
            f"SQLite integrity check failed: {result!r}"
        )

def _inventory(root: Path) -> list[dict]:
    records = []

    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise BackupError(
                f"Symlinks are not supported: {path}"
            )

        if path.is_file() and path.name != MANIFEST_NAME:
            records.append({
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            })

    return records


def _validate_manifest_path(relative: object) -> str:
    """Validate a manifest path as a safe POSIX relative path."""
    if not isinstance(relative, str) or not relative:
        raise BackupError(
            f"Invalid manifest file path: {relative!r}"
        )

    if "\x00" in relative or "\\" in relative or ":" in relative:
        raise BackupError(
            f"Unsafe manifest path: {relative!r}"
        )

    parsed = PurePosixPath(relative)

    if (
        parsed.is_absolute()
        or any(part in ("", ".", "..") for part in parsed.parts)
        or parsed.as_posix() != relative
    ):
        raise BackupError(
            f"Unsafe manifest path: {relative!r}"
        )

    if (
        relative == MANIFEST_NAME
        or relative != DB_NAME
        and not relative.startswith(UPLOADS_NAME + "/")
    ):
        raise BackupError(
            f"Unexpected file location in manifest: {relative!r}"
        )

    return relative


def _safe_existing_path(value: str | Path, description: str) -> Path:
    """Resolve a path after rejecting symlink components."""
    raw = Path(value).expanduser().absolute()
    _reject_symlink_components(raw)
    resolved = raw.resolve()

    if not resolved.exists():
        raise BackupError(f"{description} does not exist: {resolved}")

    return resolved


def create_backup(
    destination: str | Path,
    data_dir: str | Path | None = None,
) -> Path:
    """Create a verified backup outside the live data directory."""
    settings = get_settings()

    source_raw = Path(
        data_dir or settings.data_dir
    ).expanduser().absolute()

    target_raw = Path(destination).expanduser().absolute()

    _reject_symlink_components(source_raw)
    _reject_symlink_components(target_raw)

    source = source_raw.resolve()
    target = target_raw.resolve()

    database = source / DB_NAME
    uploads = source / UPLOADS_NAME

    if not source.is_dir() or not database.is_file():
        raise BackupError(
            f"Data directory or database is missing: {source}"
        )

    if (
        _is_within(target, source)
        or _is_within(source, target)
    ):
        raise BackupError(
            "Backup destination must be separate from the data directory."
        )

    if target.exists():
        raise BackupError(
            f"Backup destination already exists: {target}"
        )

    _reject_symlinks(source)

    if uploads.exists() and not uploads.is_dir():
        raise BackupError("Uploads path must be a directory.")

    target.parent.mkdir(parents=True, exist_ok=True)

    # Recheck after creating the parent directory.
    _reject_symlink_components(target)

    staging = Path(tempfile.mkdtemp(
        prefix=f".{target.name}.tmp-",
        dir=target.parent,
    ))

    try:
        # SQLite backup API includes committed WAL data.
        source_connection = sqlite3.connect(database)
        target_connection = sqlite3.connect(
            staging / DB_NAME
        )

        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
            source_connection.close()

        _integrity_check(staging / DB_NAME)

        if uploads.exists():
            shutil.copytree(
                uploads,
                staging / UPLOADS_NAME,
            )
        else:
            (staging / UPLOADS_NAME).mkdir()

        manifest = {
            "format_version": FORMAT_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database": DB_NAME,
            "uploads_directory": UPLOADS_NAME,
            "files": _inventory(staging),
        }

        (staging / MANIFEST_NAME).write_text(
            json.dumps(
                manifest,
                indent=2,
                sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )

        staging.rename(target)
        return target

    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_backup(backup: str | Path) -> dict:
    """Verify backup structure, file hashes, and SQLite integrity."""
    root_raw = Path(backup).expanduser().absolute()
    _reject_symlink_components(root_raw)

    root = root_raw.resolve()

    if not root.is_dir():
        raise BackupError(
            "Backup directory does not exist."
        )

    _reject_symlinks(root)

    manifest_path = root / MANIFEST_NAME

    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackupError(
            f"Cannot read backup manifest: {exc}"
        ) from exc

    if not isinstance(manifest, dict):
        raise BackupError(
            "Invalid backup manifest structure."
        )

    if (
        manifest.get("format_version") != FORMAT_VERSION
        or manifest.get("database") != DB_NAME
        or manifest.get("uploads_directory") != UPLOADS_NAME
    ):
        raise BackupError(
            "Unsupported or invalid backup manifest."
        )

    records = manifest.get("files")

    if not isinstance(records, list):
        raise BackupError(
            "Invalid manifest file list."
        )

    expected = set()

    for record in records:
        if not isinstance(record, dict):
            raise BackupError(
                "Invalid manifest file record."
            )

        relative = _validate_manifest_path(
            record.get("path")
        )

        if relative in expected:
            raise BackupError(
                f"Duplicate manifest path: {relative!r}"
            )

        size = record.get("size")
        digest = record.get("sha256")

        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise BackupError(
                f"Invalid file size in manifest: {relative}"
            )

        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise BackupError(
                f"Invalid SHA-256 value in manifest: {relative}"
            )

        expected.add(relative)
        path = root.joinpath(*PurePosixPath(relative).parts)

        if path.is_symlink() or not path.is_file():
            raise BackupError(
                f"Missing or unsafe backup file: {relative}"
            )

        if path.stat().st_size != size:
            raise BackupError(
                f"File size mismatch: {relative}"
            )

        if _sha256(path) != digest:
            raise BackupError(
                f"SHA-256 mismatch: {relative}"
            )

    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != MANIFEST_NAME
    }

    if actual != expected:
        raise BackupError(
            "Backup contains unlisted or missing files."
        )

    if DB_NAME not in expected:
        raise BackupError(
            "SQLite database is missing from backup."
        )

    if not (root / UPLOADS_NAME).is_dir():
        raise BackupError(
            "Uploads directory is missing."
        )

    _integrity_check(root / DB_NAME)

    return manifest


def restore_backup(
    backup: str | Path,
    destination: str | Path,
) -> Path:
    """Restore into a new or empty destination without overwriting data."""
    source = _safe_existing_path(
        backup,
        "Backup directory",
    )

    verify_backup(source)

    target_raw = Path(destination).expanduser().absolute()
    _reject_symlink_components(target_raw)

    target = target_raw.resolve()

    if (
        _is_within(target, source)
        or _is_within(source, target)
    ):
        raise BackupError(
            "Restore destination must be separate from backup."
        )

    if target.exists() and (
        not target.is_dir() or any(target.iterdir())
    ):
        raise BackupError(
            "Restore destination must be new or an empty directory."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(target)

    staging = Path(tempfile.mkdtemp(
        prefix=f".{target.name}.restore-",
        dir=target.parent,
    ))

    try:
        shutil.copy2(
            source / DB_NAME,
            staging / DB_NAME,
        )

        shutil.copytree(
            source / UPLOADS_NAME,
            staging / UPLOADS_NAME,
        )

        _integrity_check(staging / DB_NAME)

        if target.exists():
            target.rmdir()

        staging.rename(target)
        return target

    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline DocIntel backup utility"
    )

    commands = parser.add_subparsers(
        dest="command",
        required=True,
    )

    create = commands.add_parser("create")
    create.add_argument("--destination", required=True)
    create.add_argument("--data-dir")

    verify = commands.add_parser("verify")
    verify.add_argument("--backup", required=True)

    restore = commands.add_parser("restore")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--destination", required=True)

    args = parser.parse_args(argv)

    try:
        if args.command == "create":
            result = create_backup(
                args.destination,
                args.data_dir,
            )
            print(f"Backup created: {result}")

        elif args.command == "verify":
            result = verify_backup(args.backup)
            print(
                f"Backup verified: {len(result['files'])} files"
            )

        else:
            result = restore_backup(
                args.backup,
                args.destination,
            )
            print(f"Backup restored: {result}")

        return 0

    except (BackupError, OSError, sqlite3.Error, shutil.Error) as exc:
        print(
            f"Backup error: {exc}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
