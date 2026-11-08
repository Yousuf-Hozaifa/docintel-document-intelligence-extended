
import os
import sqlite3
import tempfile
from pathlib import Path


def main():
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir) / "data"
        data_dir.mkdir()

        db_path = data_dir / "docintel.sqlite3"

        os.environ["DOCINTEL_DATA_DIR"] = str(data_dir)

        from app.db import init_db

        init_db()

        conn = sqlite3.connect(db_path)

        conn.execute("""
            INSERT INTO documents (
                filename, stored_name, sha256, file_type,
                size_bytes, status, active_generation,
                next_generation, created_at, updated_at
            ) VALUES (
                'fts-test.pdf', 'fts-test.pdf', 'fts-test-hash',
                'pdf', 100, 'completed', 2, 3,
                '2026-01-01', '2026-01-01'
            )
        """)

        document_id = conn.execute(
            "SELECT id FROM documents"
        ).fetchone()[0]

        conn.executemany(
            """
            INSERT INTO pages (
                document_id, generation_id, page_no, text
            ) VALUES (?, ?, ?, ?)
            """,
            [
                (
                    document_id,
                    1,
                    1,
                    "Legacy generation one text",
                ),
                (
                    document_id,
                    2,
                    1,
                    "Kestrel generation two recovery test",
                ),
            ],
        )

        conn.commit()

        # Simulate a missing FTS index.
        conn.execute("DROP TABLE page_fts")
        conn.commit()
        conn.close()

        # Reinitialize and rebuild the missing index.
        init_db()

        conn = sqlite3.connect(db_path)

        generation_one = conn.execute(
            """
            SELECT COUNT(*)
            FROM page_fts
            WHERE page_fts MATCH 'Legacy'
              AND document_id = ?
              AND generation_id = 1
            """,
            (document_id,),
        ).fetchone()[0]

        generation_two = conn.execute(
            """
            SELECT COUNT(*)
            FROM page_fts
            WHERE page_fts MATCH 'Kestrel'
              AND document_id = ?
              AND generation_id = 2
            """,
            (document_id,),
        ).fetchone()[0]

        total_fts = conn.execute(
            "SELECT COUNT(*) FROM page_fts"
        ).fetchone()[0]

        print(
            f"{'PASS' if generation_one == 1 else 'FAIL'}: "
            "Generation 1 searchable"
        )

        print(
            f"{'PASS' if generation_two == 1 else 'FAIL'}: "
            "Generation 2 searchable with correct generation ID"
        )

        print(
            f"{'PASS' if total_fts == 2 else 'FAIL'}: "
            "Exactly two page records indexed"
        )

        conn.close()

        if (
            generation_one != 1
            or generation_two != 1
            or total_fts != 2
        ):
            raise SystemExit("Missing FTS recovery test FAILED")

        print("\nMissing FTS recovery test PASSED.")


if __name__ == "__main__":
    main()
