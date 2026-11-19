
"""Tenant-aware API-key authentication and legacy-key bootstrap."""

import hashlib
import hmac
import os

from fastapi import HTTPException, Request

from .db import connect, now


LEGACY_TENANT_ID = 1
LEGACY_TENANT_SLUG = "legacy"


def hash_api_key(api_key: str) -> str:
    """Return the SHA-256 hash of an API key."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def bootstrap_legacy_api_key() -> None:
    """
    Register the legacy environment API key for the legacy tenant.

    Only the hash is stored in the database.
    """
    api_key = os.environ.get("DOCINTEL_API_KEY", "").strip()

    if not api_key:
        return

    key_hash = hash_api_key(api_key)

    with connect() as db:
        tenant = db.execute(
            "SELECT id FROM tenants WHERE slug = ?",
            (LEGACY_TENANT_SLUG,),
        ).fetchone()

        if tenant is None:
            raise RuntimeError(
                "Legacy tenant is missing; run database migrations first."
            )

        db.execute(
            """
            INSERT OR IGNORE INTO tenant_api_keys (
                tenant_id,
                key_hash,
                name,
                created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                tenant["id"],
                key_hash,
                "Legacy environment API key",
                now(),
            ),
        )


def _authentication_required() -> bool:
    """
    Require authentication when explicitly enabled or when a legacy
    API key has been configured.
    """
    value = os.environ.get(
        "DOCINTEL_REQUIRE_API_KEY",
        "0",
    ).strip().lower()

    explicitly_required = value in {"1", "true", "yes", "on"}

    legacy_key_configured = bool(
        os.environ.get("DOCINTEL_API_KEY", "").strip()
    )

    return explicitly_required or legacy_key_configured


def require_tenant(request: Request) -> int | None:
    """
    Authenticate the request and store its tenant ID on request.state.

    Health checks and CORS preflight requests remain unauthenticated.
    Registered API keys are resolved through their database records.
    The legacy environment key is also supported for compatibility.
    """
    # Public health endpoint and CORS preflight.
    if (
        request.url.path == "/api/health"
        or request.method == "OPTIONS"
    ):
        request.state.tenant_id = None
        return None

    api_key = request.headers.get("X-API-Key", "").strip()

    # Handle requests without an API key.
    if not api_key:
        if _authentication_required():
            raise HTTPException(
                status_code=401,
                detail="Missing X-API-Key header",
            )

        # Local development compatibility: use the legacy tenant.
        with connect() as db:
            tenant = db.execute(
                "SELECT id FROM tenants WHERE slug = ?",
                (LEGACY_TENANT_SLUG,),
            ).fetchone()

        if tenant is None:
            raise HTTPException(
                status_code=503,
                detail="Tenant database is not initialized",
            )

        request.state.tenant_id = tenant["id"]
        return tenant["id"]

    supplied_hash = hash_api_key(api_key)

    # Look up the supplied key by its hash.
    with connect() as db:
        row = db.execute(
            """
            SELECT tenant_id, revoked_at
            FROM tenant_api_keys
            WHERE key_hash = ?
            """,
            (supplied_hash,),
        ).fetchone()

        legacy_tenant = db.execute(
            "SELECT id FROM tenants WHERE slug = ?",
            (LEGACY_TENANT_SLUG,),
        ).fetchone()

    # Registered API keys must not be revoked.
    if row is not None:
        if row["revoked_at"] is not None:
            raise HTTPException(
                status_code=401,
                detail="Invalid or revoked X-API-Key",
            )

        request.state.tenant_id = row["tenant_id"]
        return row["tenant_id"]

    # Backward compatibility with DOCINTEL_API_KEY.
    # This also supports tests or environments that configure the key
    # after the application has already started.
    legacy_key = os.environ.get(
        "DOCINTEL_API_KEY",
        "",
    ).strip()

    if (
        legacy_key
        and hmac.compare_digest(api_key, legacy_key)
        and legacy_tenant is not None
    ):
        request.state.tenant_id = legacy_tenant["id"]
        return legacy_tenant["id"]

    # Reject unknown or invalid keys.
    raise HTTPException(
        status_code=401,
        detail="Invalid or revoked X-API-Key",
    )
