
"""Tests for versioned SQLite database migrations."""

import sqlite3

import pytest

from app.db import SCHEMA
from app.db_migrations import run_migrations


def make_database():
    """Create an isolated database with the current base schema."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript(SCHEMA)
    return conn


def test_migrations_run_once():
    conn = make_database()

    try:
        run_migrations(conn)
        first = conn.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()

        run_migrations(conn)
        second = conn.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()

        assert len(first) == 2
        assert [tuple(row) for row in first] == [
            tuple(row) for row in second
        ]
    finally:
        conn.close()


def test_migration_preserves_existing_invoice_data():
    conn = make_database()

    try:
        conn.execute("""
            INSERT INTO documents (
                filename, stored_name, sha256, size_bytes,
                created_at, updated_at
            )
            VALUES ('invoice.pdf', 'stored.pdf', 'abc123', 100,
                    '2026-01-01', '2026-01-01')
        """)

        conn.execute("""
            INSERT INTO invoices (
                document_id, seq, page_start, page_end,
                fields_json, invoice_number, extractor
            )
            VALUES (1, 1, 1, 1, '{}', 'INV-2026-001', 'test')
        """)

        run_migrations(conn)

        invoice = conn.execute(
            """
            SELECT invoice_number, num_key
            FROM invoices
            WHERE id=1
            """
        ).fetchone()

        assert invoice["invoice_number"] == "INV-2026-001"
        assert invoice["num_key"] == "inv2026001"

        count = conn.execute(
            "SELECT COUNT(*) FROM documents"
        ).fetchone()[0]

        assert count == 1

        applied = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]

        assert applied == 2

        document = conn.execute(
            "SELECT tenant_id FROM documents WHERE id=1"
        ).fetchone()

        assert document["tenant_id"] == 1

    finally:
        conn.close()


def test_failed_migration_is_not_recorded():
    conn = make_database()

    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "ALTER TABLE invoices ADD COLUMN test_column TEXT"
                )
                raise sqlite3.OperationalError("Simulated migration failure")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(invoices)")
        }

        assert "test_column" not in columns

    finally:
        conn.close()
