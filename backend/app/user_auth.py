"""Password hashing, user sessions, and administrator bootstrap."""
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from .db import connect, now

SESSION_HOURS = 12
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

def normalize_email(email: str) -> str:
    value = email.strip().lower()
    if len(value) > 254 or not EMAIL_RE.fullmatch(value):
        raise HTTPException(422, "Enter a valid email address")
    return value

def hash_password(password: str) -> str:
    if len(password) < 12:
        raise HTTPException(422, "Password must contain at least 12 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + salt.hex() + "$" + digest.hex()

def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = encoded.split("$", 2)
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
        return hmac.compare_digest(actual.hex(), digest_hex)
    except (ValueError, TypeError):
        return False

def issue_session(db, user_id: int) -> str:
    token = secrets.token_urlsafe(48)
    expires = (datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)).isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO auth_sessions(user_id, token_hash, created_at, expires_at) VALUES(?,?,?,?)",
        (user_id, hashlib.sha256(token.encode()).hexdigest(), now(), expires),
    )
    return token

def authenticate_bearer(request: Request) -> int | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[7:].strip()
    if not token or len(token) > 256:
        raise HTTPException(401, "Invalid session")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with connect() as db:
        row = db.execute("""
            SELECT u.id, u.tenant_id, u.email, u.role, u.is_active
            FROM auth_sessions s JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=? AND s.revoked_at IS NULL
              AND s.expires_at > ? 
        """, (token_hash, now())).fetchone()
    if row is None or not row["is_active"]:
        raise HTTPException(401, "Session expired or account disabled")
    request.state.user_id = row["id"]
    request.state.user_email = row["email"]
    request.state.user_role = row["role"]
    request.state.tenant_id = row["tenant_id"]
    return row["tenant_id"]

def require_admin(request: Request) -> None:
    if getattr(request.state, "user_role", None) != "admin":
        raise HTTPException(403, "Administrator access required")

def bootstrap_admin() -> None:
    """Create the configured first admin; never promote an existing user."""
    email = os.environ.get("DOCINTEL_ADMIN_BOOTSTRAP_EMAIL", "").strip()
    password = os.environ.get("DOCINTEL_ADMIN_BOOTSTRAP_PASSWORD", "")
    if not email and not password:
        return
    if not email or not password:
        raise RuntimeError("Set both DOCINTEL_ADMIN_BOOTSTRAP_EMAIL and DOCINTEL_ADMIN_BOOTSTRAP_PASSWORD")
    normalized = normalize_email(email)
    # Hash only when an account needs to be created.
    with connect() as db:
        existing = db.execute("SELECT role FROM users WHERE email=?", (normalized,)).fetchone()
        if existing:
            if existing["role"] != "admin":
                raise RuntimeError("Configured bootstrap email already belongs to a non-admin account")
            return
        tenant_slug = "user-" + secrets.token_hex(12)
        tenant = db.execute(
            "INSERT INTO tenants(slug,name,created_at) VALUES(?,?,?)",
            (tenant_slug, "DocIntel Administrator", now()),
        )
        db.execute(
            """INSERT INTO users(tenant_id,email,password_hash,role,is_active,created_at,updated_at)
               VALUES(?,?,?,'admin',1,?,?)""",
            (tenant.lastrowid, normalized, hash_password(password), now(), now()),
        )
