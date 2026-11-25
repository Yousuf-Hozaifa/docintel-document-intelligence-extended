"""Password-based AES-256-GCM encryption for verified DocIntel backups.

The encrypted file format is versioned and authenticated. Passwords are
requested by the CLI and are never stored in the backup or command history.
"""
from __future__ import annotations

import argparse
import getpass
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from app.backup import BackupError, verify_backup

MAGIC = b"DCBKE001"
SALT_BYTES = 16
NONCE_BYTES = 12
TAG_BYTES = 16
HEADER_BYTES = len(MAGIC) + SALT_BYTES + NONCE_BYTES
CHUNK_BYTES = 1024 * 1024
DEFAULT_MAX_BYTES = int(8.5 * 1024 * 1024 * 1024)
ENCRYPTED_SUFFIX = ".dcbak"


def _derive_key(password: str, salt: bytes) -> bytes:
    if not isinstance(password, str) or len(password) < 12:
        raise BackupError("Use an encryption password of at least 12 characters.")
    return Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(
        password.encode("utf-8")
    )


def _reject_symlinks(root: Path) -> None:
    if root.is_symlink():
        raise BackupError(f"Symlinks are not supported: {root}")
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            item = Path(current) / name
            if item.is_symlink():
                raise BackupError(f"Symlinks are not supported: {item}")


def _directory_size(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise BackupError(f"Symlinks are not supported: {path}")
        if path.is_file():
            total += path.stat().st_size
    return total


def _used_quota(quota_dir: Path) -> int:
    total = 0
    for path in quota_dir.iterdir():
        if path.is_symlink():
            raise BackupError(f"Symlinks are not supported in quota folder: {path}")
        if path.is_file() and path.name.lower().endswith(ENCRYPTED_SUFFIX):
            total += path.stat().st_size
    return total


def _safe_zip_name(name: str) -> str:
    if not name or "\\" in name or "\x00" in name or ":" in name:
        raise BackupError(f"Unsafe encrypted archive path: {name!r}")
    parsed = PurePosixPath(name)
    if parsed.is_absolute() or any(part in ("", ".", "..") for part in parsed.parts):
        raise BackupError(f"Unsafe encrypted archive path: {name!r}")
    if parsed.as_posix() != name:
        raise BackupError(f"Unsafe encrypted archive path: {name!r}")
    return name


def encrypt_backup(
    backup: str | Path,
    destination: str | Path,
    password: str,
    quota_dir: str | Path,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Path:
    """Verify, ZIP and stream-encrypt a backup into a quota-controlled folder."""
    source_input = Path(backup).expanduser().absolute()
    if source_input.is_symlink():
        raise BackupError(f"Symlinks are not supported: {source_input}")
    source = source_input.resolve()
    target = Path(destination).expanduser().absolute()
    quota = Path(quota_dir).expanduser().absolute()
    if not source.is_dir():
        raise BackupError(f"Backup directory does not exist: {source}")
    verify_backup(source)
    _reject_symlinks(source)

    quota.mkdir(parents=True, exist_ok=True)
    if quota.is_symlink() or target.parent.resolve() != quota.resolve():
        raise BackupError("Encrypted backup destination must be directly inside the quota folder.")
    if target.suffix.lower() != ENCRYPTED_SUFFIX:
        raise BackupError(f"Encrypted backup filename must end with {ENCRYPTED_SUFFIX}.")
    if target.exists():
        raise BackupError(f"Encrypted backup already exists: {target}")
    if max_bytes <= 0:
        raise BackupError("Quota limit must be positive.")

    # Conservative preflight: source bytes are an upper bound for ordinary
    # ZIP_DEFLATED output, with final exact-size enforcement after encryption.
    used = _used_quota(quota)
    source_size = _directory_size(source)
    if used + source_size > max_bytes:
        raise BackupError(
            f"8.5 GB allocation would be exceeded: existing={used}, "
            f"backup_source={source_size}, limit={max_bytes} bytes."
        )

    fd, zip_name = tempfile.mkstemp(prefix=".docintel-", suffix=".zip", dir=quota)
    os.close(fd)
    zip_path = Path(zip_name)
    fd, encrypted_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=quota
    )
    os.close(fd)
    encrypted_temp = Path(encrypted_name)

    try:
        with zipfile.ZipFile(
            zip_path, "w", compression=zipfile.ZIP_DEFLATED,
            compresslevel=6, allowZip64=True
        ) as archive:
            for path in sorted(source.rglob("*")):
                if path.is_symlink():
                    raise BackupError(f"Symlinks are not supported: {path}")
                if path.is_dir():
                    archive.write(path, path.relative_to(source).as_posix() + "/")
                elif path.is_file():
                    archive.write(path, path.relative_to(source).as_posix())

        salt = os.urandom(SALT_BYTES)
        nonce = os.urandom(NONCE_BYTES)
        header = MAGIC + salt + nonce
        key = _derive_key(password, salt)
        encryptor = Cipher(
            algorithms.AES(key), modes.GCM(nonce)
        ).encryptor()
        encryptor.authenticate_additional_data(header)

        with zip_path.open("rb") as src, encrypted_temp.open("wb") as dst:
            dst.write(header)
            while True:
                chunk = src.read(CHUNK_BYTES)
                if not chunk:
                    break
                dst.write(encryptor.update(chunk))
            dst.write(encryptor.finalize())
            dst.write(encryptor.tag)
            dst.flush()
            os.fsync(dst.fileno())

        final_used = used + encrypted_temp.stat().st_size
        if final_used > max_bytes:
            raise BackupError(
                f"Encrypted backup would exceed allocation: "
                f"{final_used} > {max_bytes} bytes."
            )
        # Atomic publication that refuses to overwrite an existing file.
        # Both paths are in the same quota directory/filesystem.
        os.link(encrypted_temp, target)
        encrypted_temp.unlink()
        return target
    finally:
        zip_path.unlink(missing_ok=True)
        encrypted_temp.unlink(missing_ok=True)


def _extract_verified_zip(zip_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            for info in archive.infolist():
                name = _safe_zip_name(info.filename.rstrip("/"))
                if name in seen:
                    raise BackupError(f"Duplicate archive path: {name}")
                seen.add(name)
                unix_mode = info.external_attr >> 16
                if stat.S_ISLNK(unix_mode):
                    raise BackupError(f"Archive contains a symlink: {name}")
                output = destination.joinpath(*PurePosixPath(name).parts)
                if info.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                    continue
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as src, output.open("xb") as dst:
                    shutil.copyfileobj(src, dst, length=CHUNK_BYTES)
        verify_backup(destination)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def decrypt_backup(
    encrypted_file: str | Path,
    destination: str | Path,
    password: str,
) -> Path:
    """Authenticate/decrypt an encrypted backup and verify it before publishing."""
    source_input = Path(encrypted_file).expanduser().absolute()
    if source_input.is_symlink():
        raise BackupError(f"Symlinks are not supported: {source_input}")
    source = source_input.resolve()
    target = Path(destination).expanduser().absolute()
    if not source.is_file() or source.is_symlink():
        raise BackupError(f"Encrypted backup file not found or unsafe: {source}")
    if source.stat().st_size < HEADER_BYTES + TAG_BYTES:
        raise BackupError("Encrypted backup is truncated or invalid.")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise BackupError("Restore destination must be new or an empty directory.")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise BackupError("Restore destination cannot be a symlink.")

    staging_parent = Path(tempfile.mkdtemp(prefix=".docintel-decrypt-", dir=target.parent))
    zip_path = staging_parent / "decrypted.zip"
    extracted = staging_parent / "verified"
    try:
        file_size = source.stat().st_size
        with source.open("rb") as src:
            header = src.read(HEADER_BYTES)
            if len(header) != HEADER_BYTES or header[:len(MAGIC)] != MAGIC:
                raise BackupError("Unsupported or invalid encrypted backup format.")
            salt_start = len(MAGIC)
            salt = header[salt_start:salt_start + SALT_BYTES]
            nonce = header[salt_start + SALT_BYTES:]
            src.seek(file_size - TAG_BYTES)
            tag = src.read(TAG_BYTES)
            ciphertext_length = file_size - HEADER_BYTES - TAG_BYTES
            src.seek(HEADER_BYTES)

            key = _derive_key(password, salt)
            decryptor = Cipher(
                algorithms.AES(key), modes.GCM(nonce, tag)
            ).decryptor()
            decryptor.authenticate_additional_data(header)

            remaining = ciphertext_length
            with zip_path.open("wb") as dst:
                while remaining:
                    chunk = src.read(min(CHUNK_BYTES, remaining))
                    if not chunk:
                        raise BackupError("Encrypted backup is truncated.")
                    remaining -= len(chunk)
                    dst.write(decryptor.update(chunk))
                try:
                    dst.write(decryptor.finalize())
                except InvalidTag as exc:
                    raise BackupError(
                        "Authentication failed: incorrect password or damaged backup."
                    ) from exc

        _extract_verified_zip(zip_path, extracted)
        if target.exists():
            target.rmdir()
        extracted.rename(target)
        return target
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Encrypt/decrypt DocIntel backups")
    commands = parser.add_subparsers(dest="command", required=True)
    enc = commands.add_parser("encrypt")
    enc.add_argument("--backup", required=True)
    enc.add_argument("--destination", required=True)
    enc.add_argument("--quota-dir", required=True)
    enc.add_argument("--limit-gb", type=float, default=8.5)
    dec = commands.add_parser("decrypt")
    dec.add_argument("--encrypted", required=True)
    dec.add_argument("--destination", required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "encrypt":
            password = getpass.getpass("Encryption password (12+ characters): ")
            confirm = getpass.getpass("Confirm encryption password: ")
            if password != confirm:
                raise BackupError("Passwords do not match.")
            result = encrypt_backup(
                args.backup, args.destination, password, args.quota_dir,
                int(args.limit_gb * 1024 * 1024 * 1024)
            )
            print(f"Encrypted backup created: {result}")
        else:
            password = getpass.getpass("Encryption password: ")
            result = decrypt_backup(args.encrypted, args.destination, password)
            print(f"Decrypted backup verified and restored: {result}")
        return 0
    except (BackupError, OSError, ValueError, zipfile.BadZipFile) as exc:
        parser.exit(2, f"Backup encryption error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())

