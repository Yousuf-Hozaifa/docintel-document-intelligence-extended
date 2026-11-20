"""Administrator bootstrap and authorization tests."""
from app.db import connect
from app.user_auth import bootstrap_admin

def test_public_registration_never_grants_admin_role(client):
    response = client.post("/api/auth/register", json={
        "email": "first-user@example.com", "password": "a-strong-test-password-123"
    })
    assert response.status_code == 201
    assert response.json()["user"]["role"] == "user"

def test_bootstrap_admin_can_manage_accounts(client, monkeypatch):
    monkeypatch.setenv("DOCINTEL_ADMIN_BOOTSTRAP_EMAIL", "owner@example.com")
    monkeypatch.setenv("DOCINTEL_ADMIN_BOOTSTRAP_PASSWORD", "a-very-strong-admin-password-123")
    bootstrap_admin()
    login = client.post("/api/auth/login", json={
        "email": "owner@example.com", "password": "a-very-strong-admin-password-123"
    })
    assert login.status_code == 200
    headers = {"Authorization": "Bearer " + login.json()["access_token"]}
    assert client.get("/api/admin/overview", headers=headers).status_code == 200

    regular = client.post("/api/auth/register", json={
        "email": "member@example.com", "password": "a-strong-test-password-123"
    }).json()
    member_headers = {"Authorization": "Bearer " + regular["access_token"]}
    assert client.get("/api/admin/users", headers=member_headers).status_code == 403

    member_id = regular["user"]["id"]
    disabled = client.patch(
        f"/api/admin/users/{member_id}", headers=headers, json={"is_active": False}
    )
    assert disabled.status_code == 200
    assert client.get("/api/documents", headers=member_headers).status_code == 401

def test_bootstrap_admin_reuses_legacy_tenant(client, monkeypatch):
    monkeypatch.setenv("DOCINTEL_ADMIN_BOOTSTRAP_EMAIL", "owner-legacy@example.com")
    monkeypatch.setenv("DOCINTEL_ADMIN_BOOTSTRAP_PASSWORD", "a-very-strong-admin-password-123")
    with connect() as db:
        legacy_id = db.execute("SELECT id FROM tenants WHERE slug='legacy'").fetchone()["id"]
    bootstrap_admin()
    with connect() as db:
        owner_tenant = db.execute(
            "SELECT tenant_id FROM users WHERE email='owner-legacy@example.com'"
        ).fetchone()["tenant_id"]
    assert owner_tenant == legacy_id
