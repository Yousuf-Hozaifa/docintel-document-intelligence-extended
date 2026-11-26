
"""FastAPI application: upload, status, results, search."""


import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import worker
from .backup_scheduler import (
    BackupGateMiddleware,
    _backup_config,
    backup_enabled,
    backup_gate,
    scheduler_loop,
)
from .config import get_settings
from .logging_config import configure_logging
from .db import connect, init_db
from .routers.health import router as health_router
from .routers.upload import router as upload_router
from .routers.documents import router as documents_router
from .routers.invoice_review import router as invoice_review_router
from .routers.search import router as search_router
from .routers.jobs import router as jobs_router
from .tenant_auth import bootstrap_legacy_api_key, require_tenant
from .user_auth import bootstrap_admin
from .routers.auth import router as auth_router
from .routers.admin import router as admin_router
from .routers.exports import router as exports_router
from .routers.business import router as business_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    scheduler_task = None
    try:
        # Validate persistent storage before init_db can create files.
        if backup_enabled():
            _backup_config(require_mount=True)

        init_db()
        bootstrap_legacy_api_key()
        bootstrap_admin()
        worker.start()

        if backup_enabled():
            scheduler_task = asyncio.create_task(
                scheduler_loop(), name="docintel-backup-scheduler"
            )
        yield
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass
        worker.stop()

app = FastAPI(
    title="Care Guide Document Intelligence API",
    version="0.2.0",
    lifespan=lifespan,
    dependencies=[Depends(require_tenant)],
)

app.add_middleware(BackupGateMiddleware, gate=backup_gate)

app.add_middleware(
    CORSMiddleware,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key", "Authorization"],
    allow_origins=[
        origin.strip()
        for origin in os.environ.get(
            "DOCINTEL_CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173",
        ).split(",")
        if origin.strip()
    ],
)
app.include_router(auth_router, prefix="/api")
app.include_router(admin_router, prefix="/api")
app.include_router(health_router, prefix="/api")
app.include_router(upload_router, prefix="/api")
app.include_router(documents_router, prefix="/api")
app.include_router(invoice_review_router, prefix="/api")
app.include_router(exports_router, prefix="/api")
app.include_router(business_router, prefix="/api")
app.include_router(search_router, prefix="/api")
app.include_router(jobs_router, prefix="/api")

# ------------------------------------------------------------------
# Documents
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# Keyword search
# ------------------------------------------------------------------
