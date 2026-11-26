from app import worker
from synth import invoices as S


def _upload_and_process(client, tmp_path, spec):
    path = S.write_pdf(spec, tmp_path / "business-invoice.pdf")
    response = client.post(
        "/api/documents",
        files=[("files", (path.name, path.read_bytes(), "application/pdf"))],
    )
    assert response.status_code == 202, response.text
    worker.run_all()
    document_id = response.json()["results"][0]["document_id"]
    document = client.get(f"/api/documents/{document_id}").json()
    return document, document["invoices"][0]


def test_supplier_auto_population_dashboard_and_approval(client, tmp_path, spec):
    document, invoice = _upload_and_process(client, tmp_path, spec)

    suppliers = client.get("/api/business/suppliers")
    assert suppliers.status_code == 200
    assert any(row["name"] == invoice["supplier"] for row in suppliers.json()["suppliers"])

    dashboard = client.get("/api/business/dashboard")
    assert dashboard.status_code == 200
    assert dashboard.json()["metrics"]["invoices"] == 1
    assert dashboard.json()["supplier_count"] >= 1

    approval = client.post(
        f"/api/invoices/{invoice['id']}/approval",
        json={"status": "approved", "note": "Synthetic demo invoice reviewed"},
    )
    assert approval.status_code == 200, approval.text
    assert approval.json()["status"] == "approved"
    assert client.get(f"/api/invoices/{invoice['id']}/approval").json()["status"] == "approved"


def test_purchase_order_match_and_mapping_export(client, tmp_path, spec):
    document, invoice = _upload_and_process(client, tmp_path, spec)

    po = client.post(
        "/api/business/purchase-orders",
        json={
            "po_number": "PO-DEMO-001",
            "supplier_name": invoice["supplier"],
            "currency": invoice["currency"],
            "total": invoice["total"],
            "line_items": [],
        },
    )
    assert po.status_code == 200, po.text

    match = client.get(f"/api/invoices/{invoice['id']}/purchase-order-match")
    assert match.status_code == 200
    assert match.json()["status"] == "matched"

    mapping = client.post(
        "/api/business/mappings",
        json={
            "name": "Demo accounting format",
            "mapping": {
                "Vendor Name": "supplier",
                "Invoice Ref": "invoice_number",
                "Gross Amount": "total",
            },
        },
    )
    assert mapping.status_code == 200, mapping.text

    exported = client.get(
        "/api/exports/invoices.csv",
        params={"mapping_id": mapping.json()["id"]},
    )
    assert exported.status_code == 200
    assert "Vendor Name" in exported.text
    assert invoice["supplier"] in exported.text

def test_corrected_downloads_preserve_original_upload(client, tmp_path, spec):
    document, invoice = _upload_and_process(client, tmp_path, spec)
    doc_id = document["id"]
    original_before = client.get(f"/api/documents/{doc_id}/file")
    assert original_before.status_code == 200

    corrected = client.patch(
        f"/api/invoices/{invoice['id']}",
        json={"fields": {"total": "999.99"}, "note": "Synthetic test correction"},
    )
    assert corrected.status_code == 200, corrected.text

    pdf = client.get(f"/api/invoices/{invoice['id']}/corrected.pdf")
    xlsx = client.get(f"/api/invoices/{invoice['id']}/corrected.xlsx")
    original_after = client.get(f"/api/documents/{doc_id}/file")

    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert xlsx.status_code == 200
    assert original_after.content == original_before.content


def test_google_sheets_export_is_explicitly_unavailable_without_credentials(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_SHEETS_SPREADSHEET_ID", raising=False)
    response = client.post("/api/exports/google-sheets")
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]

def test_llm_assisted_invoice_always_requires_human_review(client, tmp_path, spec, monkeypatch):
    from app import pipeline

    def fake_enrichment(result, source_text):
        result["extractor"] = "rules-v1+gemini"
        result["_llm_metadata"] = {
            "model": "gemini-3.5-flash-lite",
            "prompt_version": "invoice-extract-v1",
            "latency_ms": 100,
            "total_tokens": 200,
        }
        return True

    monkeypatch.setattr(pipeline, "enrich_invoice", fake_enrichment)
    document, invoice = _upload_and_process(client, tmp_path, spec)

    assert invoice["needs_review"]
    ai_issue = next(issue for issue in invoice["issues"] if issue["rule_id"] == "AI-REVIEW-001")
    blocked = client.post(
        f"/api/invoices/{invoice['id']}/approval",
        json={"status": "approved"},
    )
    assert blocked.status_code == 409
    resolved = client.post(
        f"/api/issues/{ai_issue['id']}/resolve",
        json={"resolved": True},
    )
    assert resolved.status_code == 200
    approved = client.post(
        f"/api/invoices/{invoice['id']}/approval",
        json={"status": "approved", "note": "AI suggestion verified"},
    )
    assert approved.status_code == 200
