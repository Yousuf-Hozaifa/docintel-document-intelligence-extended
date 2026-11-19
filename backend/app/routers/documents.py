"""Document listing, detail, deletion, and original-file API endpoints."""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from ..db import connect
from ..document_helpers import (
    _doc_row,
    _invoice_detail,
    _stored_path,
)


router = APIRouter()


@router.get("/documents")
def list_documents(
    status: str | None = None,
    review: bool | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    """List documents with active and archived invoice counts."""
    query = "SELECT * FROM documents WHERE 1=1"
    args = []

    if status:
        query += " AND status=?"
        args.append(status)

    if review is not None:
        query += " AND review_required=?"
        args.append(int(review))

    query += " ORDER BY id DESC LIMIT ? OFFSET ?"

    with connect() as db:
        rows = db.execute(
            query,
            (*args, limit, offset),
        ).fetchall()

        output = []

        for row in rows:
            document = _doc_row(row)

            job = db.execute(
                """
                SELECT id, status, attempts, progress_done,
                       progress_total, error
                FROM jobs
                WHERE document_id=?
                ORDER BY id DESC
                LIMIT 1
                """,
                (row["id"],),
            ).fetchone()

            document["job"] = dict(job) if job else None

            document["invoice_count"] = db.execute(
                """
                SELECT COUNT(*)
                FROM invoices
                WHERE document_id=?
                  AND generation_id=?
                """,
                (row["id"], row["active_generation"]),
            ).fetchone()[0]

            document["archived_invoice_count"] = db.execute(
                """
                SELECT COUNT(*)
                FROM invoices
                WHERE document_id=?
                  AND generation_id != ?
                """,
                (row["id"], row["active_generation"]),
            ).fetchone()[0]

            output.append(document)

    return {"documents": output}


@router.get("/documents/{doc_id}")
def get_document(
    doc_id: int,
    invoice_limit: int = Query(50, ge=1, le=500),
    invoice_offset: int = Query(0, ge=0),
):
    """Return active-generation results and archived invoice counts."""
    with connect() as db:
        row = db.execute(
            "SELECT * FROM documents WHERE id=?",
            (doc_id,),
        ).fetchone()

        if not row:
            raise HTTPException(404, "Document not found")

        document = _doc_row(row)
        active_generation = row["active_generation"]

        job = db.execute(
            """
            SELECT *
            FROM jobs
            WHERE document_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (doc_id,),
        ).fetchone()

        document["job"] = dict(job) if job else None

        document["pages"] = [
            dict(page)
            for page in db.execute(
                """
                SELECT page_no, label, method, ocr_confidence,
                       error, substr(text,1,2000) AS text_preview
                FROM pages
                WHERE document_id=?
                  AND generation_id=?
                ORDER BY page_no
                LIMIT 200
                """,
                (doc_id, active_generation),
            )
        ]

        document["invoice_total"] = db.execute(
            """
            SELECT COUNT(*)
            FROM invoices
            WHERE document_id=?
              AND generation_id=?
            """,
            (doc_id, active_generation),
        ).fetchone()[0]

        document["archived_invoice_total"] = db.execute(
            """
            SELECT COUNT(*)
            FROM invoices
            WHERE document_id=?
              AND generation_id != ?
            """,
            (doc_id, active_generation),
        ).fetchone()[0]

        invoices = db.execute(
            """
            SELECT *
            FROM invoices
            WHERE document_id=?
              AND generation_id=?
            ORDER BY seq
            LIMIT ? OFFSET ?
            """,
            (
                doc_id,
                active_generation,
                invoice_limit,
                invoice_offset,
            ),
        ).fetchall()

        document["invoices"] = [
            _invoice_detail(db, invoice)
            for invoice in invoices
        ]

        document["history"] = [
            dict(event)
            for event in db.execute(
                """
                SELECT ts, event, detail
                FROM events
                WHERE document_id=?
                ORDER BY id
                """,
                (doc_id,),
            )
        ]

    return document


@router.delete("/documents/{doc_id}")
def delete_document(doc_id: int):
    """Permanently delete a document and all its generations and history."""
    from ..review import revalidate_related

    with connect() as db:
        document = db.execute(
            """
            SELECT stored_name, active_generation
            FROM documents
            WHERE id=?
            """,
            (doc_id,),
        ).fetchone()

        if not document:
            raise HTTPException(404, "Document not found")

        active_job = db.execute(
            """
            SELECT 1
            FROM jobs
            WHERE document_id=?
              AND status IN ('queued', 'running')
            """,
            (doc_id,),
        ).fetchone()

        if active_job:
            raise HTTPException(
                409,
                "Document has an active job; wait for it to finish",
            )

        keys = {
            row["num_key"]
            for row in db.execute(
                """
                SELECT DISTINCT num_key
                FROM invoices
                WHERE document_id=?
                  AND generation_id=?
                """,
                (doc_id, document["active_generation"]),
            )
            if row["num_key"]
        }

        db.execute(
            "DELETE FROM page_fts WHERE document_id=?",
            (doc_id,),
        )

        db.execute(
            "DELETE FROM documents WHERE id=?",
            (doc_id,),
        )

        revalidate_related(db, keys)

    target = _stored_path(document["stored_name"])

    if target is not None:
        target.unlink(missing_ok=True)

    return {"deleted": doc_id}


_INLINE_TYPES = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "tiff": "image/tiff",
}

_DOWNLOAD_TYPES = {
    "xlsx": (
        "application/vnd.openxmlformats-officedocument."
        "spreadsheetml.sheet"
    ),
    "xls": "application/vnd.ms-excel",
    "csv": "text/csv",
}


@router.get("/documents/{doc_id}/file")
def original_file(doc_id: int):
    """Serve the uploaded original file for human review."""
    with connect() as db:
        document = db.execute(
            """
            SELECT filename, stored_name, file_type
            FROM documents
            WHERE id=?
            """,
            (doc_id,),
        ).fetchone()

    if not document:
        raise HTTPException(404, "Document not found")

    path = _stored_path(document["stored_name"])
    kind = document["file_type"]

    if (
        path is None
        or kind not in {**_INLINE_TYPES, **_DOWNLOAD_TYPES}
        or not path.exists()
    ):
        raise HTTPException(
            404,
            "Original file is not available",
        )

    media = (
        _INLINE_TYPES.get(kind)
        or _DOWNLOAD_TYPES[kind]
    )

    disposition = (
        "inline"
        if kind in _INLINE_TYPES
        else "attachment"
    )

    return FileResponse(
        path,
        media_type=media,
        filename=document["filename"],
        content_disposition_type=disposition,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                "default-src 'none'; "
                "style-src 'unsafe-inline'; "
                "img-src data:"
            ),
            "Cache-Control": "private, no-store",
        },
    )
