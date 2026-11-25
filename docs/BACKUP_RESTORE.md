# DocIntel Backup and Restore Guide

## 1. Purpose

DocIntel stores application data in the directory configured by `DOCINTEL_DATA_DIR` (default: `./data`).

The data directory may contain:
- `docintel.sqlite3` — SQLite database and application records.
- `uploads/` — original uploaded documents.
- `models/` — optional semantic-search models.

The offline backup utility includes:
- A consistent SQLite database snapshot.
- The `uploads/` directory.
- A manifest containing file paths, sizes, and SHA-256 checksums.

Semantic models, environment variables, application configuration, and secrets are excluded. Models must be restored separately or downloaded again.

## 2. Operating Requirements

Stop the DocIntel backend and worker before creating or restoring backups. Do not allow uploads, processing jobs, or database writes during these operations.

The utility does not currently provide:
- Automatic or scheduled backups.
- Off-site backup storage.
- Automatic cloud-provider backup configuration.
- Cloud-provider backup configuration.
- Automated disaster recovery.

Backups may contain confidential uploaded documents and user data. Store them securely with restricted access.

## 3. Create a Backup

Run from the `backend` directory:

    python -m app.backup create --destination "F:\DocIntelBackups\backup-001"

To specify the source data directory:

    python -m app.backup create --data-dir ".\data" --destination "F:\DocIntelBackups\backup-001"

The destination must not already exist and must be separate from the source data directory.

## 4. Verify a Backup

    python -m app.backup verify --backup "F:\DocIntelBackups\backup-001"

Verification checks the manifest, expected files, file sizes, SHA-256 checksums, directory contents, and SQLite integrity.

Do not restore a backup that fails verification.

## 5. Restore a Backup

Restore requires a new or empty destination. The utility refuses to overwrite a non-empty destination.

    python -m app.backup restore --backup "F:\DocIntelBackups\backup-001" --destination "F:\DocIntelRestoreTest"

Configure `DOCINTEL_DATA_DIR` to point to the restored directory before starting DocIntel. Restore optional semantic models separately or download them again. Configure environment variables and secrets separately.

## 6. Recommended Operational Procedure

1. Stop the backend and worker.
2. Create a backup using a new destination.
3. Verify the backup.
4. Copy the verified backup to separate, access-controlled storage.
5. Record the backup date and storage location.
6. Restart the application.
7. Periodically restore a copy into a separate test directory and verify it.

## 7. Production Limitations

A local backup does not protect against loss of the host, account compromise, or storage failure.

Before production deployment, establish:
- Automated backup scheduling.
- Off-site backup retention.
- Appropriate encryption and access controls.
- Recovery-point objective (RPO) and recovery-time objective (RTO).
- Periodic restore testing.
- Monitoring and alerting for backup failures.

For a single-instance Render deployment using SQLite, configure `DOCINTEL_DATA_DIR` on a persistent disk. A persistent disk does not replace independent off-site backups.

This utility is an offline backup and restore mechanism, not a complete production disaster-recovery solution.


## Encrypted off-site copies

The optional `python -m app.backup_encryption` utility encrypts a verified offline
backup into a versioned AES-256-GCM container. A password-derived key is created
with scrypt. The password is prompted interactively and is not stored in the
backup or command-line arguments. Losing the password makes the encrypted copy
unrecoverable; keep it in a separate secure password manager or offline record.

Create an encrypted copy (the backup directory must already have been created
and verified with `python -m app.backup`):

```powershell
python -m app.backup_encryption encrypt --backup "F:\DocIntelBackups\plain-2026-11-26" --destination "F:\DocIntelBackups\drive-staging\docintel-2026-11-26.dcbak" --quota-dir "F:\DocIntelBackups\drive-staging" --limit-gb 8.5
```

Decrypt and verify into a new destination:

```powershell
python -m app.backup_encryption decrypt --encrypted "F:\DocIntelBackups\drive-staging\docintel-2026-11-26.dcbak" --destination "F:\DocIntelBackups\restore-test"
```

The quota check counts `.dcbak` files in the configured staging folder and
refuses an operation that would exceed 8.5 GiB. This is a local application
limit, not a Google Drive quota guarantee. Google Drive account usage must be
checked separately before manual upload. No cloud credentials or automatic
uploads are configured. Test with synthetic data before using real backups.

