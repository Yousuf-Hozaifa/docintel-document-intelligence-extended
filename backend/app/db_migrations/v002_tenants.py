"""Version 002: introduce tenants and tenant-scoped API keys."""

import sqlite3

from ..db import _columns, now


LEGACY_TENANT_ID = 1


def upgrade(conn: sqlite3.Connection) -> None:
    """Create tenant records and assign all existing documents to legacy."""

    conn.execute("""
        CREATE TABLE IF NOT EXISTS tenants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    conn.execute(
        """
        INSERT OR IGNORE INTO tenants (id, slug, name, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            LEGACY_TENANT_ID,
            "legacy",
            "Legacy tenant",
            now(),
        ),
    )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS tenant_api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL
                REFERENCES tenants(id) ON DELETE CASCADE,
            key_hash TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            revoked_at TEXT
        )
    """)

    document_columns = _columns(conn, "documents")

    if "tenant_id" not in document_columns:
        conn.execute(
            """
            ALTER TABLE documents
            ADD COLUMN tenant_id INTEGER NOT NULL DEFAULT 1
                REFERENCES tenants(id)
            """
        )

    # Ensure existing records are explicitly assigned to the legacy tenant.
    conn.execute(
        """
        UPDATE documents
        SET tenant_id = ?
        WHERE tenant_id IS NULL
        """,
        (LEGACY_TENANT_ID,),
    )

    conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_documents_tenant
        ON documents(tenant_id)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_documents_tenant_sha
        ON documents(tenant_id, sha256)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_tenant_api_keys_tenant
        ON tenant_api_keys(tenant_id)
    """)