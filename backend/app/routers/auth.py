"""Open user registration and bearer-session authentication."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
import secrets

from ..db import connect, now
from ..user_auth import normalize_email, hash_password, verify_password, issue_session

router = APIRouter()

class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)

@router.post("/auth/register", status_code=201)
def register(body: Credentials):
    email = normalize_email(body.email)
    password_hash = hash_password(body.password)
    with connect() as db:
        if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise HTTPException(409, "An account with this email already exists")
        tenant_slug = "user-" + secrets.token_hex(12)
        tenant = db.execute(
            "INSERT INTO tenants(slug,name,created_at) VALUES(?,?,?)",
            (tenant_slug, email, now()),
        )
        cursor = db.execute(
            """INSERT INTO users(tenant_id,email,password_hash,role,is_active,created_at,updated_at)
               VALUES(?,?,?,'user',1,?,?)""",
            (tenant.lastrowid, email, password_hash, now(), now()),
        )
        token = issue_session(db, cursor.lastrowid)
        return {"access_token": token, "token_type": "bearer", "expires_in": 43200,
                "user": {"id": cursor.lastrowid, "email": email, "role": "user"}}

@router.post("/auth/login")
def login(body: Credentials):
    email = normalize_email(body.email)
    with connect() as db:
        user = db.execute(
            "SELECT id,tenant_id,email,password_hash,role,is_active FROM users WHERE email=?",
            (email,),
        ).fetchone()
        if user is None or not verify_password(body.password, user["password_hash"]):
            raise HTTPException(401, "Invalid email or password")
        if not user["is_active"]:
            raise HTTPException(403, "This account has been disabled")
        token = issue_session(db, user["id"])
        return {"access_token": token, "token_type": "bearer", "expires_in": 43200,
                "user": {"id": user["id"], "email": user["email"], "role": user["role"]}}

@router.get("/auth/me")
def me(request: Request):
    return {"id": request.state.user_id, "tenant_id": request.state.tenant_id,
            "email": request.state.user_email, "role": request.state.user_role}

@router.post("/auth/logout")
def logout(request: Request):
    token = request.headers.get("Authorization", "")[7:].strip()
    import hashlib
    with connect() as db:
        db.execute("UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                   (now(), hashlib.sha256(token.encode()).hexdigest()))
    return {"logged_out": True}
