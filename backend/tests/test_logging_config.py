import json
import logging

import pytest

from app.logging_config import JsonFormatter, configure_logging


def test_json_formatter_includes_standard_and_context_fields():
    record = logging.LogRecord(
        name="docintel.worker",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="Document processing completed",
        args=(),
        exc_info=None,
    )
    record.event = "job_completed"
    record.job_id = 12
    record.document_id = 34
    record.attempt = 2

    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "docintel.worker"
    assert payload["message"] == "Document processing completed"
    assert payload["event"] == "job_completed"
    assert payload["job_id"] == 12
    assert payload["document_id"] == 34
    assert payload["attempt"] == 2
    assert payload["timestamp"].endswith("Z")


def test_json_formatter_omits_missing_context_fields():
    record = logging.LogRecord(
        name="docintel",
        level=logging.WARNING,
        pathname=__file__,
        lineno=20,
        msg="Worker is waiting",
        args=(),
        exc_info=None,
    )

    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "Worker is waiting"
    assert "job_id" not in payload
    assert "document_id" not in payload


def test_configure_logging_rejects_invalid_level(monkeypatch):
    monkeypatch.setenv("DOCINTEL_LOG_LEVEL", "NOT_A_LEVEL")

    with pytest.raises(ValueError, match="Invalid DOCINTEL_LOG_LEVEL"):
        configure_logging()
