# User accounts and administrator bootstrap

This branch adds email/password accounts on top of the existing tenant isolation model.

## Account behavior

- Public registration creates a regular user and a dedicated tenant.
- Login returns a random, revocable bearer session token (12-hour lifetime). Only the token hash is stored.
- The browser keeps the token in session storage and sends it as an Authorization: Bearer header.
- Every existing document API continues to scope reads and writes to the authenticated tenant.
- The administrator role is never selectable during public registration.
- Administrators can view account/usage summaries and disable or re-enable regular accounts. Disabling an account revokes its active sessions.
- Existing API-key authentication remains for compatibility.

## Bootstrap the first administrator

Before deploying this branch to a public service, configure these private backend environment variables in Render:

- DOCINTEL_ADMIN_BOOTSTRAP_EMAIL: the administrator's email address.
- DOCINTEL_ADMIN_BOOTSTRAP_PASSWORD: a unique password of at least 12 characters.

Set both values before the first deployment that enables registration. On startup, the backend creates the administrator only if that email is not already registered. It reuses the existing `legacy` tenant so the current owner's documents remain available to the administrator. It never promotes an existing regular account. If the email already belongs to a regular user, startup fails safely; choose a different admin email or resolve the account deliberately before deployment.

Keep the bootstrap password private. Do not put it in GitHub, frontend environment variables, README examples, or chat. After the admin account is created, the variables can be removed from Render; the account remains in the database. If the variables are retained, bootstrap is idempotent and does not reset the existing password.

## Current deployment limitations and production requirements

The current Render demo uses SQLite and local upload storage. The repository documents that this filesystem is ephemeral on the free tier; uploaded files and account data can be lost after restart, redeploy, or instance replacement. This implementation is for a controlled demonstration until persistent storage and backup/restore are configured. Do not invite real customers to upload confidential or business-critical documents yet.

Before production use, add persistent storage (or move relational data to a managed database and uploads to private object storage), verified backups, registration/login rate limiting, email verification and password reset, monitoring, and an appropriate retention/deletion policy. Restrict upload size and processing concurrency to protect the shared demo instance.

## API

- `POST /api/auth/register`
- `POST /api/auth/login`
- `GET /api/auth/me`
- `POST /api/auth/logout`
- `GET /api/admin/overview` (admin only)
- `GET /api/admin/users` (admin only)
- `PATCH /api/admin/users/{user_id}` (admin only; body: `{"is_active": false}`)

Passwords must be at least 12 characters. Sessions expire after 12 hours and can be revoked by logout or account deactivation.
