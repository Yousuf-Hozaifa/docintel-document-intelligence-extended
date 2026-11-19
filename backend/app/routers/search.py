"""Keyword and semantic document search API endpoints."""

import re

from fastapi import APIRouter, HTTPException, Query

from .. import semantic
from ..db import connect


router = APIRouter()


def _fts_query(query: str) -> str | None:
    tokens = re.findall(r"\w+", query, flags=re.UNICODE)

    return (
        " ".join(
            f'"{token}"' + ("*" if index == len(tokens) - 1 else "")
            for index, token in enumerate(tokens)
        )
        or None
    )


@router.get("/search")
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
                               '<mark>', '</mark>', ' â€¦ ', 14
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


@router.get("/semantic/status")
def semantic_status():
    return semantic.status()


@router.post("/semantic/reindex", status_code=202)
def semantic_reindex():
    """Start background indexing of active documents."""
    try:
        started = semantic.start_reindex()

    except semantic.SemanticDisabled as error:
        raise HTTPException(503, str(error))

    if not started:
        raise HTTPException(409, "A reindex is already running")

    return semantic.status()["reindex"]


@router.get("/search/semantic")
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
