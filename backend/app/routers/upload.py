"""Document upload API endpoint."""

import hashlib
import uuid

from fastapi import APIRouter, File, UploadFile

from .. import worker
from ..config import get_settings
from ..db import add_event, connect, now
from ..errors import DocumentError
from ..readers import ALLOWED_EXT, detect_type


router = APIRouter()


def _safe_name(name: str) -> str:
    import re

    base = re.sub(
        r"[^\w.\- ]",
        "_",
        (name or "upload").replace("\\", "/").split("/")[-1],
    )
    return base[:150] or "upload"


@router.post("/documents", status_code=202)
async def upload(files: list[UploadFile] = File(...)):
    """Accept multiple files; reject invalid files independently."""
    settings = get_settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)

    results = []

    for file in files:
        name = _safe_name(file.filename)
        ext = (
            "." + name.rsplit(".", 1)[-1].lower()
            if "." in name
            else ""
        )

        if ext not in ALLOWED_EXT:
            results.append({
                "filename": name,
                "status": "rejected",
                "error": f"Unsupported extension '{ext}'",
            })
            continue

        stored = f"{uuid.uuid4().hex}{ext}"
        path = settings.upload_dir / stored

        file_hash = hashlib.sha256()
        size = 0
        too_big = False

        with open(path, "wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)

                if size > settings.max_upload_bytes:
                    too_big = True
                    break

                file_hash.update(chunk)
                output.write(chunk)

        if too_big or size == 0:
            path.unlink(missing_ok=True)

            results.append({
                "filename": name,
                "status": "rejected",
                "error": (
                    "File exceeds the upload size limit"
                    if too_big
                    else "File is empty"
                ),
            })
            continue

        try:
            detect_type(path, name)
        except DocumentError as error:
            path.unlink(missing_ok=True)

            results.append({
                "filename": name,
                "status": "rejected",
                "error": str(error),
            })
            continue

        sha = file_hash.hexdigest()

        with connect() as db:
            existing = db.execute(
                """
                SELECT id, status
                FROM documents
                WHERE sha256=? AND status!='failed'
                ORDER BY id
                LIMIT 1
                """,
                (sha,),
            ).fetchone()

            if existing:
                path.unlink(missing_ok=True)

                results.append({
                    "filename": name,
                    "status": "duplicate",
                    "document_id": existing["id"],
                    "duplicate_of": existing["id"],
                })
                continue

            timestamp = now()

            cursor = db.execute(
                """
                INSERT INTO documents(
                    filename,
                    stored_name,
                    sha256,
                    size_bytes,
                    status,
                    active_generation,
                    next_generation,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    stored,
                    sha,
                    size,
                    "queued",
                    0,
                    1,
                    timestamp,
                    timestamp,
                ),
            )

            document_id = cursor.lastrowid
            job_id = worker.enqueue(db, document_id)

            add_event(
                db,
                document_id,
                "uploaded",
                f"size={size} sha256={sha[:12]}",
            )

        results.append({
            "filename": name,
            "status": "queued",
            "document_id": document_id,
            "job_id": job_id,
        })

    return {"results": results}
