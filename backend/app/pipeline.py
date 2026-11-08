
"""Processing pipeline with generation-based safe reprocessing.

Each attempt writes to a new staging generation. The previous successful
generation remains active until the new generation is fully processed.
Failed attempts remove only their own staging data.
"""

import json
import time

from .config import get_settings
from .db import add_event, connect, now
from .errors import CorruptFile, DocumentError, NoContent
from .extract import FIELDS, extract_invoice, extract_register, segment_pages
from .readers import detect_type, open_pages
from .semantic import embed_after_processing
from .review import (
    cross_issues,
    norm,
    refresh_document_review,
    revalidate_related,
    write_issues,
)
from .validate import validate_invoice


def _discard_generation(db, doc_id: int, generation_id: int) -> None:
    """Delete only the specified staging generation."""
    db.execute(
        "DELETE FROM page_fts WHERE document_id=? AND generation_id=?",
        (doc_id, generation_id),
    )
    db.execute(
        "DELETE FROM chunks WHERE document_id=? AND generation_id=?",
        (doc_id, generation_id),
    )
    db.execute(
        "DELETE FROM pages WHERE document_id=? AND generation_id=?",
        (doc_id, generation_id),
    )
    db.execute(
        "DELETE FROM invoices WHERE document_id=? AND generation_id=?",
        (doc_id, generation_id),
    )


def _allocate_generation(doc_id: int) -> tuple[int, set[str]]:
    """Reserve a new staging generation without changing active results."""
    with connect() as db:
        doc = db.execute(
            """
            SELECT active_generation, next_generation, processing_generation
            FROM documents
            WHERE id=?
            """,
            (doc_id,),
        ).fetchone()

        if doc is None:
            raise DocumentError(f"Document {doc_id} not found")

        if doc["processing_generation"] is not None:
            raise RuntimeError(
                f"Document {doc_id} already has a processing generation"
            )

        generation_id = max(
            int(doc["next_generation"] or 1),
            int(doc["active_generation"] or 0) + 1,
        )

        old_keys = {
            row["num_key"]
            for row in db.execute(
                """
                SELECT DISTINCT num_key
                FROM invoices
                WHERE document_id=?
                  AND generation_id=?
                """,
                (doc_id, doc["active_generation"]),
            )
            if row["num_key"]
        }

        db.execute(
            """
            UPDATE documents
            SET processing_generation=?,
                next_generation=?,
                status=CASE
                    WHEN active_generation=0 THEN 'processing'
                    ELSE status
                END,
                updated_at=?
            WHERE id=?
            """,
            (generation_id, generation_id + 1, now(), doc_id),
        )

        add_event(
            db,
            doc_id,
            "generation_started",
            f"generation={generation_id}",
        )

        return generation_id, old_keys


def _cleanup_failed_generation(
    doc_id: int,
    generation_id: int,
) -> None:
    """Remove failed staging data while preserving the active generation."""
    with connect() as db:
        doc = db.execute(
            """
            SELECT processing_generation
            FROM documents
            WHERE id=?
            """,
            (doc_id,),
        ).fetchone()

        if doc is None or doc["processing_generation"] != generation_id:
            return

        _discard_generation(db, doc_id, generation_id)

        db.execute(
            """
            UPDATE documents
            SET processing_generation=NULL,
                updated_at=?
            WHERE id=? AND processing_generation=?
            """,
            (now(), doc_id, generation_id),
        )

        add_event(
            db,
            doc_id,
            "generation_discarded",
            f"generation={generation_id}",
        )


def process_document(
    doc_id: int,
    job_id: int | None = None,
) -> str:
    """Process a document into a staging generation and activate it on success.

    Permanent document errors are raised as DocumentError subclasses.
    Other exceptions may be retried by the worker.
    """
    settings = get_settings()

    with connect() as db:
        doc = db.execute(
            "SELECT * FROM documents WHERE id=?",
            (doc_id,),
        ).fetchone()

        if doc is None:
            raise DocumentError(f"Document {doc_id} not found")

        stored_name = doc["stored_name"]
        filename = doc["filename"]

    path = settings.upload_dir / stored_name

    if not path.exists():
        raise CorruptFile("Stored file is missing")

    t0 = time.perf_counter()

    # Detect and open before reserving a generation. Failures here cannot
    # affect the existing successful results.
    kind = detect_type(path, filename)
    total, pages = open_pages(path, kind)

    generation_id = None

    try:
        generation_id, old_keys = _allocate_generation(doc_id)

        with connect() as db:
            if job_id:
                db.execute(
                    """
                    UPDATE jobs
                    SET progress_done=0, progress_total=?
                    WHERE id=?
                    """,
                    (total, job_id),
                )

            add_event(
                db,
                doc_id,
                "read_started",
                f"type={kind} pages={total} generation={generation_id}",
            )

        seg_input: list[tuple] = []
        results = []
        failed = 0

        # Persist each page under the staging generation.
        for pg in pages:
            with connect() as db:
                db.execute(
                    """
                    INSERT INTO pages(
                        document_id, generation_id, page_no,
                        label, method, ocr_confidence, text, error
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        doc_id,
                        generation_id,
                        pg.number,
                        pg.label,
                        pg.method,
                        pg.ocr_confidence,
                        pg.text,
                        pg.error,
                    ),
                )

                if pg.text:
                    db.execute(
                        """
                        INSERT INTO page_fts(
                            text, document_id, page_no, generation_id
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            pg.text,
                            doc_id,
                            pg.number,
                            generation_id,
                        ),
                    )

                if pg.method == "failed":
                    failed += 1
                    add_event(
                        db,
                        doc_id,
                        "page_failed",
                        f"generation={generation_id} page={pg.number} {pg.error}",
                    )

                if job_id:
                    db.execute(
                        """
                        UPDATE jobs
                        SET progress_done=progress_done+1
                        WHERE id=?
                        """,
                        (job_id,),
                    )

            if pg.rows is not None:
                register_results = extract_register(pg.rows)
                pg.rows = None

                if register_results is not None:
                    for result in register_results:
                        result["page_start"] = pg.number
                        result["page_end"] = pg.number

                    results.extend(register_results)
                    continue

            if pg.text.strip():
                confidence = (
                    pg.ocr_confidence / 100.0
                    if pg.ocr_confidence is not None
                    else 1.0
                )

                seg_input.append(
                    (
                        pg.number,
                        pg.text,
                        max(min(confidence, 1.0), 0.05),
                        pg.line_words,
                    )
                )

        if failed == total:
            raise CorruptFile("Every page failed to process")

        if not seg_input and not results:
            raise NoContent(
                "No text could be extracted (blank or unreadable document)"
            )

        for segment in segment_pages(seg_input) if seg_input else []:
            result = extract_invoice(segment)
            result["page_start"] = segment[0][0]
            result["page_end"] = segment[-1][0]
            results.append(result)

        results.sort(key=lambda result: result["page_start"])

        new_keys: set[str] = set()

        # Write invoices and their dependent records into the staging
        # generation. The active generation remains unchanged.
        with connect() as db:
            for sequence, result in enumerate(results, 1):
                fields = result["fields"]
                values = {
                    field: fields[field]["value"]
                    for field in FIELDS
                }

                key = norm(values["invoice_number"]) or None

                if key:
                    new_keys.add(key)

                cursor = db.execute(
                    """
                    INSERT INTO invoices(
                        document_id, generation_id, seq,
                        page_start, page_end, fields_json,
                        invoice_number, invoice_date,
                        supplier, customer, subtotal, tax,
                        total, currency, dup_key, num_key,
                        needs_review, extractor
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        doc_id,
                        generation_id,
                        sequence,
                        result["page_start"],
                        result["page_end"],
                        json.dumps(fields),
                        values["invoice_number"],
                        values["invoice_date"],
                        values["supplier"],
                        values["customer"],
                        values["subtotal"],
                        values["tax"],
                        values["total"],
                        values["currency"],
                        None,
                        key,
                        0,
                        result["extractor"],
                    ),
                )

                invoice_id = cursor.lastrowid

                for item_index, item in enumerate(
                    result["line_items"], 1
                ):
                    db.execute(
                        """
                        INSERT INTO line_items(
                            invoice_id, idx, description,
                            quantity, unit_price, amount,
                            evidence, ocr_conf
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            invoice_id,
                            item_index,
                            item["description"],
                            item["quantity"],
                            item["unit_price"],
                            item["amount"],
                            item["evidence"],
                            item.get("ocr_conf"),
                        ),
                    )

                issues = (
                    validate_invoice(
                        fields,
                        result["line_items"],
                        expect_line_items=(
                            not result["extractor"].endswith("-register")
                        ),
                    )
                    + cross_issues(
                        db,
                        invoice_id,
                        kind,
                        values,
                    )
                )

                write_issues(db, invoice_id, issues)

        status = "partial" if failed else "completed"

        # Atomically activate the new generation. If this transaction
        # fails, SQLite rolls it back and the previous generation stays active.
        with connect() as db:
            current = db.execute(
                """
                SELECT processing_generation, active_generation
                FROM documents
                WHERE id=?
                """,
                (doc_id,),
            ).fetchone()

            if (
                current is None
                or current["processing_generation"] != generation_id
            ):
                raise RuntimeError(
                    "Processing generation changed before activation"
                )

            db.execute(
                """
                UPDATE documents
                SET active_generation=?,
                    processing_generation=NULL,
                    status=?,
                    file_type=?,
                    page_count=?,
                    pages_failed=?,
                    error_code=NULL,
                    error_message=NULL,
                    updated_at=?
                WHERE id=?
                """,
                (
                    generation_id,
                    status,
                    kind,
                    total,
                    failed,
                    now(),
                    doc_id,
                ),
            )

            # Other documents now see the newly activated generation.
            revalidate_related(
                db,
                new_keys | old_keys,
                exclude_document=doc_id,
            )

            refresh_document_review(db, doc_id)

            add_event(
                db,
                doc_id,
                "generation_activated",
                (
                    f"generation={generation_id} status={status} "
                    f"invoices={len(results)} "
                    f"seconds={time.perf_counter() - t0:.2f}"
                ),
            )

        # Semantic indexing is optional and must not invalidate a successful
        # processing generation.
        embed_after_processing(doc_id, generation_id)

        return status

    except Exception:
        if generation_id is not None:
            _cleanup_failed_generation(doc_id, generation_id)
        raise
