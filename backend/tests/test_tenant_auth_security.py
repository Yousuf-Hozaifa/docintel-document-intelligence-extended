"""Security tests for tenant API-key lifecycle and authentication."""

from app.db import connect, now
from app.tenant_auth import (
    LEGACY_TENANT_ID,
    bootstrap_legacy_api_key,
    hash_api_key,
)


def _register_key(slug, api_key, revoked_at=None):
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
            INSERT INTO tenant_api_keys
                (tenant_id, key_hash, name, created_at, revoked_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (tenant_id, hash_api_key(api_key), slug, now(), revoked_at),
        )

    return tenant_id


def test_revoked_registered_tenant_key_is_rejected(client):
    key = "revoked-tenant-security-key"
    _register_key("revoked-security-tenant", key, revoked_at=now())

    response = client.get(
        "/api/documents",
        headers={"X-API-Key": key},
    )

    assert response.status_code == 401


def test_unknown_api_key_is_rejected(client):
    response = client.get(
        "/api/documents",
        headers={"X-API-Key": "unknown-unregistered-security-key"},
    )

    assert response.status_code == 401


def test_explicit_authentication_requirement_without_legacy_key(
    client, monkeypatch
):
    monkeypatch.delenv("DOCINTEL_API_KEY", raising=False)
    monkeypatch.setenv("DOCINTEL_REQUIRE_API_KEY", "true")

    response = client.get("/api/documents")

    assert response.status_code == 401


def test_legacy_key_bootstrap_stores_hash_and_is_idempotent(
    monkeypatch,
):
    key = "legacy-bootstrap-security-key"
    monkeypatch.setenv("DOCINTEL_API_KEY", key)

    bootstrap_legacy_api_key()
    bootstrap_legacy_api_key()

    with connect() as db:
        rows = db.execute(
            """
            SELECT tenant_id, key_hash, name
            FROM tenant_api_keys
            WHERE key_hash = ?
            """,
            (hash_api_key(key),),
        ).fetchall()

        total = db.execute(
            """
            SELECT COUNT(*)
            FROM tenant_api_keys
            WHERE key_hash = ?
            """,
            (hash_api_key(key),),
        ).fetchone()[0]

    assert len(rows) == 1
    assert total == 1
    assert rows[0]["tenant_id"] == LEGACY_TENANT_ID
    assert rows[0]["key_hash"] == hash_api_key(key)
    assert rows[0]["key_hash"] != key
    assert key not in rows[0]["name"]


def test_revoked_bootstrapped_legacy_key_cannot_use_fallback(
    client, monkeypatch
):
    key = "revoked-legacy-fallback-security-key"
    monkeypatch.setenv("DOCINTEL_API_KEY", key)

    bootstrap_legacy_api_key()

    with connect() as db:
        db.execute(
            """
            UPDATE tenant_api_keys
            SET revoked_at = ?
            WHERE key_hash = ?
            """,
            (now(), hash_api_key(key)),
        )

    response = client.get(
        "/api/documents",
        headers={"X-API-Key": key},
    )

    assert response.status_code == 401
