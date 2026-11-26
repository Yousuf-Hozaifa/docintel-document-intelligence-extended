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

For the offline CLI create/restore procedures in Sections 3–5, stop the backend and worker and prevent all writes. The Render scheduler in Section 8 performs its own request drain and worker shutdown before creating a backup.

The offline CLI utility does not itself provide scheduling or cloud uploads. An optional single-instance Render scheduler is documented in Section 8. Neither local encrypted backups nor a persistent disk alone provide off-site disaster recovery.

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

For production, configure a single Render web-service instance with a persistent disk and enable the scheduler described below. The scheduler writes encrypted copies to that same disk; it does not replace independent off-site backups. Define RPO/RTO, monitor backup-failure logs, and periodically perform a separate restore drill.

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



## 8. Automated backups on a single-instance Render service

The optional scheduler runs inside the same FastAPI web-service process as the SQLite database, uploads directory, and in-process worker. It must not be moved to a separate Render Cron Job or second service: those do not share the web service's persistent disk. Keep exactly one service instance and one Uvicorn worker.

### Render persistent-disk layout

Attach a paid Render persistent disk mounted at `/var/data`. Configure these private environment variables on the **backend web service**:

| Variable | Value |
|---|---|
| `DOCINTEL_DATA_DIR` | `/var/data/appdata` |
| `DOCINTEL_BACKUP_ENABLED` | `true` |
| `DOCINTEL_PERSISTENT_DISK_MOUNT` | `/var/data` |
| `DOCINTEL_BACKUP_DIR` | `/var/data/backups` |
| `DOCINTEL_BACKUP_PASSWORD` | A unique randomly generated password of at least 12 characters |
| `DOCINTEL_BACKUP_UTC_HOUR` | `20` |
| `DOCINTEL_BACKUP_UTC_MINUTE` | `0` |
| `DOCINTEL_BACKUP_LIMIT_GIB` | `8.5` |

At startup, the service verifies `/var/data` is an actual mounted filesystem and that both live data and backup directories are inside it; it refuses to enable scheduling if the disk is absent. The scheduled time is 20:00 UTC, which is 02:00 the following day in Bangladesh (UTC+6). The password is read from the Render environment and is never written to logs or backup metadata. Do not commit it or send it in chat. Keep a separate secure copy of the password: losing it makes the encrypted backups unrecoverable.

Provision enough disk capacity for the live database and uploads, retained encrypted backups (up to 8.5 GiB), and temporary plaintext/verification work during a backup. The scheduler uses `/var/data/backups/.work` for temporary files and removes them after each run.

### Backup cycle and safeguards

1. The request gate rejects new API requests with HTTP 503 and `Retry-After` while a backup is running; `/api/health` remains available. Active requests are drained before the snapshot begins.
2. The in-process worker is stopped and joined. If it cannot stop cleanly, backup creation is skipped.
3. The native SQLite backup API creates a consistent database snapshot, uploads are copied, and the manifest/checksums and SQLite integrity are verified.
4. The snapshot is encrypted with AES-256-GCM, then decrypted into a temporary verification directory and verified again.
5. Only after the new encrypted backup is verified does retention verify every backup selected to keep. If any retained backup fails verification, no expired backup is deleted.
6. The worker is restarted and the request gate is reopened, including after a backup failure.

Retention keeps the newest 7 daily backups, the newest representative from each of the 4 most recent ISO weeks, and the newest representative from each of the 3 most recent calendar months. Sets overlap, so the actual count is usually lower than 14. Unrecognized filenames are not automatically deleted. The 8.5 GiB allocation applies to `.dcbak` files in the backup directory. The candidate is created and verified in a private staging directory before retention is applied. Retention verifies the selected recovery points before deleting expired dated backups. If the newest backup cannot fit, or verification fails, no existing recovery point is deleted. The persistent disk needs temporary free space for the candidate and verification files.

Failures are written to the backend logs as `backup_failure` / `backup_cycle_failure` events. Configure Render log alerts or an external log monitor for these events. No email/SMS alerting is built in.

### Important limitations

- These encrypted backups are on the **same Render persistent disk** as the live data. Disk loss, account loss, or account compromise can affect both. This is not off-site backup.
- Google Drive upload/download automation is not implemented. Keep independent off-site copies and test their restore path separately.
- The scheduler is process-local and intentionally single-instance. Do not increase Render instances or Uvicorn workers while SQLite and this scheduler are in use.
- During the backup window, API requests other than health checks receive a temporary 503 and should be retried. If the service is unavailable at the scheduled time, the next attempt is at the next daily schedule; there is no catch-up run.
- A full disk, failed encryption password configuration, or failed retained-backup verification causes the cycle to fail closed; existing backups are preserved.

Before enabling this on real customer data, deploy with a synthetic dataset, confirm the first scheduled backup and logs, and complete a restore drill into a separate directory.
