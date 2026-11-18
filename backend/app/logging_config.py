"""Centralized JSON logging configuration for DocIntel."""

import json
import logging
import os
from datetime import datetime, timezone


_CONTEXT_FIELDS = (
    "event",
    "job_id",
    "document_id",
    "attempt",
    "error_code",
    "exception_type",
)


class JsonFormatter(logging.Formatter):
    """Format log records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, timezone.utc
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for field in _CONTEXT_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging() -> logging.Logger:
    """Configure the DocIntel application logger once."""

    level_name = os.environ.get("DOCINTEL_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, None)

    if not isinstance(level, int):
        raise ValueError(
            f"Invalid DOCINTEL_LOG_LEVEL: {level_name}"
        )

    logger = logging.getLogger("docintel")
    logger.setLevel(level)
    logger.propagate = False

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)

    for handler in logger.handlers:
        handler.setLevel(level)

    return logger
