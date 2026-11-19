
"""Version 001: upgrade legacy databases to generation-aware schema.

This migration preserves existing document and invoice data.
"""
import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    """Apply the legacy schema upgrades exactly once."""

    # Reuse the existing, tested generation upgrade logic.
    # The import is deferred to avoid a module-import cycle.
    from ..db import _migrate_generations, _columns

    _migrate_generations(conn)

    # Add invoice number normalization key where needed.
    invoice_columns = _columns(conn, "invoices")

    if "num_key" not in invoice_columns:
        conn.execute("ALTER TABLE invoices ADD COLUMN num_key TEXT")

    # Preserve existing keys; backfill only missing values.
    rows = conn.execute(
        """
        SELECT id, invoice_number
        FROM invoices
        WHERE num_key IS NULL
        """
    ).fetchall()

    for row in rows:
        key = "".join(
            ch for ch in (row["invoice_number"] or "").lower()
            if ch.isalnum()
        ) or None

        conn.execute(
            "UPDATE invoices SET num_key=? WHERE id=?",
            (key, row["id"]),
        )

    # Add OCR confidence to older line-item tables.
    line_item_columns = _columns(conn, "line_items")

    if "ocr_conf" not in line_item_columns:
        conn.execute(
            "ALTER TABLE line_items ADD COLUMN ocr_conf REAL"
        )

    # Create the lookup index after ensuring num_key exists.
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_invoices_numkey
        ON invoices(num_key)
        """
    )
