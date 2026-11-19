"""Invoice correction and review API endpoints."""

from fastapi import APIRouter, HTTPException
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


@router.get("/invoices/{invoice_id}")
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


@router.patch("/invoices/{invoice_id}")
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


@router.get("/invoices/{invoice_id}/corrections")
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


@router.post("/issues/{issue_id}/resolve")
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
