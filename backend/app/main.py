
"""FastAPI application: upload, status, results, search."""


import hmac
import json
import os


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

from . import worker
from .config import get_settings
from .logging_config import configure_logging
from .db import add_event, connect, init_db, now
from .routers.health import router as health_router
from .routers.upload import router as upload_router
from .routers.documents import router as documents_router
from .routers.invoice_review import router as invoice_review_router
from .routers.search import router as search_router

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
app.include_router(search_router, prefix="/api")

# ------------------------------------------------------------------
# Documents
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# Keyword search
# ------------------------------------------------------------------

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
