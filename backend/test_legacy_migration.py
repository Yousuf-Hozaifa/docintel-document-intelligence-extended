
import os
import sqlite3
import tempfile
from pathlib import Path


def main():
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir) / "legacy_data"
        data_dir.mkdir()

        db_path = data_dir / "docintel.sqlite3"

        # Build a database using the original, pre-generation schema.
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")

        conn.executescript("""
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                file_type TEXT,
                size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                error_code TEXT,
                error_message TEXT,
                page_count INTEGER,
                pages_failed INTEGER NOT NULL DEFAULT 0,
                review_required INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL
                    REFERENCES documents(id) ON DELETE CASCADE,
                page_no INTEGER NOT NULL,
                label TEXT,
                method TEXT,
                ocr_confidence REAL,
                text TEXT,
                error TEXT,
                UNIQUE(document_id, page_no)
            );

            CREATE VIRTUAL TABLE page_fts USING fts5(
                text,
                document_id UNINDEXED,
                page_no UNINDEXED,
                tokenize='porter unicode61'
            );

            CREATE TABLE invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL
                    REFERENCES documents(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL,
                page_start INTEGER NOT NULL,
                page_end INTEGER NOT NULL,
                fields_json TEXT NOT NULL,
                invoice_number TEXT,
                invoice_date TEXT,
                supplier TEXT,
                customer TEXT,
                subtotal TEXT,
                tax TEXT,
                total TEXT,
                currency TEXT,
                dup_key TEXT,
                num_key TEXT,
                needs_review INTEGER NOT NULL DEFAULT 0,
                extractor TEXT NOT NULL
            );

            CREATE TABLE line_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER NOT NULL
                    REFERENCES invoices(id) ON DELETE CASCADE,
                idx INTEGER NOT NULL,
                description TEXT,
                quantity TEXT,
                unit_price TEXT,
                amount TEXT,
                evidence TEXT,
                ocr_conf REAL
            );

            CREATE TABLE validation_issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER NOT NULL
                    REFERENCES invoices(id) ON DELETE CASCADE,
                rule_id TEXT NOT NULL,
                severity TEXT NOT NULL,
                field TEXT,
                message TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER NOT NULL
                    REFERENCES invoices(id) ON DELETE CASCADE,
                field TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                note TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL
                    REFERENCES documents(id) ON DELETE CASCADE,
                page_no INTEGER NOT NULL,
                chunk_idx INTEGER NOT NULL,
                text TEXT NOT NULL,
                model TEXT NOT NULL,
                dim INTEGER NOT NULL,
                embedding BLOB NOT NULL
            );

            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL
                    REFERENCES documents(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL,
                run_after REAL NOT NULL DEFAULT 0,
                progress_done INTEGER NOT NULL DEFAULT 0,
                progress_total INTEGER,
                error TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            );

            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL
                    REFERENCES documents(id) ON DELETE CASCADE,
                ts TEXT NOT NULL,
                event TEXT NOT NULL,
                detail TEXT
            );
        """)

        conn.execute("""
            INSERT INTO documents (
                id, filename, stored_name, sha256, file_type,
                size_bytes, status, page_count, created_at, updated_at
            ) VALUES (1, 'legacy.pdf', 'legacy.pdf', 'abc123',
                      'pdf', 100, 'completed', 1, '2026-01-01', '2026-01-01')
        """)

        conn.execute("""
            INSERT INTO pages (
                id, document_id, page_no, text
            ) VALUES (10, 1, 1, 'Kestrel legacy migration test')
        """)

        conn.execute("""
            INSERT INTO page_fts(text, document_id, page_no)
            VALUES ('Kestrel legacy migration test', 1, 1)
        """)

        conn.execute("""
            INSERT INTO invoices (
                id, document_id, seq, page_start, page_end,
                fields_json, invoice_number, supplier, total, extractor
            ) VALUES (
                20, 1, 1, 1, 1, '{}', 'INV-100',
                'Kestrel Ltd', '100.00', 'test'
            )
        """)

        conn.execute("""
            INSERT INTO line_items (
                id, invoice_id, idx, description, amount
            ) VALUES (30, 20, 1, 'Legacy item', '100.00')
        """)

        conn.execute("""
            INSERT INTO validation_issues (
                id, invoice_id, rule_id, severity, message
            ) VALUES (40, 20, 'TEST-001', 'warning', 'Legacy issue')
        """)

        conn.execute("""
            INSERT INTO corrections (
                id, invoice_id, field, old_value, new_value,
                note, created_at
            ) VALUES (
                50, 20, 'total', '90.00', '100.00',
                'Legacy correction', '2026-01-01'
            )
        """)

        conn.execute("""
            INSERT INTO chunks (
                id, document_id, page_no, chunk_idx,
                text, model, dim, embedding
            ) VALUES (60, 1, 1, 0, 'Legacy text', 'test-model',
                      1, X'00000000')
        """)

        conn.commit()
        conn.close()

        # Point DocIntel at this temporary legacy database.
        os.environ["DOCINTEL_DATA_DIR"] = str(data_dir)

        from app.db import init_db

        init_db()
        # Verify migration is idempotent.
        init_db()
        init_db()
        print("PASS: Repeated database initialization")

        # Verify the migrated database.
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        checks = {
            "document preserved": conn.execute(
                "SELECT COUNT(*) FROM documents WHERE id=1"
            ).fetchone()[0] == 1,

            "page preserved": conn.execute(
                "SELECT COUNT(*) FROM pages WHERE id=10 AND generation_id=1"
            ).fetchone()[0] == 1,

            "invoice preserved": conn.execute(
                "SELECT COUNT(*) FROM invoices WHERE id=20 AND generation_id=1"
            ).fetchone()[0] == 1,

            "line item preserved": conn.execute(
                "SELECT COUNT(*) FROM line_items WHERE id=30"
            ).fetchone()[0] == 1,

            "validation issue preserved": conn.execute(
                "SELECT COUNT(*) FROM validation_issues WHERE id=40"
            ).fetchone()[0] == 1,

            "correction preserved": conn.execute(
                "SELECT COUNT(*) FROM corrections WHERE id=50"
            ).fetchone()[0] == 1,

            "semantic chunk preserved": conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE id=60 AND generation_id=1"
            ).fetchone()[0] == 1,

            "invoice number key migrated": conn.execute(
                "SELECT num_key FROM invoices WHERE id=20"
            ).fetchone()[0] == "inv100",

            "FTS search works": conn.execute(
                "SELECT COUNT(*) FROM page_fts WHERE page_fts MATCH 'Kestrel'"
            ).fetchone()[0] == 1,

            "foreign keys valid": len(
                conn.execute("PRAGMA foreign_key_check").fetchall()
            ) == 0,
        }

        for label, passed in checks.items():
            print(f"{'PASS' if passed else 'FAIL'}: {label}")

        conn.close()

        if not all(checks.values()):
            raise SystemExit("Legacy migration test FAILED")

        print("\nLegacy migration test PASSED.")


if __name__ == "__main__":
    main()
