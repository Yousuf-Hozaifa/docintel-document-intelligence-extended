
"""Security tests for tenant-scoped document operations."""

from app import worker
from app.db import connect, now
from app.tenant_auth import hash_api_key
from synth import invoices as S


def create_tenant(slug: str, api_key: str) -> int:
    """Create a tenant and register its hashed API key."""
    with connect() as db:
        cursor = db.execute(
            """
            INSERT INTO tenants (slug, name, created_at)
            VALUES (?, ?, ?)
            """,
            (slug, slug.title(), now()),
        )
        tenant_id = cursor.lastrowid

        db.execute(
            """
            INSERT INTO tenant_api_keys (
                tenant_id,
                key_hash,
                name,
                created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                tenant_id,
                hash_api_key(api_key),
                f"{slug} test key",
                now(),
            ),
        )

    return tenant_id


def upload_pdf(client, api_key: str, filename: str, content: bytes):
    """Upload a PDF using a tenant API key."""
    return client.post(
        "/api/documents",
        headers={"X-API-Key": api_key},
        files=[
            ("files", (filename, content, "application/pdf")),
        ],
    )


def test_tenant_document_access_and_duplicate_isolation(
    client,
    tmp_path,
    spec,
):
    """A tenant cannot access another tenant's documents or files."""

    key_a = "tenant-a-test-key"
    key_b = "tenant-b-test-key"

    create_tenant("tenant-a", key_a)
    create_tenant("tenant-b", key_b)

    headers_a = {"X-API-Key": key_a}
    headers_b = {"X-API-Key": key_b}

    pdf_path = S.write_pdf(spec, tmp_path / "shared-invoice.pdf")
    pdf_content = pdf_path.read_bytes()

    # Tenant A uploads the document.
    response_a = upload_pdf(
        client,
        key_a,
        "tenant-a-invoice.pdf",
        pdf_content,
    )
    assert response_a.status_code == 202

    result_a = response_a.json()["results"][0]
    assert result_a["status"] == "queued"
    document_a = result_a["document_id"]

    # The same tenant cannot create a second copy.
    duplicate_a = upload_pdf(
        client,
        key_a,
        "tenant-a-duplicate.pdf",
        pdf_content,
    )
    assert duplicate_a.status_code == 202

    duplicate_result = duplicate_a.json()["results"][0]
    assert duplicate_result["status"] == "duplicate"
    assert duplicate_result["duplicate_of"] == document_a

    # Tenant B can upload identical bytes independently.
    response_b = upload_pdf(
        client,
        key_b,
        "tenant-b-invoice.pdf",
        pdf_content,
    )
    assert response_b.status_code == 202

    result_b = response_b.json()["results"][0]
    assert result_b["status"] == "queued"
    document_b = result_b["document_id"]

    assert document_b != document_a

    # Process both tenants' queued documents.
    worker.run_all()

    # Each tenant sees only its own document.
    documents_a = client.get(
        "/api/documents",
        headers=headers_a,
    ).json()["documents"]

    documents_b = client.get(
        "/api/documents",
        headers=headers_b,
    ).json()["documents"]

    assert [d["id"] for d in documents_a] == [document_a]
    assert [d["id"] for d in documents_b] == [document_b]

    # Tenant A cannot view, download, or delete Tenant B's document.
    assert client.get(
        f"/api/documents/{document_b}",
        headers=headers_a,
    ).status_code == 404

    assert client.get(
        f"/api/documents/{document_b}/file",
        headers=headers_a,
    ).status_code == 404

    assert client.delete(
        f"/api/documents/{document_b}",
        headers=headers_a,
    ).status_code == 404

    # Tenant B retains access to its own document and original file.
    detail_b = client.get(
        f"/api/documents/{document_b}",
        headers=headers_b,
    )
    assert detail_b.status_code == 200

    file_b = client.get(
        f"/api/documents/{document_b}/file",
        headers=headers_b,
    )
    assert file_b.status_code == 200
    assert file_b.content == pdf_content

    # Tenant A can delete its own document.
    deleted_a = client.delete(
        f"/api/documents/{document_a}",
        headers=headers_a,
    )
    assert deleted_a.status_code == 200

    # Deleting A's document does not affect B.
    assert client.get(
        f"/api/documents/{document_b}",
        headers=headers_b,
    ).status_code == 200

    assert client.get(
        "/api/documents",
        headers=headers_a,
    ).json()["documents"] == []


def test_cross_tenant_invoices_do_not_trigger_duplicate_issues(
    client,
    tmp_path,
    spec,
):
    """Matching invoice numbers in different tenants are not duplicates."""

    key_a = "cross-tenant-a-key"
    key_b = "cross-tenant-b-key"

    create_tenant("cross-tenant-a", key_a)
    create_tenant("cross-tenant-b", key_b)

    pdf_path = S.write_pdf(spec, tmp_path / "same-invoice.pdf")
    content = pdf_path.read_bytes()

    response_a = upload_pdf(
        client,
        key_a,
        "invoice-a.pdf",
        content,
    )
    response_b = upload_pdf(
        client,
        key_b,
        "invoice-b.pdf",
        content,
    )

    assert response_a.status_code == 202
    assert response_b.status_code == 202

    result_a = response_a.json()["results"][0]
    result_b = response_b.json()["results"][0]

    assert result_a["status"] == "queued"
    assert result_b["status"] == "queued"

    worker.run_all()

    detail_a = client.get(
        f"/api/documents/{result_a['document_id']}",
        headers={"X-API-Key": key_a},
    )
    detail_b = client.get(
        f"/api/documents/{result_b['document_id']}",
        headers={"X-API-Key": key_b},
    )

    assert detail_a.status_code == 200
    assert detail_b.status_code == 200

    for detail in (detail_a.json(), detail_b.json()):
        assert detail["invoices"]

        issue_ids = {
            issue["rule_id"]
            for invoice in detail["invoices"]
            for issue in invoice["issues"]
        }

        assert "VAL-DUP-001" not in issue_ids
        assert "VAL-XDOC-001" not in issue_ids
        assert "VAL-XDOC-002" not in issue_ids
