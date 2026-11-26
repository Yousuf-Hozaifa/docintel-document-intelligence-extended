"""Tenant-scoped business dashboard, suppliers, approvals, POs and mappings."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..db import add_event, connect, now

router = APIRouter()
MAP_FIELDS = {
    "invoice_number", "invoice_date", "supplier", "customer",
    "subtotal", "tax", "total", "currency",
    "validation_status", "filename", "correction_count",
}


def _tenant(request: Request) -> int:
    value = getattr(request.state, "tenant_id", None)
    if value is None:
        raise HTTPException(401, "Tenant authentication required")
    return value


def _owned_invoice(db, invoice_id: int, tenant_id: int):
    return db.execute(
        """SELECT i.*, d.tenant_id, d.filename
           FROM invoices i JOIN documents d ON d.id=i.document_id
           WHERE i.id=? AND d.tenant_id=?
             AND i.generation_id=d.active_generation""",
        (invoice_id, tenant_id),
    ).fetchone()


class SupplierBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    tax_id: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=60)
    payment_terms: str | None = Field(default=None, max_length=100)


class ApprovalBody(BaseModel):
    status: Literal["pending_review", "approved", "rejected"]
    note: str | None = Field(default=None, max_length=1000)


class PurchaseOrderLine(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    quantity: str | None = None
    unit_price: str | None = None
    amount: str | None = None


class PurchaseOrderBody(BaseModel):
    po_number: str = Field(min_length=1, max_length=100)
    supplier_name: str = Field(min_length=1, max_length=200)
    currency: str | None = Field(default=None, max_length=3)
    total: str | None = None
    line_items: list[PurchaseOrderLine] = Field(default_factory=list, max_length=200)


class MappingBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    mapping: dict[str, str] = Field(min_length=1, max_length=40)


@router.get("/business/dashboard")
def dashboard(request: Request):
    """Return tenant-scoped live operational metrics; currency totals stay separated."""
    tenant_id = _tenant(request)
    with connect() as db:
        counts = db.execute(
            """SELECT
                 COUNT(DISTINCT d.id) AS documents,
                 COUNT(i.id) AS invoices,
                 SUM(CASE WHEN i.needs_review=1 THEN 1 ELSE 0 END) AS needs_review,
                 SUM(CASE WHEN i.id IS NOT NULL AND COALESCE(a.status,'pending_review')='pending_review' THEN 1 ELSE 0 END) AS pending_approval,
                 SUM(CASE WHEN a.status='approved' THEN 1 ELSE 0 END) AS approved,
                 SUM(CASE WHEN a.status='rejected' THEN 1 ELSE 0 END) AS rejected,
                 SUM(CASE WHEN v.open_issues>0 THEN 1 ELSE 0 END) AS invoices_with_open_issues
               FROM documents d
               LEFT JOIN invoices i
                 ON i.document_id=d.id AND i.generation_id=d.active_generation
               LEFT JOIN invoice_approvals a ON a.invoice_id=i.id
               LEFT JOIN (
                 SELECT invoice_id, COUNT(*) AS open_issues
                 FROM validation_issues WHERE resolved=0 GROUP BY invoice_id
               ) v ON v.invoice_id=i.id
               WHERE d.tenant_id=?""",
            (tenant_id,),
        ).fetchone()
        totals = db.execute(
            """SELECT i.currency, COUNT(*) AS invoices, SUM(CAST(i.total AS REAL)) AS total
               FROM invoices i JOIN documents d ON d.id=i.document_id
               WHERE d.tenant_id=? AND i.generation_id=d.active_generation
                 AND i.total IS NOT NULL
               GROUP BY i.currency ORDER BY i.currency""",
            (tenant_id,),
        ).fetchall()
        suppliers = db.execute(
            "SELECT COUNT(*) FROM suppliers WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()[0]
        recent = db.execute(
            """SELECT substr(i.invoice_date,1,7) AS month, i.currency,
                      SUM(CAST(i.total AS REAL)) AS total, COUNT(*) AS count
               FROM invoices i JOIN documents d ON d.id=i.document_id
               WHERE d.tenant_id=? AND i.generation_id=d.active_generation
                 AND i.invoice_date IS NOT NULL
               GROUP BY month, i.currency ORDER BY month DESC LIMIT 18""",
            (tenant_id,),
        ).fetchall()
    return {
        "metrics": {key: (value or 0) for key, value in dict(counts).items()},
        "supplier_count": suppliers,
        "totals_by_currency": [dict(row) for row in totals],
        "monthly_totals": [dict(row) for row in recent],
    }


@router.get("/business/suppliers")
def list_suppliers(request: Request):
    with connect() as db:
        rows = db.execute(
            """SELECT s.id, s.name, s.tax_id, s.email, s.phone, s.payment_terms,
                      COUNT(i.id) AS invoice_count,
                      MAX(i.invoice_date) AS last_invoice_date
               FROM suppliers s
               LEFT JOIN documents d ON d.tenant_id=s.tenant_id
               LEFT JOIN invoices i ON i.document_id=d.id
                    AND i.supplier=s.name
                    AND i.generation_id=d.active_generation
               WHERE s.tenant_id=?
               GROUP BY s.id ORDER BY s.name COLLATE NOCASE""",
            (_tenant(request),),
        ).fetchall()
    return {"suppliers": [dict(row) for row in rows]}


@router.post("/business/suppliers")
def save_supplier(body: SupplierBody, request: Request):
    tenant_id = _tenant(request)
    timestamp = now()
    with connect() as db:
        db.execute(
            """INSERT INTO suppliers(tenant_id,name,tax_id,email,phone,payment_terms,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(tenant_id,name) DO UPDATE SET
                 tax_id=excluded.tax_id, email=excluded.email,
                 phone=excluded.phone, payment_terms=excluded.payment_terms,
                 updated_at=excluded.updated_at""",
            (tenant_id, body.name.strip(), body.tax_id, body.email, body.phone,
             body.payment_terms, timestamp, timestamp),
        )
        row = db.execute(
            "SELECT * FROM suppliers WHERE tenant_id=? AND name=?",
            (tenant_id, body.name.strip()),
        ).fetchone()
    return dict(row)


@router.get("/business/purchase-orders")
def list_purchase_orders(request: Request):
    with connect() as db:
        rows = db.execute(
            """SELECT p.*, COUNT(l.id) AS line_count
               FROM purchase_orders p LEFT JOIN purchase_order_lines l
                 ON l.purchase_order_id=p.id
               WHERE p.tenant_id=? GROUP BY p.id ORDER BY p.id DESC""",
            (_tenant(request),),
        ).fetchall()
    return {"purchase_orders": [dict(row) for row in rows]}


@router.post("/business/purchase-orders")
def create_purchase_order(body: PurchaseOrderBody, request: Request):
    tenant_id = _tenant(request)
    timestamp = now()
    with connect() as db:
        supplier = db.execute(
            "SELECT id FROM suppliers WHERE tenant_id=? AND name=?",
            (tenant_id, body.supplier_name.strip()),
        ).fetchone()
        supplier_id = supplier["id"] if supplier else None
        if supplier is None:
            cur = db.execute(
                """INSERT INTO suppliers(tenant_id,name,created_at,updated_at)
                   VALUES(?,?,?,?)""",
                (tenant_id, body.supplier_name.strip(), timestamp, timestamp),
            )
            supplier_id = cur.lastrowid
        try:
            cur = db.execute(
                """INSERT INTO purchase_orders(
                     tenant_id,supplier_id,po_number,supplier_name,currency,total,status,created_at
                   ) VALUES(?,?,?,?,?,?,'open',?)""",
                (tenant_id, supplier_id, body.po_number.strip(),
                 body.supplier_name.strip(), (body.currency or "").upper() or None,
                 body.total, timestamp),
            )
        except Exception as error:
            if "UNIQUE" in str(error).upper():
                raise HTTPException(409, "Purchase order number already exists") from error
            raise
        po_id = cur.lastrowid
        for idx, line in enumerate(body.line_items, 1):
            db.execute(
                """INSERT INTO purchase_order_lines(
                     purchase_order_id,idx,description,quantity,unit_price,amount
                   ) VALUES(?,?,?,?,?,?)""",
                (po_id, idx, line.description, line.quantity, line.unit_price, line.amount),
            )
    return {"id": po_id, "po_number": body.po_number, "status": "open"}


@router.get("/invoices/{invoice_id}/purchase-order-match")
def match_purchase_order(invoice_id: int, request: Request):
    tenant_id = _tenant(request)
    with connect() as db:
        invoice = _owned_invoice(db, invoice_id, tenant_id)
        if invoice is None:
            raise HTTPException(404, "Invoice not found")
        candidates = db.execute(
            """SELECT p.* FROM purchase_orders p
               WHERE p.tenant_id=? AND lower(trim(p.supplier_name))=lower(trim(?))
               ORDER BY p.id DESC""",
            (tenant_id, invoice["supplier"] or ""),
        ).fetchall()
        invoice_total = invoice["total"]
        results = []
        for po in candidates:
            try:
                amount_match = (
                    invoice_total is not None and po["total"] is not None
                    and abs(Decimal(str(invoice_total)) - Decimal(str(po["total"]))) <= Decimal("0.01")
                )
            except (InvalidOperation, ValueError):
                amount_match = False
            currency_match = (
                (invoice["currency"] or "").upper() == (po["currency"] or "").upper()
            )
            results.append({
                **dict(po),
                "supplier_match": True,
                "amount_match": amount_match,
                "currency_match": currency_match,
                "match_status": "matched" if amount_match and currency_match else "mismatch",
            })
    return {
        "invoice_id": invoice_id,
        "supplier": invoice["supplier"],
        "invoice_total": invoice_total,
        "matches": results,
        "status": "no_candidate" if not results else (
            "matched" if any(row["match_status"] == "matched" for row in results) else "mismatch"
        ),
        "demo_note": "Purchase-order matching compares this invoice with purchase orders entered in this workspace.",
    }


@router.post("/invoices/{invoice_id}/approval")
def update_approval(invoice_id: int, body: ApprovalBody, request: Request):
    tenant_id = _tenant(request)
    actor = getattr(request.state, "user_email", "API-key user")
    timestamp = now()
    with connect() as db:
        invoice = _owned_invoice(db, invoice_id, tenant_id)
        if invoice is None:
            raise HTTPException(404, "Invoice not found")
        if body.status == "approved":
            blockers = db.execute(
                """SELECT COUNT(*) FROM validation_issues
                   WHERE invoice_id=? AND severity='error' AND resolved=0""",
                (invoice_id,),
            ).fetchone()[0]
            if blockers:
                raise HTTPException(
                    409,
                    f"Resolve {blockers} blocking validation error(s) before approval",
                )
        db.execute(
            """INSERT INTO invoice_approvals(
                 tenant_id,invoice_id,status,submitted_by,approved_by,note,updated_at
               ) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(invoice_id) DO UPDATE SET
                 status=excluded.status,
                 approved_by=excluded.approved_by,
                 note=excluded.note,
                 updated_at=excluded.updated_at""",
            (tenant_id, invoice_id, body.status, actor,
             actor if body.status == "approved" else None, body.note, timestamp),
        )
        add_event(
            db,
            invoice["document_id"],
            "invoice_" + body.status,
            f"invoice_id={invoice_id} actor={actor} note={(body.note or '')[:160]}",
        )
    return {"invoice_id": invoice_id, "status": body.status, "updated_at": timestamp}


@router.get("/invoices/{invoice_id}/approval")
def get_approval(invoice_id: int, request: Request):
    with connect() as db:
        invoice = _owned_invoice(db, invoice_id, _tenant(request))
        if invoice is None:
            raise HTTPException(404, "Invoice not found")
        row = db.execute(
            "SELECT status,submitted_by,approved_by,note,updated_at FROM invoice_approvals WHERE invoice_id=?",
            (invoice_id,),
        ).fetchone()
    return dict(row) if row else {"status": "pending_review"}


@router.get("/business/mappings")
def list_mappings(request: Request):
    with connect() as db:
        rows = db.execute(
            "SELECT id,name,mapping_json,created_at,updated_at FROM mapping_templates WHERE tenant_id=? ORDER BY name",
            (_tenant(request),),
        ).fetchall()
    return {
        "mappings": [
            {**dict(row), "mapping": json.loads(row["mapping_json"])}
            for row in rows
        ]
    }


@router.post("/business/mappings")
def create_mapping(body: MappingBody, request: Request):
    tenant_id = _tenant(request)
    mapping = {}
    for column, field in body.mapping.items():
        label = column.strip()
        if not label or len(label) > 100 or field not in MAP_FIELDS:
            raise HTTPException(422, "Each mapping needs a column name and a supported DocIntel field")
        mapping[label] = field
    timestamp = now()
    with connect() as db:
        try:
            cur = db.execute(
                """INSERT INTO mapping_templates(
                     tenant_id,name,mapping_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?)""",
                (tenant_id, body.name.strip(), json.dumps(mapping), timestamp, timestamp),
            )
        except Exception as error:
            if "UNIQUE" in str(error).upper():
                raise HTTPException(409, "A mapping with this name already exists") from error
            raise
    return {"id": cur.lastrowid, "name": body.name, "mapping": mapping}
