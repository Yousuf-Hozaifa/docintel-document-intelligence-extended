"""Cross-document verification, duplicates, corrections, resolve, jobs/reprocess, auth, registers."""
import copy
import random
from decimal import Decimal

import openpyxl
import pymupdf
import pytest

from app import worker
from synth import invoices as S
from synth.invoices import random_spec
from tests.test_api import upload


def variant_pdf(spec, path):
    """Same content, different bytes (so it is not caught by the identical-file check)."""
    S.write_pdf(spec, path)
    tmp = path.with_suffix(".tmp")
    d = pymupdf.open(path); d.set_metadata({"title": f"copy-{path.name}"}); d.save(str(tmp)); d.close()
    path.unlink(); tmp.rename(path)
    return path


def register_xlsx(path, rows):
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Register"
    ws.append(["Invoice Register Q1"]); ws.append([])
    ws.append(["Invoice No", "Date", "Supplier", "Customer", "Subtotal", "Tax", "Total", "Currency"])
    for r in rows:
        ws.append(r)
    ws.append(["Total", None, None, None, None, None, 999999, None])
    wb.save(path)
    return path


def reg_row(s, total=None):
    return [s.number, s.date_iso, s.supplier, s.customer, float(s.subtotal), float(s.tax), float(total if total is not None else s.total), s.currency]


def docs(client):
    out = {}
    for d in client.get("/api/documents").json()["documents"]:
        out[d["filename"]] = client.get(f"/api/documents/{d['id']}").json()
    return out


def rules(doc, i=0):
    return [x["rule_id"] for x in doc["invoices"][i]["issues"]]


# ------------------------------------------------------------------ duplicates (symmetric) & cross-verification
def test_duplicates_are_flagged_on_both_files(client, tmp_path, spec):
    upload(client, variant_pdf(spec, tmp_path / "a.pdf"))
    worker.run_all()
    assert docs(client)["a.pdf"]["review_required"] is False
    upload(client, variant_pdf(spec, tmp_path / "b.pdf"))
    worker.run_all()
    d = docs(client)
    for name in ("a.pdf", "b.pdf"):
        assert "VAL-DUP-001" in rules(d[name]) and "VAL-XDOC-001" not in rules(d[name])
        assert d[name]["review_required"] is True


def test_conflicting_copies_report_which_values_differ(client, tmp_path, spec):
    other = copy.deepcopy(spec)
    other.items = [(d, q, u + Decimal("1.00")) for d, q, u in spec.items]
    upload(client, S.write_pdf(spec, tmp_path / "a.pdf"), S.write_pdf(other, tmp_path / "b.pdf"))
    worker.run_all()
    d = docs(client)
    msg = [i["message"] for i in d["a.pdf"]["invoices"][0]["issues"] if i["rule_id"] == "VAL-XDOC-001"]
    assert msg and "total" in msg[0] and "b.pdf" in msg[0]
    assert "VAL-DUP-001" in rules(d["b.pdf"]) and "VAL-XDOC-001" in rules(d["b.pdf"])


def test_register_sheet_yields_one_invoice_per_row(client, tmp_path):
    specs = [random_spec(random.Random(i), 100 + i, 2) for i in range(4)]
    upload(client, register_xlsx(tmp_path / "reg.xlsx", [reg_row(s) for s in specs]))
    worker.run_all()
    d = docs(client)["reg.xlsx"]
    assert d["status"] == "completed" and len(d["invoices"]) == 4          # the "Total" summary row is skipped
    assert [i["invoice_number"] for i in d["invoices"]] == [s.number for s in specs]
    assert d["invoices"][0]["total"] == str(specs[0].total)
    assert "VAL-REQ-002" not in rules(d)                                   # registers have no line items by design


def test_pdf_verified_against_excel_register(client, tmp_path):
    specs = [random_spec(random.Random(i), 200 + i, 3) for i in range(3)]
    upload(client, S.write_pdf(specs[1], tmp_path / "inv.pdf"), register_xlsx(tmp_path / "reg.xlsx", [reg_row(s) for s in specs]))
    worker.run_all()
    d = docs(client)
    assert "VAL-XDOC-002" in rules(d["inv.pdf"]) and d["inv.pdf"]["review_required"] is False
    matched = [i for i in d["reg.xlsx"]["invoices"] if i["invoice_number"] == specs[1].number][0]
    assert "VAL-XDOC-002" in [x["rule_id"] for x in matched["issues"]]


def test_pdf_excel_mismatch_is_an_error_on_both_sides(client, tmp_path):
    s = random_spec(random.Random(5), 300, 3)
    upload(client, S.write_pdf(s, tmp_path / "inv.pdf"), register_xlsx(tmp_path / "reg.xlsx", [reg_row(s, total=s.total + 50)]))
    worker.run_all()
    d = docs(client)
    for name in ("inv.pdf", "reg.xlsx"):
        iss = [i for i in d[name]["invoices"][0]["issues"] if i["rule_id"] == "VAL-XDOC-001"]
        assert iss and iss[0]["severity"] == "error" and "total" in iss[0]["message"]
        assert d[name]["review_required"] is True


def test_same_number_from_different_suppliers_is_not_a_match(client, tmp_path, spec):
    other = copy.deepcopy(spec); other.supplier = "Completely Different Vendor GmbH"
    upload(client, S.write_pdf(spec, tmp_path / "a.pdf"), S.write_pdf(other, tmp_path / "b.pdf"))
    worker.run_all()
    for d in docs(client).values():
        assert "VAL-DUP-001" not in rules(d)


# ------------------------------------------------------------------ corrections
def tampered_pdf(tmp_path, seed=3):
    spec = random_spec(random.Random(seed), 5, 2)
    p = S.write_pdf(spec, tmp_path / "w.pdf")
    doc = pymupdf.open(p); pg = doc[0]
    for r in pg.search_for(f"{spec.total:.2f}"):
        pg.add_redact_annot(r, text="999.99", fontsize=10)
    pg.apply_redactions(); doc.saveIncr()
    return spec, p


def first_invoice(client):
    d = client.get("/api/documents").json()["documents"][0]
    return client.get(f"/api/documents/{d['id']}").json()["invoices"][0]


def test_correcting_a_value_revalidates_audits_and_clears_review(client, tmp_path):
    spec, p = tampered_pdf(tmp_path)
    upload(client, p); worker.run_all()
    inv = first_invoice(client)
    assert "VAL-SUB-001" in [i["rule_id"] for i in inv["issues"]] and inv["needs_review"]
    r = client.patch(f"/api/invoices/{inv['id']}", json={"fields": {"total": f"{spec.total:,.2f}"}, "note": "typo in OCR"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["changes_applied"] == 1 and out["total"] == str(spec.total)          # "1,234.50" normalised
    assert out["issues"] == [] and out["needs_review"] is False
    f = out["fields"]["total"]
    assert f["source"] == "manual" and f["original_value"] == "999.99" and f["confidence"] == 1.0
    audit = client.get(f"/api/invoices/{inv['id']}/corrections").json()["corrections"]
    assert [(a["field"], a["old_value"], a["new_value"], a["note"]) for a in audit] == [("total", "999.99", str(spec.total), "typo in OCR")]
    assert client.get("/api/documents").json()["documents"][0]["review_required"] is False
    assert client.patch(f"/api/invoices/{inv['id']}", json={"fields": {"total": str(spec.total)}}).json()["changes_applied"] == 0
    assert len(client.get(f"/api/invoices/{inv['id']}/corrections").json()["corrections"]) == 1


def test_correction_validation_errors(client, tmp_path, spec):
    upload(client, S.write_pdf(spec, tmp_path / "a.pdf")); worker.run_all()
    inv = first_invoice(client)
    assert client.patch(f"/api/invoices/{inv['id']}", json={"fields": {"nonsense": "1"}}).status_code == 422
    assert client.patch("/api/invoices/9999", json={"fields": {"total": "1"}}).status_code == 404
    bad = client.patch(f"/api/invoices/{inv['id']}", json={"fields": {"invoice_date": "2024-13-45", "currency": "xyz", "tax": "abc"}}).json()
    got = {(i["rule_id"], i["field"]) for i in bad["issues"]}
    assert {("VAL-DATE-001", "invoice_date"), ("VAL-CUR-001", "currency"), ("VAL-NUM-001", "tax")} <= got
    assert bad["currency"] == "XYZ"


def test_line_items_can_be_edited_added_and_deleted(client, tmp_path, spec):
    upload(client, S.write_pdf(spec, tmp_path / "a.pdf")); worker.run_all()
    inv = first_invoice(client)
    gone = inv["line_items"][0]
    r = client.patch(f"/api/invoices/{inv['id']}", json={"line_items": [{"idx": gone["idx"], "delete": True}]}).json()
    assert "VAL-LINE-002" in [i["rule_id"] for i in r["issues"]] and len(r["line_items"]) == 2   # sum no longer matches subtotal
    r = client.patch(f"/api/invoices/{inv['id']}", json={"line_items": [
        {"description": gone["description"], "quantity": gone["quantity"], "unit_price": gone["unit_price"], "amount": gone["amount"]}]}).json()
    assert r["issues"] == [] and len(r["line_items"]) == 3
    r = client.patch(f"/api/invoices/{inv['id']}", json={"line_items": [{"idx": r["line_items"][0]["idx"], "quantity": "99"}]}).json()
    assert "VAL-LINE-001" in [i["rule_id"] for i in r["issues"]]
    fields = [c["field"] for c in client.get(f"/api/invoices/{inv['id']}/corrections").json()["corrections"]]
    assert fields[0].startswith("line_items[") and len(fields) == 3


def test_resolving_a_warning_clears_review_and_survives_revalidation(client, tmp_path, spec):
    upload(client, S.write_pdf(spec, tmp_path / "a.pdf")); worker.run_all()
    inv = first_invoice(client)
    r = client.patch(f"/api/invoices/{inv['id']}", json={"fields": {"tax": None}}).json()
    warn = [i for i in r["issues"] if i["rule_id"] == "VAL-SUB-002"][0]
    assert r["needs_review"] is True
    r = client.post(f"/api/issues/{warn['id']}/resolve", json={"resolved": True}).json()
    assert r["needs_review"] is False and client.get("/api/documents").json()["documents"][0]["review_required"] is False
    r = client.patch(f"/api/invoices/{inv['id']}", json={"fields": {"customer": "Renamed Customer"}}).json()
    assert [i["resolved"] for i in r["issues"] if i["rule_id"] == "VAL-SUB-002"] == [True] and r["needs_review"] is False
    current = [i for i in r["issues"] if i["rule_id"] == "VAL-SUB-002"][0]
    reopened = client.post(f"/api/issues/{current['id']}/resolve", json={"resolved": False}).json()
    assert reopened["needs_review"] is True
    assert client.post("/api/issues/99999/resolve").status_code == 404


def test_info_results_cannot_be_resolved(client, tmp_path):
    s = random_spec(random.Random(8), 400, 2)
    upload(client, S.write_pdf(s, tmp_path / "i.pdf"), register_xlsx(tmp_path / "r.xlsx", [reg_row(s)])); worker.run_all()
    d = docs(client)["i.pdf"]
    info = [i for i in d["invoices"][0]["issues"] if i["severity"] == "info"][0]
    assert client.post(f"/api/issues/{info['id']}/resolve").status_code == 422


def test_correcting_invoice_number_removes_stale_duplicate_flags_on_the_other_file(client, tmp_path, spec):
    upload(client, variant_pdf(spec, tmp_path / "a.pdf"), variant_pdf(spec, tmp_path / "b.pdf")); worker.run_all()
    d = docs(client)
    assert d["a.pdf"]["review_required"] and d["b.pdf"]["review_required"]
    inv_b = d["b.pdf"]["invoices"][0]
    client.patch(f"/api/invoices/{inv_b['id']}", json={"fields": {"invoice_number": "INV-CORRECTED-1"}})
    d = docs(client)
    assert "VAL-DUP-001" not in rules(d["a.pdf"]) and d["a.pdf"]["review_required"] is False
    assert "VAL-DUP-001" not in rules(d["b.pdf"])


# ------------------------------------------------------------------ jobs / reprocessing
def corrupt_pdf(tmp_path, name="bad.pdf"):
    p = tmp_path / name; p.write_bytes(b"%PDF-1.4\n" + b"x" * 60); return p


def test_failed_jobs_are_listed_and_reprocessing_is_guarded(client, tmp_path):
    upload(client, corrupt_pdf(tmp_path)); worker.run_all()
    failed = client.get("/api/jobs", params={"status": "failed"}).json()["jobs"]
    assert len(failed) == 1 and failed[0]["error_code"] == "corrupt_file" and failed[0]["filename"] == "bad.pdf"
    doc_id = failed[0]["document_id"]
    assert client.post(f"/api/documents/{doc_id}/reprocess").status_code == 202
    assert client.post(f"/api/documents/{doc_id}/reprocess").status_code == 409        # active job already queued
    assert client.get(f"/api/documents/{doc_id}").json()["status"] == "queued"
    worker.run_all()
    d = client.get(f"/api/documents/{doc_id}").json()
    assert d["status"] == "failed" and [h["event"] for h in d["history"]].count("reprocess_requested") == 1
    assert client.post("/api/documents/999/reprocess").status_code == 404


def test_transient_failure_then_successful_reprocess(client, tmp_path, spec, monkeypatch):
    from app import pipeline
    real = pipeline.process_document
    monkeypatch.setattr(worker, "process_document", lambda *a, **k: (_ for _ in ()).throw(OSError("disk offline")))
    upload(client, S.write_pdf(spec, tmp_path / "a.pdf")); worker.run_all()
    doc = client.get("/api/documents").json()["documents"][0]
    assert doc["status"] == "failed"
    monkeypatch.setattr(worker, "process_document", real)
    assert client.post("/api/jobs/retry-failed").json()["queued"][0]["document_id"] == doc["id"]
    worker.run_all()
    after = client.get(f"/api/documents/{doc['id']}").json()
    assert after["status"] == "completed" and after["invoices"][0]["invoice_number"] == spec.number

def test_failed_reprocess_preserves_previous_successful_results(
    client, tmp_path, spec, monkeypatch
):
    from app import pipeline

    # First processing succeeds.
    upload(client, S.write_pdf(spec, tmp_path / "stable.pdf"))
    worker.run_all()

    doc = client.get("/api/documents").json()["documents"][0]
    doc_id = doc["id"]
    before = client.get(f"/api/documents/{doc_id}").json()

    assert before["status"] == "completed"
    assert len(before["invoices"]) == 1

    original_invoice = before["invoices"][0]
    original_invoice_id = original_invoice["id"]

    assert original_invoice["invoice_number"] == spec.number

    # Save a manual correction before attempting reprocessing.
    correction_response = client.patch(
        f"/api/invoices/{original_invoice_id}",
        json={
            "fields": {"customer": "Preserved Customer"},
            "note": "Verify correction survives failed reprocessing",
        },
    )
    assert correction_response.status_code == 200

    correction_history_before = client.get(
        f"/api/invoices/{original_invoice_id}/corrections"
    ).json()["corrections"]

    assert len(correction_history_before) == 1
    assert correction_history_before[0]["new_value"] == "Preserved Customer"

    # Queue a reprocessing attempt.
    response = client.post(f"/api/documents/{doc_id}/reprocess")
    assert response.status_code == 202

    # Simulate a failure after the first page has been read.
    real_open_pages = pipeline.open_pages

    def failing_open_pages(path, kind):
        total, pages = real_open_pages(path, kind)

        def interrupted_pages():
            iterator = iter(pages)
            yield next(iterator)
            raise OSError("Simulated disk failure during reprocessing")

        return total, interrupted_pages()

    monkeypatch.setattr(
        pipeline,
        "open_pages",
        failing_open_pages,
    )

    worker.run_all()

    # Previous successful results must remain active.
    after = client.get(f"/api/documents/{doc_id}").json()

    assert after["status"] == "completed"
    assert len(after["invoices"]) == 1
    assert after["invoices"][0]["id"] == original_invoice_id
    assert after["invoices"][0]["invoice_number"] == spec.number

    # The processing attempt must independently report failure.
    assert after["job"]["status"] == "failed"

    # Previous correction history must remain available.
    correction_history_after = client.get(
        f"/api/invoices/{original_invoice_id}/corrections"
    ).json()["corrections"]

    assert correction_history_after == correction_history_before

    # Failed staging data must be discarded.
    from app.db import connect

    with connect() as db:
        generation_state = db.execute(
            """
            SELECT active_generation, processing_generation
            FROM documents
            WHERE id = ?
            """,
            (doc_id,),
        ).fetchone()

        assert generation_state["processing_generation"] is None

        assert db.execute(
            """
            SELECT COUNT(*)
            FROM pages
            WHERE document_id = ?
              AND generation_id != ?
            """,
            (doc_id, generation_state["active_generation"]),
        ).fetchone()[0] == 0

        assert db.execute(
            """
            SELECT COUNT(*)
            FROM invoices
            WHERE document_id = ?
              AND generation_id != ?
            """,
            (doc_id, generation_state["active_generation"]),
        ).fetchone()[0] == 0

def test_reprocess_discards_results_and_keeps_cross_flags_consistent(client, tmp_path, spec):
    upload(client, variant_pdf(spec, tmp_path / "a.pdf"), variant_pdf(spec, tmp_path / "b.pdf")); worker.run_all()
    ids = {d["filename"]: d["id"] for d in client.get("/api/documents").json()["documents"]}
    client.post(f"/api/documents/{ids['b.pdf']}/reprocess"); worker.run_all()
    d = docs(client)
    assert d["a.pdf"]["review_required"] and d["b.pdf"]["review_required"] and len(d["b.pdf"]["invoices"]) == 1


# ------------------------------------------------------------------ auth
def test_api_key_is_enforced_when_configured(client, monkeypatch):
    monkeypatch.setenv("DOCINTEL_API_KEY", "s3cret")
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/documents").status_code == 401
    assert client.get("/api/documents", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/documents", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert client.post("/api/jobs/retry-failed").status_code == 401


def test_api_is_open_without_a_configured_key(client):
    assert client.get("/api/documents").status_code == 200
