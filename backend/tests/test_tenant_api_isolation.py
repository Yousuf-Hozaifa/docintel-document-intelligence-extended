"""Tenant isolation tests for invoice, issue, job and search APIs."""

import json

from app import worker
from app.db import connect
from synth import invoices as S
from app.tenant_auth import hash_api_key
from app.db import now


def _create_tenant(slug, api_key):
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO tenants (slug, name, created_at) VALUES (?, ?, ?)",
            (slug, slug.title(), now()),
        )
        tenant_id = cursor.lastrowid
        db.execute(
            """
            INSERT INTO tenant_api_keys
                (tenant_id, key_hash, name, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (tenant_id, hash_api_key(api_key), slug, now()),
        )
    return tenant_id


def _upload(client, key, filename, content):
    return client.post(
        "/api/documents",
        headers={"X-API-Key": key},
        files=[("files", (filename, content, "application/pdf"))],
    )


def _returned_document_ids(response):
    """Collect document_id values from structured API response data."""
    found = []

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "document_id":
                    found.append(str(item))
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(response.json())
    return found


def _create_two_tenant_documents(client, tmp_path, spec):
    key_a = "isolation-tenant-a-key"
    key_b = "isolation-tenant-b-key"

    _create_tenant("isolation-a", key_a)
    _create_tenant("isolation-b", key_b)

    pdf_path = S.write_pdf(spec, tmp_path / "isolation-invoice.pdf")
    content = pdf_path.read_bytes()

    response_a = _upload(client, key_a, "tenant-a.pdf", content)
    response_b = _upload(client, key_b, "tenant-b.pdf", content)

    assert response_a.status_code == 202, response_a.text
    assert response_b.status_code == 202, response_b.text

    doc_a = response_a.json()["results"][0]["document_id"]
    doc_b = response_b.json()["results"][0]["document_id"]
    assert doc_a != doc_b

    worker.run_all()

    return key_a, key_b, doc_a, doc_b


def test_invoice_correction_and_history_are_tenant_scoped(
    client, tmp_path, spec
):
    key_a, key_b, doc_a, doc_b = _create_two_tenant_documents(
        client, tmp_path, spec
    )
    headers_a = {"X-API-Key": key_a}
    headers_b = {"X-API-Key": key_b}

    with connect() as db:
        invoice_a = db.execute(
            "SELECT id, fields_json FROM invoices WHERE document_id = ?",
            (doc_a,),
        ).fetchone()
        invoice_b = db.execute(
            "SELECT id FROM invoices WHERE document_id = ?",
            (doc_b,),
        ).fetchone()

    assert invoice_a is not None
    assert invoice_b is not None

    invoice_a_id = invoice_a["id"]
    invoice_b_id = invoice_b["id"]

    # Tenant A cannot retrieve or read the correction history of B's invoice.
    assert client.get(
        f"/api/invoices/{invoice_b_id}", headers=headers_a
    ).status_code == 404

    assert client.get(
        f"/api/invoices/{invoice_b_id}/corrections", headers=headers_a
    ).status_code == 404

    # Tenant A cannot modify B's invoice.
    foreign_patch = client.patch(
        f"/api/invoices/{invoice_b_id}",
        headers=headers_a,
        json={
            "fields": {"customer": "UNAUTHORIZED TENANT CHANGE"},
            "note": "cross-tenant security test",
        },
    )
    assert foreign_patch.status_code == 404

    # Confirm B's invoice and correction history remain unchanged.
    with connect() as db:
        corrections = db.execute(
            "SELECT COUNT(*) FROM corrections WHERE invoice_id = ?",
            (invoice_b_id,),
        ).fetchone()[0]
        current_b = db.execute(
            "SELECT fields_json FROM invoices WHERE id = ?",
            (invoice_b_id,),
        ).fetchone()

    assert corrections == 0
    assert "UNAUTHORIZED TENANT CHANGE" not in current_b["fields_json"]

    # Tenant A can access its own invoice.
    own_invoice = client.get(
        f"/api/invoices/{invoice_a_id}", headers=headers_a
    )
    assert own_invoice.status_code == 200

    # Tenant B can access its own invoice.
    assert client.get(
        f"/api/invoices/{invoice_b_id}", headers=headers_b
    ).status_code == 200


def test_issue_resolution_is_tenant_scoped(client, tmp_path, spec):
    key_a, key_b, doc_a, doc_b = _create_two_tenant_documents(
        client, tmp_path, spec
    )

    with connect() as db:
        issue = db.execute(
            """
            SELECT v.id, v.resolved
            FROM validation_issues v
            JOIN invoices i ON i.id = v.invoice_id
            WHERE i.document_id = ?
              AND v.severity IN ('error', 'warning')
            ORDER BY v.id
            LIMIT 1
            """,
            (doc_b,),
        ).fetchone()

    if issue is None:
        # No resolvable warning/error was produced by this fixture.
        return

    issue_id = issue["id"]
    original_resolved = issue["resolved"]

    response = client.post(
        f"/api/issues/{issue_id}/resolve",
        headers={"X-API-Key": key_a},
        json={"resolved": True},
    )
    assert response.status_code == 404

    with connect() as db:
        after = db.execute(
            "SELECT resolved FROM validation_issues WHERE id = ?",
            (issue_id,),
        ).fetchone()

    assert after["resolved"] == original_resolved

    # The owning tenant can access the issue-resolution endpoint.
    own_response = client.post(
        f"/api/issues/{issue_id}/resolve",
        headers={"X-API-Key": key_b},
        json={"resolved": True},
    )
    assert own_response.status_code == 200


def test_job_listing_and_reprocess_are_tenant_scoped(
    client, tmp_path, spec
):
    key_a, key_b, doc_a, doc_b = _create_two_tenant_documents(
        client, tmp_path, spec
    )

    headers_a = {"X-API-Key": key_a}
    headers_b = {"X-API-Key": key_b}

    jobs_a = client.get("/api/jobs", headers=headers_a)
    jobs_b = client.get("/api/jobs", headers=headers_b)

    assert jobs_a.status_code == 200, jobs_a.text
    assert jobs_b.status_code == 200, jobs_b.text

    # A's job response must not disclose B's document ID.
    assert str(doc_b) not in _returned_document_ids(jobs_a)
    assert str(doc_a) not in _returned_document_ids(jobs_b)

    # Cross-tenant reprocessing must be rejected.
    foreign_reprocess = client.post(
        f"/api/documents/{doc_b}/reprocess",
        headers=headers_a,
    )
    assert foreign_reprocess.status_code == 404

    # The owner can request reprocessing.
    own_reprocess = client.post(
        f"/api/documents/{doc_b}/reprocess",
        headers=headers_b,
    )
    assert own_reprocess.status_code == 202


def test_keyword_search_does_not_return_other_tenant_documents(
    client, tmp_path, spec
):
    key_a, key_b, doc_a, doc_b = _create_two_tenant_documents(
        client, tmp_path, spec
    )

    response_a = client.get(
        "/api/search",
        params={"q": "invoice"},
        headers={"X-API-Key": key_a},
    )
    response_b = client.get(
        "/api/search",
        params={"q": "invoice"},
        headers={"X-API-Key": key_b},
    )

    assert response_a.status_code == 200, response_a.text
    assert response_b.status_code == 200, response_b.text

    assert str(doc_b) not in _returned_document_ids(response_a)
    assert str(doc_a) not in _returned_document_ids(response_b)
