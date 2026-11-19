import pytest

from app.config import get_settings


def test_default_settings_are_valid(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCINTEL_WORKERS", raising=False)
    monkeypatch.setenv("DOCINTEL_DATA_DIR", str(tmp_path))

    settings = get_settings()

    assert settings.data_dir == tmp_path
    assert settings.max_upload_bytes == 200 * 1024 * 1024
    assert settings.workers == 1
    assert settings.max_attempts == 3
    assert settings.semantic_backend == "off"
    assert settings.ocr_field_min_conf == 60.0
    assert settings.tess_omp_threads == 1


def test_zero_workers_remains_valid(monkeypatch):
    monkeypatch.setenv("DOCINTEL_WORKERS", "0")

    assert get_settings().workers == 0


@pytest.mark.parametrize("value", ["", "none", "off"])
def test_disabled_semantic_values_are_compatible(monkeypatch, value):
    monkeypatch.setenv("DOCINTEL_SEMANTIC", value)

    assert get_settings().semantic_backend == "off"


@pytest.mark.parametrize("value", ["glove", "sbert"])
def test_supported_semantic_backends(monkeypatch, value):
    monkeypatch.setenv("DOCINTEL_SEMANTIC", value)

    assert get_settings().semantic_backend == value


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DOCINTEL_MAX_UPLOAD_MB", "abc"),
        ("DOCINTEL_WORKERS", "-1"),
        ("DOCINTEL_MAX_ATTEMPTS", "0"),
        ("DOCINTEL_OCR_DPI", "0"),
        ("DOCINTEL_OCR_TIMEOUT_S", "-1"),
        ("DOCINTEL_OCR_THREADS", "0"),
        ("DOCINTEL_MAX_SHEET_ROWS", "0"),
        ("DOCINTEL_TOLERANCE_MINOR_UNITS", "-1"),
        ("DOCINTEL_GLOVE_VOCAB", "0"),
        ("DOCINTEL_CHUNK_CHARS", "0"),
        ("DOCINTEL_TESS_OMP_THREADS", "-1"),
    ],
)
def test_invalid_integer_settings_are_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        get_settings()


@pytest.mark.parametrize("value", ["-0.1", "nan", "inf", "-inf", "invalid"])
def test_invalid_retry_backoff_is_rejected(monkeypatch, value):
    monkeypatch.setenv("DOCINTEL_RETRY_BACKOFF_S", value)

    with pytest.raises(ValueError, match="DOCINTEL_RETRY_BACKOFF_S"):
        get_settings()


@pytest.mark.parametrize("value", ["-1", "100.1", "nan", "inf", "invalid"])
def test_invalid_ocr_confidence_threshold_is_rejected(monkeypatch, value):
    monkeypatch.setenv("DOCINTEL_OCR_FIELD_MIN_CONF", value)

    with pytest.raises(ValueError, match="DOCINTEL_OCR_FIELD_MIN_CONF"):
        get_settings()


def test_zero_ocr_confidence_threshold_is_valid(monkeypatch):
    monkeypatch.setenv("DOCINTEL_OCR_FIELD_MIN_CONF", "0")

    assert get_settings().ocr_field_min_conf == 0


def test_zero_tesseract_threads_preserves_default_behavior(monkeypatch):
    monkeypatch.setenv("DOCINTEL_TESS_OMP_THREADS", "0")

    assert get_settings().tess_omp_threads is None


def test_unknown_semantic_backend_is_rejected(monkeypatch):
    monkeypatch.setenv("DOCINTEL_SEMANTIC", "unknown")

    with pytest.raises(ValueError, match="DOCINTEL_SEMANTIC"):
        get_settings()


def test_empty_data_directory_is_rejected(monkeypatch):
    monkeypatch.setenv("DOCINTEL_DATA_DIR", "  ")

    with pytest.raises(ValueError, match="DOCINTEL_DATA_DIR"):
        get_settings()


def test_sbert_requires_a_model_name(monkeypatch):
    monkeypatch.setenv("DOCINTEL_SEMANTIC", "sbert")
    monkeypatch.setenv("DOCINTEL_SBERT_MODEL", " ")

    with pytest.raises(ValueError, match="DOCINTEL_SBERT_MODEL"):
        get_settings()
