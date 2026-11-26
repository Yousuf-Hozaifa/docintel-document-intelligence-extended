def test_export_values_are_safe_from_spreadsheet_formula_injection():
    from app.routers.exports import _safe_cell, _csv_bytes

    record = {
        "invoice_number": "INV-1",
        "invoice_date": "2026-11-26",
        "supplier": '=HYPERLINK("https://example.invalid","click")',
        "customer": "Demo Customer",
        "subtotal": "100.00",
        "tax": "15.00",
        "total": "-125.00",
        "currency": "BDT",
        "validation_status": "Ready",
        "needs_review": 0,
        "filename": "  =cmd|' /C calc'!A0",
        "correction_count": 0,
        "line_items": [],
    }

    csv_text = _csv_bytes([record]).decode("utf-8-sig")
    assert "'=HYPERLINK" in csv_text
    assert "'  =cmd" in csv_text
    assert _safe_cell("-125.00", "total") == "-125.00"
    assert _safe_cell("-not-a-number", "supplier") == "'-not-a-number"
