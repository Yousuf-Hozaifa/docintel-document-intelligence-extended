
"""FastAPI application: upload, status, results, search."""

import hashlib
import hmac
import json
import os
import re
import uuid
from contextlib import asynccontextmanager

from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import semantic, worker
from .config import get_settings
from .corrections import (
    CorrectionError,
    InvoicePatch,
    apply_corrections,
    set_issue_resolved,
)
from .db import add_event, connect, init_db, now
from .errors import DocumentError
from .readers import ALLOWED_EXT, detect_type
from .validate import RULES


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    worker.start()
    yield
    worker.stop()


def require_key(
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    """Optional shared-secret auth; health checks stay open."""
    expected = os.environ.get("DOCINTEL_API_KEY")

    if (
        not expected
        or request.url.path == "/api/health"
        or request.method == "OPTIONS"
    ):
        return

    if not x_api_key or not hmac.compare_digest(
        x_api_key.encode(),
        expected.encode(),
    ):
        raise HTTPException(401, "Missing or invalid X-API-Key header")


app = FastAPI(
    title="Care Guide Document Intelligence API",
    version="0.2.0",
    lifespan=lifespan,
    dependencies=[Depends(require_key)],
)

app.add_middleware(
    CORSMiddleware,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key"],
    allow_origins=[
        origin.strip()
        for origin in os.environ.get(
            "DOCINTEL_CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173",
        ).split(",")
        if origin.strip()
    ],
)


def _stored_path(stored_name: str):
    """Return a stored upload path only when it stays inside the upload directory."""
    base = get_settings().upload_dir.resolve()
    path = (base / stored_name).resolve()

    return (
        path
        if path.is_relative_to(base) and path != base
        else None
    )


def _safe_name(name: str) -> str:
    base = re.sub(
        r"[^\w.\- ]",
        "_",
        (name or "upload").replace("\\", "/").split("/")[-1],
    )
    return base[:150] or "upload"


def _doc_row(row) -> dict:
    document = dict(row)
    document.pop("stored_name", None)
    document["review_required"] = bool(
        document["review_required"]
    )
    return document


@app.get("/api/health")
def health():
    """Report API and OCR availability."""
    from .readers import ocr_status

    status = ocr_status()

    return {
        "status": "ok",
        "ocr": {
            "available": status["available"],
            "version": status["version"],
            "hint": status["hint"],
        },
    }


@app.get("/api/rules")
def rules():
    return RULES


@app.post("/api/documents", status_code=202)
async def upload(files: list[UploadFile] = File(...)):
    """Accept multiple files; reject invalid files independently."""
    settings = get_settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)

    results = []

    for file in files:
        name = _safe_name(file.filename)
        ext = (
            "." + name.rsplit(".", 1)[-1].lower()
            if "." in name
            else ""
        )

        if ext not in ALLOWED_EXT:
            results.append({
                "filename": name,
                "status": "rejected",
                "error": f"Unsupported extension '{ext}'",
            })
            continue

        stored = f"{uuid.uuid4().hex}{ext}"
        path = settings.upload_dir / stored

        file_hash = hashlib.sha256()
        size = 0
        too_big = False

        with open(path, "wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)

                if size > settings.max_upload_bytes:
                    too_big = True
                    break

                file_hash.update(chunk)
                output.write(chunk)

        if too_big or size == 0:
            path.unlink(missing_ok=True)

            results.append({
                "filename": name,
                "status": "rejected",
                "error": (
                    "File exceeds the upload size limit"
                    if too_big
                    else "File is empty"
                ),
            })
            continue

        try:
            detect_type(path, name)
        except DocumentError as error:
            path.unlink(missing_ok=True)

            results.append({
                "filename": name,
                "status": "rejected",
                "error": str(error),
            })
            continue

        sha = file_hash.hexdigest()

        with connect() as db:
            existing = db.execute(
                """
                SELECT id, status
                FROM documents
                WHERE sha256=? AND status!='failed'
                ORDER BY id
                LIMIT 1
                """,
                (sha,),
            ).fetchone()

            if existing:
                path.unlink(missing_ok=True)

                results.append({
                    "filename": name,
                    "status": "duplicate",
                    "document_id": existing["id"],
                    "duplicate_of": existing["id"],
                })
                continue

            timestamp = now()

            cursor = db.execute(
                """
                INSERT INTO documents(
                    filename,
                    stored_name,
                    sha256,
                    size_bytes,
                    status,
                    active_generation,
                    next_generation,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    stored,
                    sha,
                    size,
                    "queued",
                    0,
                    1,
                    timestamp,
                    timestamp,
                ),
            )

            document_id = cursor.lastrowid
            job_id = worker.enqueue(db, document_id)

            add_event(
                db,
                document_id,
                "uploaded",
                f"size={size} sha256={sha[:12]}",
            )

        results.append({
            "filename": name,
            "status": "queued",
            "document_id": document_id,
            "job_id": job_id,
        })

    return {"results": results}


# ------------------------------------------------------------------
# Documents
# ------------------------------------------------------------------

@app.get("/api/documents")
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


def _invoice_detail(db, invoice) -> dict:
    """Return invoice details and identify archived invoices as read-only."""
    result = dict(invoice)

    document = db.execute(
        """
        SELECT active_generation
        FROM documents
        WHERE id=?
        """,
        (result["document_id"],),
    ).fetchone()

    active_generation = (
        document["active_generation"]
        if document
        else None
    )

    result["fields"] = json.loads(
        result.pop("fields_json")
    )
    result["needs_review"] = bool(result["needs_review"])
    result.pop("dup_key", None)

    result["active_generation"] = active_generation
    result["archived"] = (
        active_generation is None
        or result["generation_id"] != active_generation
    )
    result["read_only"] = result["archived"]

    result["line_items"] = [
        dict(item)
        for item in db.execute(
            """
            SELECT idx, description, quantity, unit_price,
                   amount, evidence
            FROM line_items
            WHERE invoice_id=?
            ORDER BY idx
            """,
            (result["id"],),
        )
    ]

    result["issues"] = [
        dict(issue, resolved=bool(issue["resolved"]))
        for issue in db.execute(
            """
            SELECT id, rule_id, severity, field,
                   message, resolved
            FROM validation_issues
            WHERE invoice_id=?
            ORDER BY id
            """,
            (result["id"],),
        )
    ]

    result["correction_count"] = db.execute(
        """
        SELECT COUNT(*)
        FROM corrections
        WHERE invoice_id=?
        """,
        (result["id"],),
    ).fetchone()[0]

    return result


@app.get("/api/documents/{doc_id}")
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


# ------------------------------------------------------------------
# Keyword search
# ------------------------------------------------------------------

def _fts_query(query: str) -> str | None:
    tokens = re.findall(r"\w+", query, flags=re.UNICODE)

    return (
        " ".join(
            f'"{token}"' + ("*" if index == len(tokens) - 1 else "")
            for index, token in enumerate(tokens)
        )
        or None
    )


@app.get("/api/search")
def search(
    q: str | None = None,
    supplier: str | None = None,
    customer: str | None = None,
    currency: str | None = None,
    status: str | None = None,
    review: bool | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = Query(25, le=100),
):
    """Search active-generation text and invoice data."""
    query = (
        "SELECT d.id AS document_id, d.filename, d.status, "
        "d.review_required, i.id AS invoice_id, i.invoice_number, "
        "i.supplier, i.customer, i.invoice_date, i.total, i.currency "
        "FROM documents d "
        "LEFT JOIN invoices i "
        "ON i.document_id=d.id "
        "AND i.generation_id=d.active_generation "
        "WHERE 1=1"
    )

    args: list = []
    fts = _fts_query(q) if q else None

    if q and not fts:
        return {"results": []}

    if fts:
        query += """
            AND EXISTS (
                SELECT 1
                FROM page_fts
                WHERE page_fts MATCH ?
                  AND page_fts.document_id=d.id
                  AND page_fts.generation_id=d.active_generation
            )
        """
        args.append(fts)

    for column, value in (
        ("i.supplier", supplier),
        ("i.customer", customer),
    ):
        if value:
            query += f" AND {column} LIKE ?"
            args.append(f"%{value}%")

    if currency:
        query += " AND i.currency=?"
        args.append(currency.upper())

    if status:
        query += " AND d.status=?"
        args.append(status)

    if review is not None:
        query += " AND d.review_required=?"
        args.append(int(review))

    if date_from:
        query += " AND i.invoice_date>=?"
        args.append(date_from)

    if date_to:
        query += " AND i.invoice_date<=?"
        args.append(date_to)

    query += " ORDER BY d.id DESC LIMIT ?"
    args.append(limit)

    with connect() as db:
        rows = [
            dict(row)
            for row in db.execute(query, args)
        ]

        if fts:
            for row in rows:
                snippets = db.execute(
                    """
                    SELECT page_no,
                           snippet(
                               page_fts, 0,
                               '<mark>', '</mark>', ' … ', 14
                           ) AS snippet
                    FROM page_fts
                    WHERE page_fts MATCH ?
                      AND document_id=?
                      AND generation_id=(
                          SELECT active_generation
                          FROM documents
                          WHERE id=?
                      )
                    ORDER BY rank
                    LIMIT 3
                    """,
                    (
                        fts,
                        row["document_id"],
                        row["document_id"],
                    ),
                ).fetchall()

                row["snippets"] = [
                    dict(item) for item in snippets
                ]

    return {"results": rows}


# ------------------------------------------------------------------
# Corrections and review
# ------------------------------------------------------------------

@app.get("/api/invoices/{invoice_id}")
def get_invoice(invoice_id: int):
    """Retrieve an active or archived invoice by its ID."""
    with connect() as db:
        invoice = db.execute(
            "SELECT * FROM invoices WHERE id=?",
            (invoice_id,),
        ).fetchone()

        if not invoice:
            raise HTTPException(404, "Invoice not found")

        return _invoice_detail(db, invoice)


@app.patch("/api/invoices/{invoice_id}")
def correct_invoice(
    invoice_id: int,
    patch: InvoicePatch,
):
    """Correct active invoices with an audit trail."""
    try:
        with connect() as db:
            changes = apply_corrections(
                db,
                invoice_id,
                patch,
            )

            invoice = db.execute(
                "SELECT * FROM invoices WHERE id=?",
                (invoice_id,),
            ).fetchone()

            output = _invoice_detail(db, invoice)

    except LookupError as error:
        raise HTTPException(404, str(error))

    except CorrectionError as error:
        raise HTTPException(422, str(error))

    output["changes_applied"] = changes
    return output


@app.get("/api/invoices/{invoice_id}/corrections")
def list_corrections(invoice_id: int):
    """Return the correction history, including archived invoices."""
    with connect() as db:
        exists = db.execute(
            "SELECT 1 FROM invoices WHERE id=?",
            (invoice_id,),
        ).fetchone()

        if not exists:
            raise HTTPException(404, "Invoice not found")

        corrections = db.execute(
            """
            SELECT id, field, old_value, new_value,
                   note, created_at
            FROM corrections
            WHERE invoice_id=?
            ORDER BY id
            """,
            (invoice_id,),
        )

        return {
            "corrections": [
                dict(row) for row in corrections
            ]
        }


class ResolveBody(BaseModel):
    resolved: bool = True


@app.post("/api/issues/{issue_id}/resolve")
def resolve_issue(
    issue_id: int,
    body: ResolveBody | None = None,
):
    """Resolve or reopen an issue on an active invoice."""
    try:
        with connect() as db:
            invoice_id = set_issue_resolved(
                db,
                issue_id,
                (body or ResolveBody()).resolved,
            )

            invoice = db.execute(
                "SELECT * FROM invoices WHERE id=?",
                (invoice_id,),
            ).fetchone()

            return _invoice_detail(db, invoice)

    except LookupError as error:
        raise HTTPException(404, str(error))

    except CorrectionError as error:
        raise HTTPException(422, str(error))


# ------------------------------------------------------------------
# Jobs and reprocessing
# ------------------------------------------------------------------

@app.get("/api/jobs")
def list_jobs(
    status: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    query = (
        "SELECT j.id, j.document_id, d.filename, d.file_type, "
        "j.status, j.attempts, j.max_attempts, j.progress_done, "
        "j.progress_total, j.error, d.error_code, "
        "j.created_at, j.started_at, j.finished_at "
        "FROM jobs j "
        "JOIN documents d ON d.id=j.document_id "
        "WHERE 1=1"
    )

    args: list = []

    if status:
        query += " AND j.status=?"
        args.append(status)

    query += " ORDER BY j.id DESC LIMIT ? OFFSET ?"

    with connect() as db:
        return {
            "jobs": [
                dict(row)
                for row in db.execute(
                    query,
                    (*args, limit, offset),
                )
            ]
        }


def _queue_reprocess(db, doc_id: int) -> int:
    document = db.execute(
        """
        SELECT id, status, active_generation
        FROM documents
        WHERE id=?
        """,
        (doc_id,),
    ).fetchone()

    if not document:
        raise HTTPException(404, "Document not found")

    active_job = db.execute(
        """
        SELECT id
        FROM jobs
        WHERE document_id=?
          AND status IN ('queued', 'running')
        """,
        (doc_id,),
    ).fetchone()

    if active_job:
        raise HTTPException(
            409,
            f"Document already has an active job ({active_job['id']})",
        )

    # Keep the previous successful status visible during reprocessing.
    # Only documents without a successful generation become queued.
    db.execute(
        """
        UPDATE documents
        SET status=CASE
                WHEN active_generation=0 THEN 'queued'
                ELSE status
            END,
            error_code=CASE
                WHEN active_generation=0 THEN NULL
                ELSE error_code
            END,
            error_message=CASE
                WHEN active_generation=0 THEN NULL
                ELSE error_message
            END,
            updated_at=?
        WHERE id=?
        """,
        (now(), doc_id),
    )

    job_id = worker.enqueue(db, doc_id)

    add_event(
        db,
        doc_id,
        "reprocess_requested",
        f"job={job_id}",
    )

    return job_id


@app.post("/api/documents/{doc_id}/reprocess", status_code=202)
def reprocess(doc_id: int):
    """Reprocess into a new generation while retaining previous results."""
    with connect() as db:
        return {
            "document_id": doc_id,
            "job_id": _queue_reprocess(db, doc_id),
        }


@app.post("/api/jobs/retry-failed", status_code=202)
def retry_failed():
    """Retry documents whose latest processing job failed."""
    with connect() as db:
        ids = [
            row["document_id"]
            for row in db.execute(
                """
                SELECT j.document_id
                FROM jobs j
                WHERE j.status='failed'
                  AND j.id=(
                      SELECT MAX(latest.id)
                      FROM jobs latest
                      WHERE latest.document_id=j.document_id
                  )
                ORDER BY j.document_id
                """
            )
        ]

        queued = []

        for document_id in ids:
            try:
                queued.append({
                    "document_id": document_id,
                    "job_id": _queue_reprocess(
                        db,
                        document_id,
                    ),
                })
            except HTTPException:
                continue

    return {"queued": queued}


# ------------------------------------------------------------------
# Semantic search
# ------------------------------------------------------------------

@app.get("/api/semantic/status")
def semantic_status():
    return semantic.status()


@app.post("/api/semantic/reindex", status_code=202)
def semantic_reindex():
    """Start background indexing of active documents."""
    try:
        started = semantic.start_reindex()

    except semantic.SemanticDisabled as error:
        raise HTTPException(503, str(error))

    if not started:
        raise HTTPException(409, "A reindex is already running")

    return semantic.status()["reindex"]


@app.get("/api/search/semantic")
def semantic_search(
    q: str = Query(..., min_length=1, max_length=500),
    supplier: str | None = None,
    customer: str | None = None,
    currency: str | None = None,
    status: str | None = None,
    review: bool | None = None,
    mode: str = Query(
        "hybrid",
        pattern="^(semantic|hybrid)$",
    ),
    limit: int = Query(10, le=50),
):
    """Search active-generation semantic chunks and invoice data."""
    try:
        hits = (
            semantic.search(q, limit=100)
            if mode == "semantic"
            else semantic.hybrid_search(q, limit=100)
        )

    except semantic.SemanticDisabled as error:
        raise HTTPException(503, str(error))

    if not hits:
        return {
            "results": [],
            "model": semantic.status().get("model"),
        }

    ids = [hit["document_id"] for hit in hits]

    query = (
        "SELECT d.id, d.filename, d.status, d.review_required "
        "FROM documents d "
        "WHERE d.id IN (%s)"
        % ",".join("?" * len(ids))
    )

    args: list = list(ids)

    if status:
        query += " AND d.status=?"
        args.append(status)

    if review is not None:
        query += " AND d.review_required=?"
        args.append(int(review))

    for column, value in (
        ("supplier", supplier),
        ("customer", customer),
    ):
        if value:
            query += (
                " AND EXISTS ("
                "SELECT 1 FROM invoices i "
                "WHERE i.document_id=d.id "
                "AND i.generation_id=d.active_generation "
                f"AND i.{column} LIKE ?)"
            )
            args.append(f"%{value}%")

    if currency:
        query += (
            " AND EXISTS ("
            "SELECT 1 FROM invoices i "
            "WHERE i.document_id=d.id "
            "AND i.generation_id=d.active_generation "
            "AND i.currency=?)"
        )
        args.append(currency.upper())

    output = []

    with connect() as db:
        documents = {
            row["id"]: row
            for row in db.execute(query, args)
        }

        for hit in hits:
            document = documents.get(hit["document_id"])

            if not document:
                continue

            invoice = db.execute(
                """
                SELECT invoice_number, supplier, customer,
                       invoice_date, total, currency
                FROM invoices
                WHERE document_id=?
                  AND generation_id=(
                      SELECT active_generation
                      FROM documents
                      WHERE id=?
                  )
                ORDER BY seq
                LIMIT 1
                """,
                (document["id"], document["id"]),
            ).fetchone()

            output.append({
                "document_id": document["id"],
                "filename": document["filename"],
                "status": document["status"],
                "review_required": bool(
                    document["review_required"]
                ),
                "score": hit["score"],
                "mode": mode,
                "sources": hit.get(
                    "sources",
                    ["semantic"],
                ),
                "snippets": [{
                    "page_no": hit["page_no"],
                    "snippet": hit["text"][:400],
                }],
                **(dict(invoice) if invoice else {}),
            })

            if len(output) >= limit:
                break

    return {
        "results": output,
        "model": semantic.status().get("model"),
    }


# ------------------------------------------------------------------
# Deletion
# ------------------------------------------------------------------

@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: int):
    """Permanently delete a document and all its generations and history."""
    from .review import revalidate_related

    settings = get_settings()

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


# ------------------------------------------------------------------
# Original file
# ------------------------------------------------------------------

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


@app.get("/api/documents/{doc_id}/file")
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
