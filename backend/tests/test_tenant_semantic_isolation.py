"""Tenant isolation tests for semantic indexing and retrieval."""

from app import semantic, worker
from app.db import connect, now
from app.tenant_auth import hash_api_key
from synth import invoices as S


def _create_tenant(slug, key):
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
            (tenant_id, hash_api_key(key), slug, now()),
        )
    return tenant_id


def _upload(client, key, filename, content):
    return client.post(
        "/api/documents",
        headers={"X-API-Key": key},
        files=[("files", (filename, content, "application/pdf"))],
    )


def _document_ids(rows):
    ids = set()
    for row in rows:
        try:
            value = row["document_id"]
        except (KeyError, TypeError, IndexError):
            continue
        if value is not None:
            ids.add(str(value))
    return ids


def test_semantic_keyword_retrieval_is_tenant_scoped(
    client, tmp_path, spec
):
    key_a = "semantic-isolation-a-key"
    key_b = "semantic-isolation-b-key"

    tenant_a = _create_tenant("semantic-isolation-a", key_a)
    tenant_b = _create_tenant("semantic-isolation-b", key_b)

    pdf_path = S.write_pdf(spec, tmp_path / "semantic-isolation.pdf")
    content = pdf_path.read_bytes()

    response_a = _upload(client, key_a, "semantic-a.pdf", content)
    response_b = _upload(client, key_b, "semantic-b.pdf", content)

    assert response_a.status_code == 202, response_a.text
    assert response_b.status_code == 202, response_b.text

    doc_a = response_a.json()["results"][0]["document_id"]
    doc_b = response_b.json()["results"][0]["document_id"]
    assert doc_a != doc_b

    worker.run_all()

    results_a = semantic.keyword_docs("invoice", tenant_id=tenant_a)
    results_b = semantic.keyword_docs("invoice", tenant_id=tenant_b)

    # keyword_docs returns (document_id, page_no, snippet) tuples.
    ids_a = {str(row[0]) for row in results_a}
    ids_b = {str(row[0]) for row in results_b}

    assert str(doc_a) in ids_a
    assert str(doc_b) in ids_b
    assert str(doc_b) not in ids_a
    assert str(doc_a) not in ids_b


def test_semantic_status_is_tenant_scoped(client):
    key_a = "semantic-status-a-key"
    key_b = "semantic-status-b-key"

    tenant_a = _create_tenant("semantic-status-a", key_a)
    tenant_b = _create_tenant("semantic-status-b", key_b)

    response_a = client.get(
        "/api/semantic/status",
        headers={"X-API-Key": key_a},
    )
    response_b = client.get(
        "/api/semantic/status",
        headers={"X-API-Key": key_b},
    )

    assert response_a.status_code == 200, response_a.text
    assert response_b.status_code == 200, response_b.text

    status_a = semantic.status(tenant_id=tenant_a)
    status_b = semantic.status(tenant_id=tenant_b)

    assert isinstance(status_a, dict)
    assert isinstance(status_b, dict)
