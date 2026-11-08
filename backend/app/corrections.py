"""Manual corrections: field and line-item edits with an audit trail, then automatic re-validation."""
import json

from pydantic import BaseModel, Field

from .db import add_event, now
from .extract import FIELDS, parse_date
from .money import parse_money
from .review import norm, refresh_document_review, revalidate_invoice, revalidate_related

AMOUNT_FIELDS = {"subtotal", "tax", "total"}
ITEM_ATTRS = ("description", "quantity", "unit_price", "amount")


class LineItemPatch(BaseModel):
    idx: int | None = None          # omit to add a new line item
    description: str | None = None
    quantity: str | None = None
    unit_price: str | None = None
    amount: str | None = None
    delete: bool = False


class InvoicePatch(BaseModel):
    fields: dict[str, str | None] = Field(default_factory=dict)
    line_items: list[LineItemPatch] = Field(default_factory=list)
    note: str | None = None


class CorrectionError(ValueError):
    pass


def _normalise(name: str, value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    v = str(value).strip()
    if name in AMOUNT_FIELDS or name in ("quantity", "unit_price", "amount"):
        d = parse_money(v)
        return str(d) if d is not None else v           # unparseable text is kept so validation can flag it
    if name == "invoice_date":
        iso, _ = parse_date(v)
        return iso or v
    if name == "currency":
        return v.upper()
    return v


def _log(db, inv_id, field, old, new, note):
    db.execute("INSERT INTO corrections(invoice_id,field,old_value,new_value,note,created_at) VALUES (?,?,?,?,?,?)",
               (inv_id, field, None if old is None else str(old), None if new is None else str(new), note, now()))


def apply_corrections(db, inv_id: int, patch: InvoicePatch) -> int:
    """Apply a patch inside the caller's transaction. Returns the number of changed values."""
    inv = db.execute(
    """
    SELECT i.*, d.active_generation
    FROM invoices i
    JOIN documents d ON d.id = i.document_id
    WHERE i.id = ?
    """,
    (inv_id,),
    ).fetchone()
    if inv is None:
        raise LookupError("Invoice not found")
    if inv["generation_id"] != inv["active_generation"]:
        raise CorrectionError("Archived invoices are read-only")
    for name in patch.fields:
        if name not in FIELDS:
            raise CorrectionError(f"Unknown field '{name}'. Allowed: {', '.join(FIELDS)}")
    F = json.loads(inv["fields_json"])
    old_key, changes = inv["num_key"], 0

    for name, raw in patch.fields.items():
        new, old = _normalise(name, raw), F[name].get("value")
        if new == old:
            continue
        F[name] = {"value": new, "confidence": 1.0 if new is not None else 0.0, "evidence": "manual correction",
                   "page": None, "note": None, "status": "ok" if new is not None else "missing", "raw": None,
                   "source": "manual", "original_value": F[name].get("original_value", old)}
        _log(db, inv_id, name, old, new, patch.note)
        changes += 1

    for lp in patch.line_items:
        given = {a: _normalise(a, getattr(lp, a)) for a in ITEM_ATTRS if a in lp.model_fields_set}
        if lp.idx is None:
            nxt = (db.execute("SELECT COALESCE(MAX(idx),0)+1 FROM line_items WHERE invoice_id=?", (inv_id,)).fetchone()[0])
            if lp.delete or not given:
                raise CorrectionError("A new line item needs at least one value")
            db.execute("INSERT INTO line_items(invoice_id,idx,description,quantity,unit_price,amount,evidence) VALUES (?,?,?,?,?,?,?)",
                       (inv_id, nxt, given.get("description"), given.get("quantity"), given.get("unit_price"), given.get("amount"), "added manually"))
            _log(db, inv_id, f"line_items[{nxt}]", None, json.dumps(given), patch.note)
            changes += 1
            continue
        row = db.execute("SELECT * FROM line_items WHERE invoice_id=? AND idx=?", (inv_id, lp.idx)).fetchone()
        if row is None:
            raise CorrectionError(f"Line item {lp.idx} does not exist")
        if lp.delete:
            db.execute("DELETE FROM line_items WHERE id=?", (row["id"],))
            _log(db, inv_id, f"line_items[{lp.idx}]", json.dumps({a: row[a] for a in ITEM_ATTRS}), None, patch.note)
            changes += 1
            continue
        for a, new in given.items():
            if new != row[a]:
                db.execute(f"UPDATE line_items SET {a}=?, evidence=?, ocr_conf=NULL WHERE id=?", (new, "manual correction", row["id"]))
                _log(db, inv_id, f"line_items[{lp.idx}].{a}", row[a], new, patch.note)
                changes += 1

    if changes:
        v = {k: F[k]["value"] for k in FIELDS}
        new_key = norm(v["invoice_number"]) or None
        db.execute("UPDATE invoices SET fields_json=?, invoice_number=?, invoice_date=?, supplier=?, customer=?, subtotal=?,"
                   " tax=?, total=?, currency=?, num_key=? WHERE id=?",
                   (json.dumps(F), v["invoice_number"], v["invoice_date"], v["supplier"], v["customer"], v["subtotal"],
                    v["tax"], v["total"], v["currency"], new_key, inv_id))
        revalidate_invoice(db, inv_id)
        revalidate_related(db, {old_key, new_key} - {None}, exclude_document=inv["document_id"])
        add_event(db, inv["document_id"], "corrected", f"invoice={inv_id} changes={changes}")
    return changes


def set_issue_resolved(db, issue_id: int, resolved: bool) -> int:
    """Acknowledge (or re-open) a warning/error. Returns the invoice id."""
    row = db.execute(
        """
        SELECT
            v.id,
            v.invoice_id,
            v.rule_id,
            v.severity,
            i.document_id,
            i.generation_id,
            d.active_generation
        FROM validation_issues v
        JOIN invoices i ON i.id = v.invoice_id
        JOIN documents d ON d.id = i.document_id
        WHERE v.id = ?
        """,
        (issue_id,),
    ).fetchone()
    if row is None:
        raise LookupError("Issue not found")
    if row["generation_id"] != row["active_generation"]:
        raise CorrectionError("Archived invoice issues are read-only")
    if row["severity"] == "info":
        raise CorrectionError("Informational results cannot be resolved")
    db.execute("UPDATE validation_issues SET resolved=? WHERE id=?", (int(resolved), issue_id))
    left = db.execute("SELECT COUNT(*) FROM validation_issues WHERE invoice_id=? AND resolved=0 AND severity IN ('error','warning')",
                      (row["invoice_id"],)).fetchone()[0]
    db.execute("UPDATE invoices SET needs_review=? WHERE id=?", (int(left > 0), row["invoice_id"]))
    refresh_document_review(db, row["document_id"])
    add_event(db, row["document_id"], "issue_resolved" if resolved else "issue_reopened", f"issue={issue_id} rule={row['rule_id']}")
    return row["invoice_id"]
