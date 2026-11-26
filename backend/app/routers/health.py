"""Health and validation-rules API endpoints."""

from fastapi import APIRouter

from ..readers import ocr_status
from ..validate import RULES
from ..llm import llm_status
from ..google_sheets import google_sheets_status


router = APIRouter()


@router.get("/health")
def health():
    """Report API and OCR availability."""
    status = ocr_status()

    return {
        "status": "ok",
        "ocr": {
            "available": status["available"],
            "version": status["version"],
            "hint": status["hint"],
        },
        "llm": llm_status(),
        "google_sheets": google_sheets_status(),
    }


@router.get("/rules")
def rules():
    """Return the document validation rules."""
    return RULES
