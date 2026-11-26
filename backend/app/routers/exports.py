"""Tenant-scoped corrected invoice and invoice-register exports."""
from __future__ import annotations

import csv
import io
import json

import fitz
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from openpyxl import Workbook

from ..db import connect
from ..google_sheets import GoogleSheetsUnavailable, append_rows

router = APIRouter()

FIELDS = (
    "invoice_number", "invoice_date", "supplier", "customer",
    "subtotal", "tax", "total", "currency",
)
HEADERS = (
    "Invoice Number", "Invoice Date", "Supplier", "Customer",
    "Subtotal", "Tax", "Total", "Currency", "Validation Status",
    "Review Required", "Document", "Correction Count",
)


def _tenant_id(request: Request) -> int:
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(401, "Tenant authentication required")
    return tenant_id


def _records(db, tenant_id: int, invoice_id: int | None = None):
    query = """
        SELECT i.id AS invoice_id, i.document_id, i.seq, i.fields_json,
               i.needs_review, d.filename,
               (SELECT COUNT(*) FROM corrections c WHERE c.invoice_id=i.id)
                   AS correction_count,
               (SELECT COUNT(*) FROM validation_issues v
                WHERE v.invoice_id=i.id AND v.resolved=0) AS open_issues
        FROM invoices i
        JOIN documents d ON d.id=i.document_id
        WHERE d.tenant_id=? AND i.generation_id=d.active_generation
    """
    args = [tenant_id]
    if invoice_id is not None:
        query += " AND i.id=?"
        args.append(invoice_id)
    query += " ORDER BY d.id DESC, i.seq"
    rows = db.execute(query, args).fetchall()
    if invoice_id is not None and not rows:
        raise HTTPException(404, "Invoice not found")
    output = []
    for row in rows:
        fields = json.loads(row["fields_json"])
        values = {name: (fields.get(name) or {}).get("value") for name in FIELDS}
        items = [
            dict(item)
            for item in db.execute(
                """SELECT idx, description, quantity, unit_price, amount
                   FROM line_items WHERE invoice_id=? ORDER BY idx""",
                (row["invoice_id"],),
            )
        ]
        output.append({
            **dict(row),
            **values,
            "line_items": items,
            "validation_status": "Needs review" if row["needs_review"] else "Ready",
        })
    return output


def _excel_bytes(records, include_all: bool, mapping: dict | None = None) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "Invoice Register"
    headers = list(mapping.keys()) if mapping else list(HEADERS)
    sheet.append(headers)
    for record in records:
        if mapping:
            sheet.append([record.get(field) for field in mapping.values()])
        else:
            sheet.append([
                record.get(name) for name in FIELDS
            ] + [
                record["validation_status"],
                "Yes" if record["needs_review"] else "No",
                record["filename"],
                record["correction_count"],
            ])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column in sheet.columns:
        letter = column[0].column_letter
        sheet.column_dimensions[letter].width = min(
            max(len(str(cell.value or "")) for cell in column) + 2, 36
        )
    if not include_all:
        detail = book.create_sheet("Line Items")
        detail.append(["Line", "Description", "Quantity", "Unit Price", "Amount"])
        for record in records:
            for item in record["line_items"]:
                detail.append([
                    item["idx"], item["description"], item["quantity"],
                    item["unit_price"], item["amount"],
                ])
        detail.freeze_panes = "A2"
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


def _csv_bytes(records, mapping: dict | None = None) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    if mapping:
        writer.writerow(list(mapping.keys()))
        for record in records:
            writer.writerow([record.get(field) for field in mapping.values()])
    else:
        writer.writerow(list(HEADERS) + ["Line Items"])
        for record in records:
            writer.writerow(
                [record.get(name) for name in FIELDS]
                + [
                    record["validation_status"],
                    "Yes" if record["needs_review"] else "No",
                    record["filename"],
                    record["correction_count"],
                    json.dumps(record["line_items"], ensure_ascii=False),
                ]
            )
    return output.getvalue().encode("utf-8-sig")


def _load_mapping(db, tenant_id: int, mapping_id: int | None):
    if mapping_id is None:
        return None
    row = db.execute(
        "SELECT mapping_json FROM mapping_templates WHERE id=? AND tenant_id=?",
        (mapping_id, tenant_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Mapping template not found")
    return json.loads(row["mapping_json"])


@router.get("/exports/invoices.csv")
def export_csv(request: Request, mapping_id: int | None = None):
    """Export the current tenant's active invoice register as CSV."""
    tenant_id = _tenant_id(request)
    with connect() as db:
        records = _records(db, tenant_id)
        mapping = _load_mapping(db, tenant_id, mapping_id)
    return Response(
        _csv_bytes(records, mapping),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="docintel-invoices.csv"'},
    )


@router.get("/exports/invoices.xlsx")
def export_xlsx(request: Request, mapping_id: int | None = None):
    """Export the current tenant's active invoice register as XLSX."""
    tenant_id = _tenant_id(request)
    with connect() as db:
        records = _records(db, tenant_id)
        mapping = _load_mapping(db, tenant_id, mapping_id)
    return Response(
        _excel_bytes(records, include_all=True, mapping=mapping),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="docintel-invoices.xlsx"'},
    )


@router.post("/exports/google-sheets")
def export_google_sheets(request: Request, mapping_id: int | None = None):
    """Append the active invoice register to a configured tenant-authorized sheet."""
    tenant_id = _tenant_id(request)
    with connect() as db:
        records = _records(db, tenant_id)
        mapping = _load_mapping(db, tenant_id, mapping_id)

    if mapping:
        values = [list(mapping.keys())]
        values.extend([
            [record.get(field) for field in mapping.values()]
            for record in records
        ])
    else:
        values = [list(HEADERS) + ["Line Items"]]
        for record in records:
            values.append(
                [record.get(name) for name in FIELDS]
                + [
                    record["validation_status"],
                    "Yes" if record["needs_review"] else "No",
                    record["filename"],
                    record["correction_count"],
                    json.dumps(record["line_items"], ensure_ascii=False),
                ]
            )

    try:
        result = append_rows(values)
    except GoogleSheetsUnavailable as error:
        raise HTTPException(503, str(error)) from error
    return {"exported_rows": len(records), **result}


@router.get("/invoices/{invoice_id}/corrected.xlsx")
def corrected_xlsx(invoice_id: int, request: Request):
    """Download corrected structured invoice data without altering the original."""
    with connect() as db:
        record = _records(db, _tenant_id(request), invoice_id)[0]
    filename = f"invoice-{invoice_id}-corrected.xlsx"
    return Response(
        _excel_bytes([record], include_all=False),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/invoices/{invoice_id}/corrected.pdf")
def corrected_pdf(invoice_id: int, request: Request):
    """Generate a corrected invoice summary PDF; this is not a reconstruction of the source layout."""
    with connect() as db:
        record = _records(db, _tenant_id(request), invoice_id)[0]

    lines = [
        "DOCINTEL — CORRECTED INVOICE SUMMARY",
        f"Source document: {record['filename']}",
        f"Invoice number: {record.get('invoice_number') or 'Not provided'}",
        f"Invoice date: {record.get('invoice_date') or 'Not provided'}",
        f"Supplier: {record.get('supplier') or 'Not provided'}",
        f"Customer: {record.get('customer') or 'Not provided'}",
        f"Subtotal: {record.get('subtotal') or 'Not provided'}",
        f"Tax: {record.get('tax') or 'Not provided'}",
        f"Total: {record.get('total') or 'Not provided'}",
        f"Currency: {record.get('currency') or 'Not provided'}",
        f"Review status: {record['validation_status']}",
        "",
        "LINE ITEMS",
    ]
    for item in record["line_items"]:
        lines.append(
            f"{item.get('idx')}. {item.get('description') or ''} | "
            f"Qty: {item.get('quantity') or ''} | "
            f"Unit: {item.get('unit_price') or ''} | "
            f"Amount: {item.get('amount') or ''}"
        )
    lines.extend([
        "",
        "This is a newly generated corrected-data summary, not an edited copy",
        "of the original invoice layout. The original upload remains unchanged.",
    ])

    pdf = fitz.open()
    for offset in range(0, max(len(lines), 1), 36):
        page = pdf.new_page(width=595, height=842)
        page.insert_text((48, 48), "DocIntel", fontsize=16, fontname="helv")
        page.insert_textbox(
            fitz.Rect(48, 76, 547, 790),
            "\n".join(lines[offset:offset + 36]),
            fontsize=10,
            fontname="helv",
            lineheight=1.5,
        )
    content = pdf.tobytes()
    pdf.close()
    filename = f"invoice-{invoice_id}-corrected.pdf"
    return Response(
        content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
