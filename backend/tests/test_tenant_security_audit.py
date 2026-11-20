"""Focused security and concurrency regression tests for tenant isolation."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
import time

from app import semantic, worker
from app.db import connect, now
from app.tenant_auth import bootstrap_legacy_api_key, hash_api_key


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


def test_revoked_registered_key_is_rejected(client):
    key = "audit-revoked-key"
    tenant_id = _create_tenant("audit-revoked", key)

    with connect() as db:
        db.execute(
            """
            UPDATE tenant_api_keys
            SET revoked_at = ?
            WHERE tenant_id = ? AND key_hash = ?
            """,
            (now(), tenant_id, hash_api_key(key)),
        )

    response = client.get(
        "/api/documents",
        headers={"X-API-Key": key},
    )
    assert response.status_code == 401


def test_unknown_api_key_is_rejected_even_when_auth_is_optional(
    client, monkeypatch
):
    monkeypatch.delenv("DOCINTEL_API_KEY", raising=False)
    monkeypatch.delenv("DOCINTEL_REQUIRE_API_KEY", raising=False)

    response = client.get(
        "/api/documents",
        headers={"X-API-Key": "not-a-registered-key"},
    )
    assert response.status_code == 401


def test_explicit_api_key_requirement_rejects_missing_key(
    client, monkeypatch
):
    monkeypatch.delenv("DOCINTEL_API_KEY", raising=False)
    monkeypatch.setenv("DOCINTEL_REQUIRE_API_KEY", "true")

    response = client.get("/api/documents")
    assert response.status_code == 401
    assert "Missing" in response.json()["detail"]


def test_explicit_api_key_requirement_accepts_registered_key(
    client, monkeypatch
):
    monkeypatch.delenv("DOCINTEL_API_KEY", raising=False)
    monkeypatch.setenv("DOCINTEL_REQUIRE_API_KEY", "1")

    key = "audit-required-valid-key"
    _create_tenant("audit-required-valid", key)

    response = client.get(
        "/api/documents",
        headers={"X-API-Key": key},
    )
    assert response.status_code == 200


def test_legacy_key_bootstrap_stores_only_hash_and_is_idempotent(
    client, monkeypatch
):
    raw_key = "audit-legacy-bootstrap-secret"
    monkeypatch.setenv("DOCINTEL_API_KEY", raw_key)

    bootstrap_legacy_api_key()
    bootstrap_legacy_api_key()

    expected_hash = hash_api_key(raw_key)

    with connect() as db:
        rows = db.execute(
            """
            SELECT key_hash, name, revoked_at
            FROM tenant_api_keys
            WHERE key_hash = ?
            """,
            (expected_hash,),
        ).fetchall()

        assert len(rows) == 1
        assert rows[0]["key_hash"] == expected_hash
        assert raw_key not in repr(dict(rows[0]))

    response = client.get(
        "/api/documents",
        headers={"X-API-Key": raw_key},
    )
    assert response.status_code == 200


def _insert_completed_document(db, tenant_id, filename):
    cursor = db.execute(
        """
        INSERT INTO documents (
            filename, stored_name, sha256, size_bytes,
            status, created_at, updated_at, tenant_id
        )
        VALUES (?, ?, ?, ?, 'completed', ?, ?, ?)
        """,
        (
            filename,
            filename,
            f"hash-{filename}",
            1,
            now(),
            now(),
            tenant_id,
        ),
    )
    return cursor.lastrowid


def test_semantic_reindex_is_tenant_scoped_and_progress_is_private(
    monkeypatch,
):
    key_a = "audit-reindex-tenant-a"
    key_b = "audit-reindex-tenant-b"
    tenant_a = _create_tenant("audit-reindex-a", key_a)
    tenant_b = _create_tenant("audit-reindex-b", key_b)

    with connect() as db:
        doc_a = _insert_completed_document(
            db, tenant_a, "reindex-a.pdf"
        )
        _insert_completed_document(
            db, tenant_b, "reindex-b.pdf"
        )

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    processed = []

    def fake_embed_document(document_id, generation_id):
        processed.append(document_id)
        started.set()
        assert release.wait(5), "Timed out waiting to release reindex"

    monkeypatch.setattr(semantic, "enabled", lambda: True)
    monkeypatch.setattr(
        semantic, "get_embedder",
        lambda: SimpleNamespace(name="audit-test-model"),
    )
    monkeypatch.setattr(
        semantic, "embed_document", fake_embed_document
    )

    with semantic._reindex_lock:
        semantic._reindex_state.update(
            running=False, done=0, total=0, error=None
        )
        semantic._reindex_tenant_id = None

    try:
        assert semantic.start_reindex(tenant_id=tenant_a) is True
        assert started.wait(5), "Reindex thread did not start"

        # A second tenant cannot start an overlapping global reindex.
        assert semantic.start_reindex(tenant_id=tenant_b) is False

        # Tenant B must not see tenant A's reindex progress.
        status_b = semantic.status(tenant_id=tenant_b)
        assert status_b["reindex"] == {
            "running": False,
            "done": 0,
            "total": 0,
            "error": None,
        }

    finally:
        release.set()

    # Wait for the background thread to finish its state update.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with semantic._reindex_lock:
            if not semantic._reindex_state["running"]:
                finished.set()
                break
        time.sleep(0.01)

    assert finished.is_set(), "Reindex thread did not finish"
    assert processed == [doc_a]

    with semantic._reindex_lock:
        assert semantic._reindex_state["done"] == 1
        semantic._reindex_tenant_id = None


def test_concurrent_workers_claim_a_queued_job_only_once():
    with connect() as db:
        cursor = db.execute(
            """
            INSERT INTO documents (
                filename, stored_name, sha256, size_bytes,
                created_at, updated_at
            )
            VALUES ('concurrent.pdf', 'concurrent.pdf', 'concurrent-hash',
                    1, 'test', 'test')
            """
        )
        document_id = cursor.lastrowid
        worker.enqueue(db, document_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: worker._claim(), range(8)))

    successful_claims = [claim for claim in claims if claim is not None]

    assert len(successful_claims) == 1
    assert successful_claims[0][1] == document_id
