"""Tenant-scoped background job and document reprocessing endpoints."""

from fastapi import APIRouter, HTTPException, Query, Request

from .. import worker
from ..db import add_event, connect, now


router = APIRouter()


@router.get("/jobs")
def list_jobs(
    request: Request,
    status: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
):
    """List jobs belonging only to the current tenant's documents."""
    tenant_id = request.state.tenant_id

    query = (
        "SELECT j.id, j.document_id, d.filename, d.file_type, "
        "j.status, j.attempts, j.max_attempts, j.progress_done, "
        "j.progress_total, j.error, d.error_code, "
        "j.created_at, j.started_at, j.finished_at "
        "FROM jobs j "
        "JOIN documents d ON d.id = j.document_id "
        "WHERE d.tenant_id = ?"
    )

    args: list = [tenant_id]

    if status:
        query += " AND j.status = ?"
        args.append(status)

    query += " ORDER BY j.id DESC LIMIT ? OFFSET ?"
    args.extend([limit, offset])

    with connect() as db:
        return {
            "jobs": [
                dict(row)
                for row in db.execute(query, args)
            ]
        }


def _queue_reprocess(db, doc_id: int, tenant_id: int) -> int:
    """Queue reprocessing only if the document belongs to this tenant."""
    document = db.execute(
        """
        SELECT id, status, active_generation
        FROM documents
        WHERE id = ?
          AND tenant_id = ?
        """,
        (doc_id, tenant_id),
    ).fetchone()

    if not document:
        raise HTTPException(404, "Document not found")

    active_job = db.execute(
        """
        SELECT id
        FROM jobs
        WHERE document_id = ?
          AND status IN ('queued', 'running')
        """,
        (doc_id,),
    ).fetchone()

    if active_job:
        raise HTTPException(
            409,
            f"Document already has an active job ({active_job['id']})",
        )

    db.execute(
        """
        UPDATE documents
        SET status = CASE
                WHEN active_generation = 0 THEN 'queued'
                ELSE status
            END,
            error_code = CASE
                WHEN active_generation = 0 THEN NULL
                ELSE error_code
            END,
            error_message = CASE
                WHEN active_generation = 0 THEN NULL
                ELSE error_message
            END,
            updated_at = ?
        WHERE id = ?
          AND tenant_id = ?
        """,
        (now(), doc_id, tenant_id),
    )

    job_id = worker.enqueue(db, doc_id)

    add_event(
        db,
        doc_id,
        "reprocess_requested",
        f"job={job_id}",
    )

    return job_id


@router.post("/documents/{doc_id}/reprocess", status_code=202)
def reprocess(doc_id: int, request: Request):
    """Reprocess a document owned by the current tenant."""
    tenant_id = request.state.tenant_id

    with connect() as db:
        return {
            "document_id": doc_id,
            "job_id": _queue_reprocess(
                db,
                doc_id,
                tenant_id,
            ),
        }


@router.post("/jobs/retry-failed", status_code=202)
def retry_failed(request: Request):
    """Retry failed jobs only for the current tenant's documents."""
    tenant_id = request.state.tenant_id

    with connect() as db:
        ids = [
            row["document_id"]
            for row in db.execute(
                """
                SELECT j.document_id
                FROM jobs j
                JOIN documents d ON d.id = j.document_id
                WHERE d.tenant_id = ?
                  AND j.status = 'failed'
                  AND j.id = (
                      SELECT MAX(latest.id)
                      FROM jobs latest
                      WHERE latest.document_id = j.document_id
                  )
                ORDER BY j.document_id
                """,
                (tenant_id,),
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
                        tenant_id,
                    ),
                })
            except HTTPException:
                continue

    return {"queued": queued}