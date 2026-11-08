"""Cross-document verification, duplicate detection, issue persistence and re-validation.

Matching: two invoices are "the same invoice" when their normalised invoice numbers match and their
suppliers do not contradict each other. How a match is treated depends on the file kinds:
  document vs spreadsheet (PDF/image <-> XLSX/XLS/CSV): cross-VERIFICATION. Values compared; a match is
      recorded as info (VAL-XDOC-002), a difference as an error (VAL-XDOC-001).
  document vs document, or spreadsheet vs spreadsheet: DUPLICATE (VAL-DUP-001), plus VAL-XDOC-001 if the
      two copies disagree on any value."""
import json
import re
from decimal import Decimal, InvalidOperation

from .config import get_settings
from .validate import Issue, needs_review, tolerance, validate_invoice

SPREADSHEET_TYPES = {"xlsx", "xls", "csv"}
MAX_MATCHES = 5


def norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def same_supplier(a, b) -> bool:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return True  # cannot contradict what is missing
    return na == nb or (min(len(na), len(nb)) >= 4 and (na in nb or nb in na))


def _dec(v):
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def differences(mine: dict, other) -> list[str]:
    out = []
    cur = mine.get("currency") or other["currency"]
    tol = tolerance(cur, get_settings().tolerance_minor_units)
    for f in ("subtotal", "tax", "total"):
        a, b = _dec(mine.get(f)) if mine.get(f) not in (None, "") else None, _dec(other[f]) if other[f] not in (None, "") else None
        if a is not None and b is not None and abs(a - b) > tol:
            out.append(f"{f} {a} vs {b}")
    for f in ("currency", "invoice_date"):
        a, b = mine.get(f), other[f]
        if a and b and str(a).upper() != str(b).upper():
            out.append(f"{f} {a} vs {b}")
    if mine.get("customer") and other["customer"] and not same_supplier(mine["customer"], other["customer"]):
        out.append(f"customer '{mine['customer']}' vs '{other['customer']}'")
    return out


def cross_issues(
    db,
    inv_id: int,
    doc_type: str | None,
    vals: dict,
) -> list[Issue]:
    key = norm(vals.get("invoice_number"))

    if not key:
        return []

    candidate = db.execute(
        """
        SELECT i.document_id, i.generation_id, d.active_generation
        FROM invoices i
        JOIN documents d ON d.id = i.document_id
        WHERE i.id = ?
        """,
        (inv_id,),
    ).fetchone()

    if candidate is None:
        return []

    # During staging validation, do not compare against the
    # previous generation of the same document.
    is_staging = (
        candidate["generation_id"] != candidate["active_generation"]
    )

    if is_staging:
        exclusion = "AND i.document_id != ?"
        excluded_id = candidate["document_id"]
    else:
        exclusion = "AND i.id != ?"
        excluded_id = inv_id

    rows = db.execute(
        f"""
        SELECT
            i.id, i.document_id, i.supplier, i.customer,
            i.invoice_date, i.subtotal, i.tax, i.total, i.currency,
            d.file_type, d.filename
        FROM invoices i
        JOIN documents d ON d.id = i.document_id
        WHERE i.num_key = ?
          {exclusion}
          AND i.generation_id = d.active_generation
        ORDER BY i.id
        LIMIT 50
        """,
        (key, excluded_id),
    ).fetchall()

    issues: list[Issue] = []
    n = 0

    for r in rows:
        if not same_supplier(vals.get("supplier"), r["supplier"]):
            continue

        n += 1

        if n > MAX_MATCHES:
            break

        diffs = differences(vals, r)

        where = (
            f"'{r['filename']}' "
            f"(document {r['document_id']}, invoice {r['id']})"
        )

        if (
            doc_type in SPREADSHEET_TYPES
        ) != (
            r["file_type"] in SPREADSHEET_TYPES
        ):
            if diffs:
                issues.append(
                    Issue(
                        "VAL-XDOC-001",
                        "error",
                        "total" if any(
                            d.startswith("total") for d in diffs
                        ) else None,
                        f"Does not match {where}: " + "; ".join(diffs),
                    )
                )
            else:
                issues.append(
                    Issue(
                        "VAL-XDOC-002",
                        "info",
                        None,
                        f"Verified against {where}: all compared values match.",
                    )
                )
        else:
            issues.append(
                Issue(
                    "VAL-DUP-001",
                    "error",
                    "invoice_number",
                    f"Same invoice number (and compatible supplier) already in {where}.",
                )
            )

            if diffs:
                issues.append(
                    Issue(
                        "VAL-XDOC-001",
                        "error",
                        None,
                        f"The two copies disagree with {where}: "
                        + "; ".join(diffs),
                    )
                )

    return issues


def write_issues(db, inv_id: int, issues: list[Issue]) -> bool:
    """Replace an invoice's issues (keeping 'resolved' marks for the same rule+field); returns needs_review."""
    resolved = {(r["rule_id"], r["field"]) for r in db.execute(
        "SELECT rule_id, field FROM validation_issues WHERE invoice_id=? AND resolved=1", (inv_id,))}
    db.execute("DELETE FROM validation_issues WHERE invoice_id=?", (inv_id,))
    open_issues = []
    for i in issues:
        res = int((i.rule_id, i.field) in resolved and i.severity != "info")
        db.execute("INSERT INTO validation_issues(invoice_id,rule_id,severity,field,message,resolved) VALUES (?,?,?,?,?,?)",
                   (inv_id, i.rule_id, i.severity, i.field, i.message, res))
        if not res:
            open_issues.append(i)
    review = needs_review(open_issues)
    db.execute("UPDATE invoices SET needs_review=? WHERE id=?", (int(review), inv_id))
    return review


def refresh_document_review(db, doc_id: int) -> None:
    flag = db.execute(
        """
        SELECT EXISTS(
            SELECT 1
            FROM invoices i
            JOIN documents d ON d.id = i.document_id
            WHERE i.document_id = ?
              AND i.generation_id = d.active_generation
              AND i.needs_review = 1
        )
        """,
        (doc_id,),
    ).fetchone()[0]

    db.execute(
        "UPDATE documents SET review_required=? WHERE id=?",
        (flag, doc_id),
    )


def load_invoice_inputs(db, inv_id: int):
    inv = db.execute(
        """
        SELECT i.*, d.file_type, d.active_generation
        FROM invoices i
        JOIN documents d ON d.id = i.document_id
        WHERE i.id = ?
        """,
        (inv_id,),
    ).fetchone()
    items = [dict(r) for r in db.execute(
        "SELECT idx, description, quantity, unit_price, amount, ocr_conf FROM line_items WHERE invoice_id=? ORDER BY idx", (inv_id,))]
    return inv, json.loads(inv["fields_json"]), items


def revalidate_invoice(db, inv_id: int) -> None:
    inv, F, items = load_invoice_inputs(db, inv_id)

    if inv is None:
        return

    if inv["generation_id"] != inv["active_generation"]:
        return

    vals = {k: (v or {}).get("value") for k, v in F.items()}
    issues = (
        validate_invoice(
            F,
            items,
            expect_line_items=not inv["extractor"].endswith("-register"),
        )
        + cross_issues(db, inv_id, inv["file_type"], vals)
    )

    write_issues(db, inv_id, issues)
    refresh_document_review(db, inv["document_id"])


def revalidate_related(
    db,
    keys: set[str],
    exclude_document: int | None = None,
) -> int:
    """Revalidate active invoices in other documents sharing invoice numbers."""
    ids = set()

    for k in keys:
        if not k:
            continue

        q = """
            SELECT i.id
            FROM invoices i
            JOIN documents d ON d.id = i.document_id
            WHERE i.num_key = ?
              AND i.generation_id = d.active_generation
        """
        args = [k]

        if exclude_document is not None:
            q += " AND i.document_id != ?"
            args.append(exclude_document)

        ids.update(
            r["id"] for r in db.execute(q, args)
        )

    for invoice_id in ids:
        revalidate_invoice(db, invoice_id)

    return len(ids)