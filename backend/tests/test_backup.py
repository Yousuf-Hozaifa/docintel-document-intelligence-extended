import json
import sqlite3
from pathlib import Path

import pytest

from app.backup import (
    BackupError,
    create_backup,
    restore_backup,
    verify_backup,
)


def make_data_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path / "docintel.sqlite3")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS sample (id INTEGER PRIMARY KEY, value TEXT)"
    )
    connection.execute(
        "INSERT INTO sample(value) VALUES ('preserve me')"
    )
    connection.commit()
    connection.close()

    (path / "uploads").mkdir(exist_ok=True)
    (path / "uploads" / "invoice.txt").write_text(
        "sample invoice",
        encoding="utf-8",
    )
    return path


def test_create_verify_and_restore_backup(tmp_path, monkeypatch):
    data_dir = make_data_dir(tmp_path / "data")
    monkeypatch.setenv("DOCINTEL_DATA_DIR", str(data_dir))

    backup = create_backup(tmp_path / "backup", data_dir)

    manifest = verify_backup(backup)
    assert manifest["format_version"] == 1
    assert (
        backup / "uploads" / "invoice.txt"
    ).read_text(encoding="utf-8") == "sample invoice"

    restored = restore_backup(backup, tmp_path / "restored")

    connection = sqlite3.connect(restored / "docintel.sqlite3")
    try:
        result = connection.execute(
            "SELECT value FROM sample"
        ).fetchone()
        assert result[0] == "preserve me"
    finally:
        connection.close()

    assert (
        restored / "uploads" / "invoice.txt"
    ).read_text(encoding="utf-8") == "sample invoice"


def test_backup_destination_must_be_outside_data_dir(
    tmp_path, monkeypatch
):
    data_dir = make_data_dir(tmp_path / "data")
    monkeypatch.setenv("DOCINTEL_DATA_DIR", str(data_dir))

    with pytest.raises(BackupError, match="separate"):
        create_backup(data_dir / "backup", data_dir)


def test_backup_refuses_existing_destination(tmp_path, monkeypatch):
    data_dir = make_data_dir(tmp_path / "data")
    monkeypatch.setenv("DOCINTEL_DATA_DIR", str(data_dir))

    destination = tmp_path / "backup"
    destination.mkdir()

    with pytest.raises(BackupError, match="already exists"):
        create_backup(destination, data_dir)


def test_verify_detects_modified_file(tmp_path, monkeypatch):
    data_dir = make_data_dir(tmp_path / "data")
    monkeypatch.setenv("DOCINTEL_DATA_DIR", str(data_dir))

    backup = create_backup(tmp_path / "backup", data_dir)
    (backup / "uploads" / "invoice.txt").write_text(
        "modified",
        encoding="utf-8",
    )

    with pytest.raises(BackupError, match="mismatch"):
        verify_backup(backup)


def test_verify_rejects_unlisted_file(tmp_path, monkeypatch):
    data_dir = make_data_dir(tmp_path / "data")
    monkeypatch.setenv("DOCINTEL_DATA_DIR", str(data_dir))

    backup = create_backup(tmp_path / "backup", data_dir)
    (backup / "extra.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(BackupError, match="unlisted"):
        verify_backup(backup)


def test_restore_refuses_nonempty_destination(tmp_path, monkeypatch):
    data_dir = make_data_dir(tmp_path / "data")
    monkeypatch.setenv("DOCINTEL_DATA_DIR", str(data_dir))

    backup = create_backup(tmp_path / "backup", data_dir)

    destination = tmp_path / "occupied"
    destination.mkdir()
    (destination / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(BackupError, match="empty directory"):
        restore_backup(backup, destination)

    assert (destination / "keep.txt").read_text(
        encoding="utf-8"
    ) == "keep"


def test_create_rejects_nested_symlink(tmp_path, monkeypatch):
    data_dir = make_data_dir(tmp_path / "data")
    monkeypatch.setenv("DOCINTEL_DATA_DIR", str(data_dir))

    external = tmp_path / "outside.txt"
    external.write_text("external", encoding="utf-8")

    link = data_dir / "uploads" / "linked.txt"

    try:
        link.symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is unavailable on this system.")

    with pytest.raises(BackupError, match="Symlinks"):
        create_backup(tmp_path / "backup", data_dir)


def test_backup_includes_committed_wal_data(tmp_path, monkeypatch):
    data_dir = make_data_dir(tmp_path / "data")
    monkeypatch.setenv("DOCINTEL_DATA_DIR", str(data_dir))

    database = data_dir / "docintel.sqlite3"
    connection = sqlite3.connect(database)

    try:
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        assert mode.lower() == "wal"

        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "INSERT INTO sample(value) VALUES ('committed WAL record')"
        )
        connection.commit()

        assert Path(str(database) + "-wal").exists()

        backup = create_backup(tmp_path / "wal-backup", data_dir)

        snapshot = sqlite3.connect(backup / "docintel.sqlite3")
        try:
            values = [
                row[0]
                for row in snapshot.execute(
                    "SELECT value FROM sample"
                ).fetchall()
            ]
            assert "committed WAL record" in values
        finally:
            snapshot.close()
    finally:
        connection.close()


def test_verify_rejects_manifest_path_traversal(tmp_path, monkeypatch):
    data_dir = make_data_dir(tmp_path / "data")
    monkeypatch.setenv("DOCINTEL_DATA_DIR", str(data_dir))

    backup = create_backup(tmp_path / "backup", data_dir)
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    manifest["files"].append({
        "path": "../outside.txt",
        "size": 0,
        "sha256": "0" * 64,
    })
    manifest_path.write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    with pytest.raises(BackupError, match="Unsafe manifest path"):
        verify_backup(backup)


def test_verify_rejects_duplicate_manifest_entries(tmp_path, monkeypatch):
    data_dir = make_data_dir(tmp_path / "data")
    monkeypatch.setenv("DOCINTEL_DATA_DIR", str(data_dir))

    backup = create_backup(tmp_path / "backup", data_dir)
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    manifest["files"].append(dict(manifest["files"][0]))
    manifest_path.write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    with pytest.raises(BackupError, match="Duplicate manifest path"):
        verify_backup(backup)
