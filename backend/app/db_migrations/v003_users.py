"""Version 003: user accounts and revocable bearer sessions.

Each account owns one tenant. Existing legacy tenant data and API keys remain
unchanged and are not reassigned to a newly registered user.
"""
import sqlite3
from ..db import now

def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL UNIQUE REFERENCES tenants(id) ON DELETE CASCADE,
            email TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user','admin')),
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS auth_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_auth_sessions_user ON auth_sessions(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_auth_sessions_expiry ON auth_sessions(expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_users_role_active ON users(role, is_active)")
