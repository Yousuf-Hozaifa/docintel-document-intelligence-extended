"""Shared document serialization and storage helpers."""

import json

from .config import get_settings


def _stored_path(stored_name: str):
    """Return a safe, flat upload path; reject traversal and symlinks."""
    from pathlib import PureWindowsPath

    if (
        not isinstance(stored_name, str)
        or not stored_name
        or stored_name in {".", ".."}
        or "\x00" in stored_name
        or "/" in stored_name
        or "\\" in stored_name
        or ":" in stored_name
        or PureWindowsPath(stored_name).drive
    ):
        return None

    try:
        base = get_settings().upload_dir.resolve()
        candidate = base / stored_name

        # Do not follow even an in-directory symlink. Check before resolve().
        if candidate.is_symlink():
            return None

        path = candidate.resolve(strict=False)
        if path == base or path.parent != base:
            return None

        # A stored upload must be a regular file if it already exists.
        if path.exists() and not path.is_file():
            return None

        return path
    except (OSError, RuntimeError, ValueError):
        return None


def _doc_row(row) -> dict:
    """Serialize a document row without exposing its stored filename."""
    document = dict(row)
    document.pop("stored_name", None)
    document["review_required"] = bool(
        document["review_required"]
    )
    return document


def _invoice_detail(db, invoice) -> dict:
    """Return invoice details and identify archived invoices as read-only."""
    result = dict(invoice)

    document = db.execute(
        """
        SELECT active_generation
        FROM documents
        WHERE id=?
        """,
        (result["document_id"],),
    ).fetchone()

    active_generation = (
        document["active_generation"]
        if document
        else None
    )

    result["fields"] = json.loads(
        result.pop("fields_json")
    )
    result["needs_review"] = bool(result["needs_review"])
    result.pop("dup_key", None)

    result["active_generation"] = active_generation
    result["archived"] = (
        active_generation is None
        or result["generation_id"] != active_generation
    )
    result["read_only"] = result["archived"]

    result["line_items"] = [
        dict(item)
        for item in db.execute(
            """
            SELECT idx, description, quantity, unit_price,
                   amount, evidence
            FROM line_items
            WHERE invoice_id=?
            ORDER BY idx
            """,
            (result["id"],),
        )
    ]

    result["issues"] = [
        dict(issue, resolved=bool(issue["resolved"]))
        for issue in db.execute(
            """
            SELECT id, rule_id, severity, field,
                   message, resolved
            FROM validation_issues
            WHERE invoice_id=?
            ORDER BY id
            """,
            (result["id"],)
        )
    ]

    result["correction_count"] = db.execute(
        """
        SELECT COUNT(*)
        FROM corrections
        WHERE invoice_id=?
        """,
        (result["id"],),
    ).fetchone()[0]

    return result
