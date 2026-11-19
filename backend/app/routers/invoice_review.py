"""Tenant-scoped invoice review and correction API endpoints."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..corrections import (
    CorrectionError,
    InvoicePatch,
    apply_corrections,
    set_issue_resolved,
)
from ..db import connect
from ..document_helpers import _invoice_detail


router = APIRouter()


def _owned_invoice(db, invoice_id: int, tenant_id: int):
    """Return an invoice only when its parent document belongs to this tenant."""
    return db.execute(
        """
        SELECT i.*
        FROM invoices i
        JOIN documents d ON d.id = i.document_id
        WHERE i.id = ?
          AND d.tenant_id = ?
        """,
        (invoice_id, tenant_id),
    ).fetchone()


@router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: int, request: Request):
    """Retrieve an active or archived invoice owned by the current tenant."""
    tenant_id = request.state.tenant_id

    with connect() as db:
        invoice = _owned_invoice(db, invoice_id, tenant_id)

        if invoice is None:
            raise HTTPException(404, "Invoice not found")

        return _invoice_detail(db, invoice)


@router.patch("/invoices/{invoice_id}")
def correct_invoice(
    invoice_id: int,
    patch: InvoicePatch,
    request: Request,
):
    """Correct an active invoice owned by the current tenant."""
    tenant_id = request.state.tenant_id

    try:
        with connect() as db:
            invoice = _owned_invoice(db, invoice_id, tenant_id)

            if invoice is None:
                raise HTTPException(404, "Invoice not found")

            changes = apply_corrections(db, invoice_id, patch)

            invoice = _owned_invoice(db, invoice_id, tenant_id)

            if invoice is None:
                raise HTTPException(404, "Invoice not found")

            output = _invoice_detail(db, invoice)

    except LookupError as error:
        raise HTTPException(404, str(error))

    except CorrectionError as error:
        raise HTTPException(422, str(error))

    output["changes_applied"] = changes
    return output


@router.get("/invoices/{invoice_id}/corrections")
def list_corrections(invoice_id: int, request: Request):
    """Return correction history only for an invoice owned by this tenant."""
    tenant_id = request.state.tenant_id

    with connect() as db:
        exists = _owned_invoice(db, invoice_id, tenant_id)

        if exists is None:
            raise HTTPException(404, "Invoice not found")

        corrections = db.execute(
            """
            SELECT id, field, old_value, new_value,
                   note, created_at
            FROM corrections
            WHERE invoice_id = ?
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


@router.post("/issues/{issue_id}/resolve")
def resolve_issue(
    issue_id: int,
    request: Request,
    body: ResolveBody | None = None,
):
    """Resolve or reopen an issue only when its invoice belongs to this tenant."""
    tenant_id = request.state.tenant_id

    try:
        with connect() as db:
            owned_issue = db.execute(
                """
                SELECT v.id
                FROM validation_issues v
                JOIN invoices i ON i.id = v.invoice_id
                JOIN documents d ON d.id = i.document_id
                WHERE v.id = ?
                  AND d.tenant_id = ?
                """,
                (issue_id, tenant_id),
            ).fetchone()

            if owned_issue is None:
                raise HTTPException(404, "Issue not found")

            invoice_id = set_issue_resolved(
                db,
                issue_id,
                (body or ResolveBody()).resolved,
            )

            invoice = _owned_invoice(db, invoice_id, tenant_id)

            if invoice is None:
                raise HTTPException(404, "Invoice not found")

            return _invoice_detail(db, invoice)

    except LookupError as error:
        raise HTTPException(404, str(error))

    except CorrectionError as error:
        raise HTTPException(422, str(error))