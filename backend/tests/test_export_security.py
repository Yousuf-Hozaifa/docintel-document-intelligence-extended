def test_export_values_are_safe_from_spreadsheet_formula_injection():
    from app.routers.exports import _safe_cell, _csv_bytes

    record = sample_record()
    record["supplier"] = "=HYPERLINK(\"https://example.invalid\",\"click\")"
    record["filename"] = "  =cmd|' /C calc'!A0"

    csv_text = _csv_bytes([record]).decode("utf-8-sig")
    assert "'=HYPERLINK" in csv_text
    assert "'  =cmd" in csv_text
    assert _safe_cell("-125.00", "total") == "-125.00"
    assert _safe_cell("-not-a-number", "supplier") == "'-not-a-number"
