
"""SQLite storage (stdlib sqlite3, WAL mode). One short-lived connection per unit of work."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import get_settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
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
  active_generation INTEGER NOT NULL DEFAULT 1,
  processing_generation INTEGER,
  next_generation INTEGER NOT NULL DEFAULT 2,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_documents_sha ON documents(sha256);
CREATE INDEX IF NOT EXISTS ix_documents_status ON documents(status);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
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
CREATE INDEX IF NOT EXISTS ix_jobs_claim ON jobs(status, run_after);

CREATE TABLE IF NOT EXISTS pages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  generation_id INTEGER NOT NULL DEFAULT 1,
  page_no INTEGER NOT NULL,
  label TEXT,
  method TEXT,
  ocr_confidence REAL,
  text TEXT,
  error TEXT,
  UNIQUE(document_id, generation_id, page_no)
);


CREATE VIRTUAL TABLE IF NOT EXISTS page_fts USING fts5(
  text,
  document_id UNINDEXED,
  page_no UNINDEXED,
  generation_id UNINDEXED,
  tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  generation_id INTEGER NOT NULL DEFAULT 1,
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
CREATE INDEX IF NOT EXISTS ix_invoices_doc ON invoices(document_id);

CREATE INDEX IF NOT EXISTS ix_invoices_dup ON invoices(dup_key);


CREATE TABLE IF NOT EXISTS line_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL,
  description TEXT,
  quantity TEXT,
  unit_price TEXT,
  amount TEXT,
  evidence TEXT,
  ocr_conf REAL
);

CREATE TABLE IF NOT EXISTS validation_issues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  rule_id TEXT NOT NULL,
  severity TEXT NOT NULL,
  field TEXT,
  message TEXT NOT NULL,
  resolved INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS corrections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  field TEXT NOT NULL,
  old_value TEXT,
  new_value TEXT,
  note TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  generation_id INTEGER NOT NULL DEFAULT 1,
  page_no INTEGER NOT NULL,
  chunk_idx INTEGER NOT NULL,
  text TEXT NOT NULL,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  embedding BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chunks_doc ON chunks(document_id);

CREATE INDEX IF NOT EXISTS ix_chunks_model ON chunks(model);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ts TEXT NOT NULL,
  event TEXT NOT NULL,
  detail TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _open() -> sqlite3.Connection:
    s = get_settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(s.db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def connect():
    """Transactional connection: commits on success, rolls back on error."""
    conn = _open()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}



def _migrate_generations(conn: sqlite3.Connection) -> None:
    """Add generation tracking while preserving all existing result records."""

    document_columns = _columns(conn, "documents")

    if "active_generation" not in document_columns:
        conn.execute(
            "ALTER TABLE documents ADD COLUMN active_generation "
            "INTEGER NOT NULL DEFAULT 1"
        )

    if "processing_generation" not in document_columns:
        conn.execute(
            "ALTER TABLE documents ADD COLUMN processing_generation INTEGER"
        )

    if "next_generation" not in document_columns:
        conn.execute(
            "ALTER TABLE documents ADD COLUMN next_generation "
            "INTEGER NOT NULL DEFAULT 2"
        )

    # Rebuild pages to replace the old uniqueness constraint:
    # UNIQUE(document_id, page_no)
    # with:
    # UNIQUE(document_id, generation_id, page_no)
    page_columns = _columns(conn, "pages")

    if "generation_id" not in page_columns:
        conn.execute("""
            CREATE TABLE pages_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL
                    REFERENCES documents(id) ON DELETE CASCADE,
                generation_id INTEGER NOT NULL DEFAULT 1,
                page_no INTEGER NOT NULL,
                label TEXT,
                method TEXT,
                ocr_confidence REAL,
                text TEXT,
                error TEXT,
                UNIQUE(document_id, generation_id, page_no)
            )
        """)

        conn.execute("""
            INSERT INTO pages_new (
                id, document_id, generation_id, page_no,
                label, method, ocr_confidence, text, error
            )
            SELECT
                id, document_id, 1, page_no,
                label, method, ocr_confidence, text, error
            FROM pages
        """)

        conn.execute("DROP TABLE pages")
        conn.execute("ALTER TABLE pages_new RENAME TO pages")

    # Add generation tracking to existing invoices.
    invoice_columns = _columns(conn, "invoices")

    if "generation_id" not in invoice_columns:
        conn.execute(
            "ALTER TABLE invoices ADD COLUMN generation_id "
            "INTEGER NOT NULL DEFAULT 1"
        )

    # Add generation tracking to existing semantic chunks.
    chunk_columns = _columns(conn, "chunks")

    if "generation_id" not in chunk_columns:
        conn.execute(
            "ALTER TABLE chunks ADD COLUMN generation_id "
            "INTEGER NOT NULL DEFAULT 1"
        )


    # Rebuild FTS when its schema is old or its records do not match pages.
    # This also recovers a missing FTS table recreated by SCHEMA.
    fts_columns = _columns(conn, "page_fts")
    fts_mismatch = True

    if "generation_id" in fts_columns:
        expected_fts = conn.execute(
            """
            SELECT COUNT(*)
            FROM pages
            WHERE text IS NOT NULL AND text != ''
            """
        ).fetchone()[0]

        actual_fts = conn.execute(
            "SELECT COUNT(*) FROM page_fts"
        ).fetchone()[0]

        missing_fts = conn.execute(
            """
            SELECT COUNT(*)
            FROM pages p
            WHERE p.text IS NOT NULL
              AND p.text != ''
              AND NOT EXISTS (
                  SELECT 1
                  FROM page_fts f
                  WHERE f.document_id = p.document_id
                    AND f.page_no = p.page_no
                    AND f.generation_id = p.generation_id
                    AND f.text = p.text
              )
            """
        ).fetchone()[0]

        unexpected_fts = conn.execute(
            """
            SELECT COUNT(*)
            FROM page_fts f
            WHERE NOT EXISTS (
                SELECT 1
                FROM pages p
                WHERE p.document_id = f.document_id
                  AND p.page_no = f.page_no
                  AND p.generation_id = f.generation_id
                  AND p.text = f.text
                  AND p.text IS NOT NULL
                  AND p.text != ''
            )
            """
        ).fetchone()[0]

        fts_mismatch = (
            actual_fts != expected_fts
            or missing_fts > 0
            or unexpected_fts > 0
        )

    if "generation_id" not in fts_columns or fts_mismatch:
        conn.execute("DROP TABLE page_fts")

        conn.execute("""
            CREATE VIRTUAL TABLE page_fts USING fts5(
                text,
                document_id UNINDEXED,
                page_no UNINDEXED,
                generation_id UNINDEXED,
                tokenize='porter unicode61'
            )
        """)

        conn.execute("""
            INSERT INTO page_fts (
                text, document_id, page_no, generation_id
            )
            SELECT
                text, document_id, page_no, generation_id
            FROM pages
            WHERE text IS NOT NULL AND text != ''
        """)

    # Create generation-aware indexes.
    conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_pages_doc_generation
        ON pages(document_id, generation_id)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_invoices_doc_generation
        ON invoices(document_id, generation_id)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_chunks_doc_generation
        ON chunks(document_id, generation_id)
    """)



def init_db() -> None:
    s = get_settings()
    s.upload_dir.mkdir(parents=True, exist_ok=True)
    conn = _open()

    try:
        # Create any missing tables first.
        # Existing tables and their records remain intact.
        conn.executescript(SCHEMA)

        # Disable foreign-key enforcement during the pages table rebuild.
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")

        try:
            # Add generation tracking to legacy databases.
            _migrate_generations(conn)

            # Add num_key if it does not exist.
            invoice_columns = _columns(conn, "invoices")

            if "num_key" not in invoice_columns:
                conn.execute(
                    "ALTER TABLE invoices ADD COLUMN num_key TEXT"
                )

            # Backfill missing invoice number keys.
            # Existing non-null keys are preserved.
            for row in conn.execute(
                """
                SELECT id, invoice_number
                FROM invoices
                WHERE num_key IS NULL
                """
            ).fetchall():
                key = "".join(
                    ch for ch in (row["invoice_number"] or "").lower()
                    if ch.isalnum()
                ) or None

                conn.execute(
                    "UPDATE invoices SET num_key=? WHERE id=?",
                    (key, row["id"]),
                )

            # Add OCR confidence to legacy line-item tables.
            line_item_columns = _columns(conn, "line_items")

            if "ocr_conf" not in line_item_columns:
                conn.execute(
                    "ALTER TABLE line_items ADD COLUMN ocr_conf REAL"
                )

            # Create the invoice number lookup index after num_key exists.
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_invoices_numkey
                ON invoices(num_key)
                """
            )

            conn.execute("COMMIT")

        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

        finally:
            # Restore foreign-key enforcement.
            conn.execute("PRAGMA foreign_keys=ON")

    finally:
        conn.close()



def add_event(
    db,
    document_id: int,
    event: str,
    detail: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO events(document_id, ts, event, detail) VALUES (?,?,?,?)",
        (document_id, now(), event, detail),
    )
