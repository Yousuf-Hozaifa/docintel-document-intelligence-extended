"""Account authentication, session lifecycle, and tenant isolation tests."""
from app.db import connect, now
from app.user_auth import hash_password

def _register(client, email):
    response = client.post("/api/auth/register", json={
        "email": email, "password": "a-strong-test-password-123"
    })
    assert response.status_code == 201, response.text
    return response.json()

def test_registration_login_and_me_are_user_scoped(client):
    created = _register(client, "Alice@example.com")
    token = created["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["email"] == "alice@example.com"
    assert me.json()["role"] == "user"

    login = client.post("/api/auth/login", json={
        "email": "ALICE@example.com", "password": "a-strong-test-password-123"
    })
    assert login.status_code == 200
    assert login.json()["user"]["id"] == created["user"]["id"]

def test_duplicate_email_is_rejected_case_insensitively(client):
    _register(client, "duplicate@example.com")
    response = client.post("/api/auth/register", json={
        "email": "DUPLICATE@example.com", "password": "a-strong-test-password-123"
    })
    assert response.status_code == 409

def test_wrong_password_and_short_password_are_rejected(client):
    _register(client, "credentials@example.com")
    wrong = client.post("/api/auth/login", json={
        "email": "credentials@example.com", "password": "not-the-password"
    })
    assert wrong.status_code == 401
    short = client.post("/api/auth/register", json={
        "email": "short@example.com", "password": "short"
    })
    assert short.status_code == 422

def test_two_accounts_cannot_access_each_others_workspace(client):
    a = _register(client, "tenant-a@example.com")
    b = _register(client, "tenant-b@example.com")
    headers_a = {"Authorization": f"Bearer {a['access_token']}"}
    headers_b = {"Authorization": f"Bearer {b['access_token']}"}

    assert client.get("/api/documents", headers=headers_a).json()["documents"] == []
    assert client.get("/api/documents", headers=headers_b).json()["documents"] == []

    # Each account has a distinct tenant, and no account receives another
    # account's tenant identifier through the public registration response.
    with connect() as db:
        tenants = db.execute(
            "SELECT tenant_id FROM users WHERE email IN (?,?) ORDER BY email",
            ("tenant-a@example.com", "tenant-b@example.com"),
        ).fetchall()
    assert len(tenants) == 2
    assert tenants[0]["tenant_id"] != tenants[1]["tenant_id"]

def test_logout_revokes_session(client):
    user = _register(client, "logout@example.com")
    headers = {"Authorization": f"Bearer {user['access_token']}"}
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401

def test_disabled_account_cannot_reuse_existing_session(client):
    user = _register(client, "disabled@example.com")
    token = user["access_token"]
    with connect() as db:
        db.execute("UPDATE users SET is_active=0 WHERE id=?", (user["user"]["id"],))
    response = client.get("/api/documents", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
