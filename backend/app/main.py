
"""FastAPI application: upload, status, results, search."""


import hmac
import json
import os
import re

from contextlib import asynccontextmanager

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import semantic, worker
from .config import get_settings
from .logging_config import configure_logging
from .db import add_event, connect, init_db, now
from .routers.health import router as health_router
from .routers.upload import router as upload_router
from .routers.documents import router as documents_router
from .routers.invoice_review import router as invoice_review_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
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
app.include_router(health_router, prefix="/api")
app.include_router(upload_router, prefix="/api")
app.include_router(documents_router, prefix="/api")
app.include_router(invoice_review_router, prefix="/api")


# ------------------------------------------------------------------
# Documents
# ------------------------------------------------------------------

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
