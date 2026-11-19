"""Runtime settings with validated environment-variable configuration."""

import math
import os
from dataclasses import dataclass
from pathlib import Path


def _int_env(name: str, default: str, minimum: int) -> int:
    raw = os.environ.get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{name} must be an integer; received {raw!r}"
        ) from error

    if value < minimum:
        raise ValueError(
            f"{name} must be at least {minimum}; received {value}"
        )
    return value


def _float_env(name: str, default: str, minimum: float) -> float:
    raw = os.environ.get(name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{name} must be a number; received {raw!r}"
        ) from error

    if not math.isfinite(value) or value < minimum:
        raise ValueError(
            f"{name} must be a finite number >= {minimum}; "
            f"received {raw!r}"
        )
    return value


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    max_upload_bytes: int
    workers: int
    max_attempts: int
    retry_backoff_s: float
    ocr_lang: str
    ocr_dpi: int
    ocr_timeout_s: int
    min_text_chars: int
    max_sheet_rows: int
    tolerance_minor_units: int
    ocr_threads: int
    semantic_backend: str
    glove_vocab: int
    sbert_model: str
    chunk_chars: int
    ocr_field_min_conf: float
    pdf_store_shrink: bool
    tess_omp_threads: int | None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "docintel.sqlite3"

    @property
    def models_dir(self) -> Path:
        configured = os.environ.get("DOCINTEL_MODELS_DIR")
        return Path(configured) if configured else self.data_dir / "models"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"


def get_settings() -> Settings:
    """Read and validate settings from the current environment."""

    data_dir = os.environ.get("DOCINTEL_DATA_DIR", "./data").strip()
    if not data_dir:
        raise ValueError("DOCINTEL_DATA_DIR must not be empty")

    ocr_lang = os.environ.get("DOCINTEL_OCR_LANG", "eng").strip()
    if not ocr_lang:
        raise ValueError("DOCINTEL_OCR_LANG must not be empty")

    semantic_backend = os.environ.get("DOCINTEL_SEMANTIC", "off").strip().lower()
    if semantic_backend in ("", "none"):
        semantic_backend = "off"
    if semantic_backend not in ("off", "glove", "sbert"):
        raise ValueError(
            "DOCINTEL_SEMANTIC must be one of: off, glove, sbert; "
            f"received {semantic_backend!r}"
        )

    sbert_model = os.environ.get(
        "DOCINTEL_SBERT_MODEL",
        "sentence-transformers/all-MiniLM-L6-v2",
    ).strip()
    if semantic_backend == "sbert" and not sbert_model:
        raise ValueError(
            "DOCINTEL_SBERT_MODEL must not be empty when "
            "DOCINTEL_SEMANTIC=sbert"
        )

    ocr_field_min_conf = _float_env(
        "DOCINTEL_OCR_FIELD_MIN_CONF", "60", 0
    )
    if ocr_field_min_conf > 100:
        raise ValueError(
            "DOCINTEL_OCR_FIELD_MIN_CONF must be between 0 and 100"
        )

    tess_omp_threads = _int_env(
        "DOCINTEL_TESS_OMP_THREADS", "1", 0
    )

    return Settings(
        data_dir=Path(data_dir),
        max_upload_bytes=_int_env("DOCINTEL_MAX_UPLOAD_MB", "200", 1)
        * 1024 * 1024,
        workers=_int_env("DOCINTEL_WORKERS", "1", 0),
        max_attempts=_int_env("DOCINTEL_MAX_ATTEMPTS", "3", 1),
        retry_backoff_s=_float_env("DOCINTEL_RETRY_BACKOFF_S", "2", 0),
        ocr_lang=ocr_lang,
        ocr_dpi=_int_env("DOCINTEL_OCR_DPI", "200", 1),
        ocr_timeout_s=_int_env("DOCINTEL_OCR_TIMEOUT_S", "90", 1),
        min_text_chars=_int_env("DOCINTEL_MIN_TEXT_CHARS", "20", 0),
        max_sheet_rows=_int_env("DOCINTEL_MAX_SHEET_ROWS", "100000", 1),
        tolerance_minor_units=_int_env(
            "DOCINTEL_TOLERANCE_MINOR_UNITS", "1", 0
        ),
        ocr_threads=_int_env("DOCINTEL_OCR_THREADS", "1", 1),
        semantic_backend=semantic_backend,
        glove_vocab=_int_env("DOCINTEL_GLOVE_VOCAB", "100000", 1),
        sbert_model=sbert_model,
        chunk_chars=_int_env("DOCINTEL_CHUNK_CHARS", "250", 1),
        ocr_field_min_conf=ocr_field_min_conf,
        pdf_store_shrink=(
            os.environ.get("DOCINTEL_PDF_STORE_SHRINK", "1") != "0"
        ),
        tess_omp_threads=tess_omp_threads or None,
    )
