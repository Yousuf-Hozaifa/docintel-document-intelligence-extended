import csv
import io

from openpyxl import load_workbook

from app.routers.exports import _csv_bytes, _excel_bytes


def sample_record():
    return {
        "invoice_number": "INV-2026-01",
        "invoice_date": "2026-11-26",
        "supplier": "Kestrel Supplies",
        "customer": "Demo Customer",
        "subtotal": "100.00",
        "tax": "15.00",
        "total": "115.00",
        "currency": "BDT",
        "validation_status": "Ready",
        "needs_review": 0,
        "filename": "sample.pdf",
        "correction_count": 1,
        "line_items": [{
            "idx": 1,
            "description": "Office supplies",
            "quantity": "2",
            "unit_price": "50.00",
            "amount": "100.00",
        }],
    }


def test_invoice_register_csv_contains_corrected_values():
    content = _csv_bytes([sample_record()]).decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(content)))
    assert rows[1][0] == "INV-2026-01"
    assert rows[1][6] == "115.00"
    assert "Office supplies" in rows[1][-1]


def test_invoice_register_xlsx_contains_invoice_data():
    content = _excel_bytes([sample_record()], include_all=True)
    book = load_workbook(io.BytesIO(content), read_only=True)
    sheet = book["Invoice Register"]
    assert sheet["A2"].value == "INV-2026-01"
    assert sheet["G2"].value == "115.00"
    assert sheet["K2"].value == "sample.pdf"


def test_corrected_invoice_xlsx_contains_line_items():
    content = _excel_bytes([sample_record()], include_all=False)
    book = load_workbook(io.BytesIO(content), read_only=True)
    assert book["Invoice Register"]["A2"].value == "INV-2026-01"
    assert book["Line Items"]["B2"].value == "Office supplies"
