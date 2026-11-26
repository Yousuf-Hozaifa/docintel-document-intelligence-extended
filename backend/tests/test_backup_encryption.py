import sqlite3
from pathlib import Path

import pytest

from app.backup import create_backup
from app.backup_encryption import (
    DEFAULT_MAX_BYTES,
    decrypt_backup,
    encrypt_backup,
)
from app.backup import BackupError


def make_data_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path / "docintel.sqlite3")
    connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO sample(value) VALUES ('encrypted round trip')")
    connection.commit()
    connection.close()
    (path / "uploads").mkdir(exist_ok=True)
    (path / "uploads" / "sample.txt").write_text("synthetic upload", encoding="utf-8")
    return path


def test_encrypt_decrypt_round_trip(tmp_path):
    data = make_data_dir(tmp_path / "data")
    backup = create_backup(tmp_path / "plain-backup", data)
    quota = tmp_path / "drive-staging"
    encrypted = encrypt_backup(
        backup, quota / "synthetic.dcbak", "test-password-long-enough",
        quota, DEFAULT_MAX_BYTES
    )
    assert encrypted.exists()
    assert b"encrypted round trip" not in encrypted.read_bytes()

    restored = decrypt_backup(
        encrypted, tmp_path / "restored", "test-password-long-enough"
    )
    connection = sqlite3.connect(restored / "docintel.sqlite3")
    try:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == (
            "encrypted round trip"
        )
    finally:
        connection.close()
    assert (restored / "uploads" / "sample.txt").read_text(
        encoding="utf-8"
    ) == "synthetic upload"


def test_wrong_password_does_not_restore(tmp_path):
    data = make_data_dir(tmp_path / "data")
    backup = create_backup(tmp_path / "plain-backup", data)
    quota = tmp_path / "quota"
    encrypted = encrypt_backup(
        backup, quota / "backup.dcbak", "test-password-long-enough", quota
    )
    with pytest.raises(BackupError, match="Authentication failed"):
        decrypt_backup(encrypted, tmp_path / "wrong-restore", "incorrect-password")
    assert not (tmp_path / "wrong-restore").exists()


def test_quota_rejects_backup_before_writing(tmp_path):
    data = make_data_dir(tmp_path / "data")
    backup = create_backup(tmp_path / "plain-backup", data)
    quota = tmp_path / "quota"
    quota.mkdir()
    (quota / "existing.dcbak").write_bytes(b"x" * 100)
    with pytest.raises(BackupError, match="allocation would be exceeded"):
        encrypt_backup(
            backup, quota / "new.dcbak", "test-password-long-enough",
            quota, max_bytes=100
        )
    assert not (quota / "new.dcbak").exists()


def test_encrypt_requires_long_password(tmp_path):
    data = make_data_dir(tmp_path / "data")
    backup = create_backup(tmp_path / "plain-backup", data)
    quota = tmp_path / "quota"
    with pytest.raises(BackupError, match="at least 12"):
        encrypt_backup(backup, quota / "backup.dcbak", "short", quota)


def test_encrypt_refuses_overwrite(tmp_path):
    data = make_data_dir(tmp_path / "data")
    backup = create_backup(tmp_path / "plain-backup", data)
    quota = tmp_path / "quota"
    quota.mkdir()
    target = quota / "backup.dcbak"
    target.write_bytes(b"keep")
    with pytest.raises(BackupError, match="already exists"):
        encrypt_backup(backup, target, "test-password-long-enough", quota)
    assert target.read_bytes() == b"keep"


def test_encrypt_decrypt_with_empty_uploads_directory(tmp_path):
    data = make_data_dir(tmp_path / "data")
    (data / "uploads" / "sample.txt").unlink()

    backup = create_backup(tmp_path / "plain-backup", data)
    quota = tmp_path / "drive-staging"

    encrypted = encrypt_backup(
        backup,
        quota / "empty-uploads.dcbak",
        "test-password-long-enough",
        quota,
    )

    restored = decrypt_backup(
        encrypted,
        tmp_path / "restored-empty",
        "test-password-long-enough",
    )

    uploads = restored / "uploads"
    assert uploads.is_dir()
    assert list(uploads.iterdir()) == []
