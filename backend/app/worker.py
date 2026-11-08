
"""Lightweight DB-backed job queue with generation-safe failure handling.

Jobs track individual processing attempts. A document retains its last
successful status and active generation when a later attempt fails.
"""

import threading
import time
import traceback

from .config import get_settings
from .db import add_event, connect, now
from .errors import DocumentError
from .pipeline import process_document

_stop = threading.Event()
_threads: list[threading.Thread] = []


def enqueue(db, document_id: int) -> int:
    settings = get_settings()

    cursor = db.execute(
        """
        INSERT INTO jobs(
            document_id, status, max_attempts, created_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            document_id,
            "queued",
            settings.max_attempts,
            now(),
        ),
    )

    return cursor.lastrowid


def recover_stuck() -> int:
    """Requeue interrupted jobs and discard incomplete staging generations."""
    with connect() as db:
        cursor = db.execute(
            """
            UPDATE jobs
            SET status='queued',
                run_after=0
            WHERE status='running'
            """
        )

        # Remove abandoned staging generations. Never delete the active one.
        documents = db.execute(
            """
            SELECT id, processing_generation
            FROM documents
            WHERE processing_generation IS NOT NULL
               OR status='processing'
            """
        ).fetchall()

        for document in documents:
            doc_id = document["id"]
            generation_id = document["processing_generation"]

            if generation_id is not None:
                db.execute(
                    """
                    DELETE FROM page_fts
                    WHERE document_id=? AND generation_id=?
                    """,
                    (doc_id, generation_id),
                )

                db.execute(
                    """
                    DELETE FROM chunks
                    WHERE document_id=? AND generation_id=?
                    """,
                    (doc_id, generation_id),
                )

                db.execute(
                    """
                    DELETE FROM pages
                    WHERE document_id=? AND generation_id=?
                    """,
                    (doc_id, generation_id),
                )

                db.execute(
                    """
                    DELETE FROM invoices
                    WHERE document_id=? AND generation_id=?
                    """,
                    (doc_id, generation_id),
                )

            db.execute(
                """
                UPDATE documents
                SET processing_generation=NULL,
                    status=CASE
                        WHEN active_generation=0
                             AND status='processing'
                        THEN 'queued'
                        ELSE status
                    END,
                    updated_at=?
                WHERE id=?
                """,
                (now(), doc_id),
            )

        return cursor.rowcount


def _claim():
    """Atomically claim the next runnable job."""
    with connect() as db:
        row = db.execute(
            """
            SELECT id, document_id
            FROM jobs
            WHERE status='queued'
              AND run_after<=?
            ORDER BY id
            LIMIT 1
            """,
            (time.time(),),
        ).fetchone()

        if not row:
            return None

        db.execute(
            """
            UPDATE jobs
            SET status='running',
                attempts=attempts+1,
                started_at=?,
                error=NULL
            WHERE id=?
            """,
            (now(), row["id"]),
        )

        return row["id"], row["document_id"]


def run_one() -> bool:
    """Claim and execute one job. Return False when none is runnable."""
    claimed = _claim()

    if not claimed:
        return False

    job_id, doc_id = claimed
    settings = get_settings()

    try:
        status = process_document(doc_id, job_id)

        with connect() as db:
            db.execute(
                """
                UPDATE jobs
                SET status='completed',
                    finished_at=?
                WHERE id=?
                """,
                (now(), job_id),
            )

            add_event(
                db,
                doc_id,
                "job_completed",
                f"job={job_id} status={status}",
            )

    except Exception as error:  # noqa: BLE001
        permanent = isinstance(error, DocumentError)
        code = getattr(error, "code", "processing_error")
        message = f"{type(error).__name__}: {error}"

        with connect() as db:
            attempts = db.execute(
                """
                SELECT attempts, max_attempts
                FROM jobs
                WHERE id=?
                """,
                (job_id,),
            ).fetchone()

            if attempts is None:
                raise RuntimeError(
                    f"Job {job_id} disappeared while processing"
                )

            # Retry transient failures while attempts remain.
            if (
                not permanent
                and attempts["attempts"] < attempts["max_attempts"]
            ):
                delay = settings.retry_backoff_s * (
                    2 ** (attempts["attempts"] - 1)
                )

                db.execute(
                    """
                    UPDATE jobs
                    SET status='queued',
                        run_after=?,
                        error=?
                    WHERE id=?
                    """,
                    (time.time() + delay, message, job_id),
                )

                # Only documents without a successful generation become
                # queued. Previously successful documents keep their status.
                db.execute(
                    """
                    UPDATE documents
                    SET status=CASE
                            WHEN active_generation=0 THEN 'queued'
                            ELSE status
                        END,
                        updated_at=?
                    WHERE id=?
                    """,
                    (now(), doc_id),
                )

                add_event(
                    db,
                    doc_id,
                    "retry_scheduled",
                    (
                        f"job={job_id} attempt={attempts['attempts']} "
                        f"in {delay:.0f}s: {message}"
                    ),
                )

            else:
                db.execute(
                    """
                    UPDATE jobs
                    SET status='failed',
                        error=?,
                        finished_at=?
                    WHERE id=?
                    """,
                    (message, now(), job_id),
                )

                # Preserve the last successful document result and its
                # status. Only an unprocessed document becomes failed.
                db.execute(
                    """
                    UPDATE documents
                    SET status=CASE
                            WHEN active_generation=0 THEN 'failed'
                            ELSE status
                        END,
                        error_code=CASE
                            WHEN active_generation=0 THEN ?
                            ELSE error_code
                        END,
                        error_message=CASE
                            WHEN active_generation=0 THEN ?
                            ELSE error_message
                        END,
                        updated_at=?
                    WHERE id=?
                    """,
                    (
                        code,
                        str(error)[:500],
                        now(),
                        doc_id,
                    ),
                )

                add_event(
                    db,
                    doc_id,
                    "job_failed",
                    f"job={job_id} {code}: {error}",
                )

        if not permanent:
            traceback.print_exc()

    return True


def run_all(max_jobs: int = 10_000) -> int:
    """Drain the queue synchronously (used by tests and CLI)."""
    count = 0

    while count < max_jobs and run_one():
        count += 1

    return count


def _loop() -> None:
    while not _stop.is_set():
        try:
            if not run_one():
                _stop.wait(0.5)

        except Exception:  # noqa: BLE001
            traceback.print_exc()
            _stop.wait(1.0)


def start() -> None:
    settings = get_settings()

    _stop.clear()
    recover_stuck()

    for index in range(settings.workers):
        thread = threading.Thread(
            target=_loop,
            name=f"worker-{index}",
            daemon=True,
        )

        thread.start()
        _threads.append(thread)


def stop() -> None:
    _stop.set()

    for thread in _threads:
        thread.join(timeout=5)

    _threads.clear()
