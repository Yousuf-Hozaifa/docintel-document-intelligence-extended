
"""FastAPI application: upload, status, results, search."""


import hmac
import json
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
from .config import get_settings
from .logging_config import configure_logging
from .db import connect, init_db
from .routers.health import router as health_router
from .routers.upload import router as upload_router
from .routers.documents import router as documents_router
from .routers.invoice_review import router as invoice_review_router
from .routers.search import router as search_router
from .routers.jobs import router as jobs_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    init_db()
    worker.start()
    yield
    worker.stop()


def require_key(
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    """Optional shared-secret auth; health checks stay open."""
    expected = os.environ.get("DOCINTEL_API_KEY")

    if (
        not expected
        or request.url.path == "/api/health"
        or request.method == "OPTIONS"
    ):
        return

    if not x_api_key or not hmac.compare_digest(
        x_api_key.encode(),
        expected.encode(),
    ):
        raise HTTPException(401, "Missing or invalid X-API-Key header")


app = FastAPI(
    title="Care Guide Document Intelligence API",
    version="0.2.0",
    lifespan=lifespan,
    dependencies=[Depends(require_key)],
)

app.add_middleware(
    CORSMiddleware,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key"],
    allow_origins=[
        origin.strip()
        for origin in os.environ.get(
            "DOCINTEL_CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173",
        ).split(",")
        if origin.strip()
    ],
)
app.include_router(health_router, prefix="/api")
app.include_router(upload_router, prefix="/api")
app.include_router(documents_router, prefix="/api")
app.include_router(invoice_review_router, prefix="/api")
app.include_router(search_router, prefix="/api")
app.include_router(jobs_router, prefix="/api")

# ------------------------------------------------------------------
# Documents
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# Keyword search
# ------------------------------------------------------------------