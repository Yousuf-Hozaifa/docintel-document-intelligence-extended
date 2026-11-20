"""Administrator-only account management and usage overview."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from ..db import connect, now
from ..user_auth import require_admin

router = APIRouter(dependencies=[Depends(require_admin)])

class ActiveUpdate(BaseModel):
    is_active: bool

@router.get("/admin/overview")
def overview():
    with connect() as db:
        users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = db.execute("SELECT COUNT(*) FROM users WHERE is_active=1").fetchone()[0]
        documents = db.execute("SELECT COUNT(*) FROM documents WHERE tenant_id IN (SELECT tenant_id FROM users)").fetchone()[0]
        jobs = db.execute("SELECT COUNT(*) FROM jobs j JOIN documents d ON d.id=j.document_id WHERE d.tenant_id IN (SELECT tenant_id FROM users)").fetchone()[0]
    return {"users": users, "active_users": active, "documents": documents, "jobs": jobs}

@router.get("/admin/users")
def list_users(limit: int = 100, offset: int = 0):
    if limit < 1 or limit > 200 or offset < 0:
        raise HTTPException(422, "Invalid pagination")
    with connect() as db:
        rows = db.execute("""
            SELECT u.id,u.email,u.role,u.is_active,u.created_at,
                   (SELECT COUNT(*) FROM documents d WHERE d.tenant_id=u.tenant_id) AS document_count
            FROM users u ORDER BY u.id LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
    return {"users": [dict(row) for row in rows]}

@router.patch("/admin/users/{user_id}")
def update_user(user_id: int, body: ActiveUpdate):
    with connect() as db:
        target = db.execute("SELECT id,role FROM users WHERE id=?", (user_id,)).fetchone()
        if target is None:
            raise HTTPException(404, "User not found")
        if target["role"] == "admin" and not body.is_active:
            raise HTTPException(409, "Administrator accounts cannot be disabled here")
        db.execute("UPDATE users SET is_active=?,updated_at=? WHERE id=?",
                   (int(body.is_active), now(), user_id))
        if not body.is_active:
            db.execute("UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                       (now(), user_id))
    return {"user_id": user_id, "is_active": body.is_active}
