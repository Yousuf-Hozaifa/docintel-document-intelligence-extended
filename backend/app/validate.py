"""Deterministic validation rules. Every rule has a stable ID; arithmetic uses Decimal,
per-currency precision and a configurable rounding tolerance (not flat equality)."""
import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .config import get_settings
from .money import ISO_4217, minor_places

RULES = {
    "VAL-REQ-001": "Required field missing",
    "VAL-REQ-002": "No line items extracted",
    "VAL-DATE-001": "Invoice date is invalid or unparseable",
    "VAL-DATE-002": "Invoice date is ambiguous (day/month order)",
    "VAL-DATE-003": "Invoice date is in the future",
    "VAL-CUR-001": "Currency code is not a recognised ISO 4217 code",
    "VAL-NUM-001": "Amount is not a valid number",
    "VAL-NUM-002": "Amount is negative or zero (possible credit note)",
    "VAL-LINE-001": "Line item quantity x unit price does not equal amount",
    "VAL-LINE-002": "Sum of line items does not equal subtotal",
    "VAL-OCR-001": "Low OCR confidence for an extracted value; verify against the source image",
    "VAL-LINE-003": "Line item quantity unreadable (OCR); confirm manually",
    "VAL-SUB-001": "Subtotal + tax does not equal total",
    "VAL-SUB-002": "Tax missing and subtotal differs from total",
    "VAL-TAX-001": "Tax is negative or larger than the subtotal",
    "VAL-DUP-001": "Possible duplicate invoice (same supplier and invoice number)",
    "VAL-XDOC-001": "Cross-document mismatch: the same invoice in another file has different values",
    "VAL-XDOC-002": "Cross-document verification passed (document vs spreadsheet values match)",
    "AI-REVIEW-001": "AI-assisted extraction suggestion requires human verification",
}
REQUIRED = {"invoice_number": "error", "invoice_date": "error", "supplier": "error",
            "total": "error", "customer": "warning", "currency": "warning"}
AMOUNTS = ("subtotal", "tax", "total")


@dataclass
class Issue:
    rule_id: str
    severity: str
    field: str | None
    message: str


def _dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v))
        return d if d.is_finite() else None
    except InvalidOperation:
        return None


def tolerance(currency: str | None, units: float = 1) -> Decimal:
    return Decimal(10) ** -minor_places(currency) * Decimal(str(units))


def validate_invoice(fields: dict, items: list[dict], today: date | None = None, expect_line_items: bool = True) -> list[Issue]:
    """fields: name -> {value, status, note, raw}. items: list of line-item dicts."""
    today = today or date.today()
    s = get_settings()
    issues: list[Issue] = []
    val = {k: (v or {}).get("value") for k, v in fields.items()}

    for name, sev in REQUIRED.items():
        if val.get(name) in (None, "") and not (name == "invoice_date" and fields.get(name, {}).get("status") in ("invalid", "ambiguous")):
            issues.append(Issue("VAL-REQ-001", sev, name, f"Required field '{name}' is missing; manual entry needed."))
    if not items and expect_line_items:
        issues.append(Issue("VAL-REQ-002", "warning", "line_items", "No line items could be extracted."))

    # weakest OCR word behind a value (values a human already corrected carry no OCR confidence)
    floor = s.ocr_field_min_conf
    if floor > 0:
        for name, f in fields.items():
            oc = (f or {}).get("ocr_confidence")
            if f and f.get("value") not in (None, "") and oc is not None and oc < floor:
                issues.append(Issue("VAL-OCR-001", "warning", name,
                                    f"OCR confidence for '{name}' is {oc:.0f}% (below {floor:.0f}%); verify the value against the source image."))
        for i, it in enumerate(items, 1):
            oc = it.get("ocr_conf")
            if oc is not None and oc < floor:
                issues.append(Issue("VAL-OCR-001", "warning", f"line_items[{i}]",
                                    f"OCR confidence for line {i} is {oc:.0f}% (below {floor:.0f}%); verify the row against the source image."))

    # date
    d = fields.get("invoice_date", {})
    if d.get("status") == "invalid":
        issues.append(Issue("VAL-DATE-001", "error", "invoice_date",
                            f"Date text '{d.get('raw')}' is not a valid date."))
    elif d.get("status") == "ambiguous":
        issues.append(Issue("VAL-DATE-002", "warning", "invoice_date",
                            f"Date '{d.get('raw')}' could be day/month or month/day; confirm manually."))
    elif val.get("invoice_date"):
        try:
            dt = datetime.strptime(str(val["invoice_date"]), "%Y-%m-%d").date()
            if dt > today:
                issues.append(Issue("VAL-DATE-003", "warning", "invoice_date", f"Invoice date {dt} is in the future."))
        except ValueError:
            issues.append(Issue("VAL-DATE-001", "error", "invoice_date",
                                f"'{val['invoice_date']}' is not a valid YYYY-MM-DD date."))

    # currency
    cur = (val.get("currency") or "").upper() or None
    if cur and cur not in ISO_4217:
        issues.append(Issue("VAL-CUR-001", "error", "currency", f"'{cur}' is not a recognised ISO 4217 currency code."))

    # numbers
    num: dict[str, Decimal | None] = {}
    for a in AMOUNTS:
        raw = val.get(a)
        num[a] = _dec(raw)
        if raw not in (None, "") and num[a] is None:
            issues.append(Issue("VAL-NUM-001", "error", a, f"'{raw}' is not a valid number."))
        elif num[a] is not None and a != "tax" and num[a] <= 0:
            issues.append(Issue("VAL-NUM-002", "warning", a, f"{a} is {num[a]} (negative/zero); check for a credit note."))
    sub, tax, total = num["subtotal"], num["tax"], num["total"]
    if tax is not None and (tax < 0 or (sub is not None and tax > sub)):
        issues.append(Issue("VAL-TAX-001", "warning", "tax", f"Tax {tax} is negative or exceeds subtotal {sub}."))

    tol = tolerance(cur, s.tolerance_minor_units)

    # line items
    amts = []
    for i, it in enumerate(items, 1):
        q, u, a = _dec(it.get("quantity")), _dec(it.get("unit_price")), _dec(it.get("amount"))
        if q is None and it.get("quantity") in (None, "", "None") and (u is not None or a is not None):
            hint = f" (amount / unit price = {(a / u).normalize():f})" if u and a is not None and u != 0 else ""
            issues.append(Issue("VAL-LINE-003", "warning", f"line_items[{i}].quantity",
                                f"Line {i}: quantity could not be read{hint}; confirm manually."))
        for key, v in (("quantity", q), ("unit_price", u), ("amount", a)):
            if it.get(key) not in (None, "", "None") and v is None:
                issues.append(Issue("VAL-NUM-001", "error", f"line_items[{i}].{key}", f"Line {i}: '{it.get(key)}' is not a valid number."))
        if None not in (q, u, a):
            # a unit price displayed at 2dp may hide fractions: allow tolerance scaled by quantity
            line_tol = tol * max(Decimal(1), abs(q)) if q == q.to_integral_value() else tol * Decimal(2)
            if abs(q * u - a) > line_tol:
                issues.append(Issue("VAL-LINE-001", "error", f"line_items[{i}]",
                                    f"Line {i}: {q} x {u} = {q * u}, but amount is {a} (tolerance {line_tol})."))
        if a is not None:
            amts.append(a)

    if amts and len(amts) == len(items):
        line_sum = sum(amts, Decimal(0))
        sum_tol = tol * max(1, math.ceil(len(amts) / 2))
        if sub is not None and abs(line_sum - sub) > sum_tol:
            issues.append(Issue("VAL-LINE-002", "warning", "subtotal",
                                f"Line items sum to {line_sum} but subtotal is {sub} (tolerance {sum_tol}); "
                                "may be a discount, shipping or rounding convention."))

    # subtotal / tax / total
    if None not in (sub, tax, total):
        if abs(sub + tax - total) > tol:
            issues.append(Issue("VAL-SUB-001", "error", "total",
                                f"Subtotal {sub} + tax {tax} = {sub + tax}, but total is {total} (tolerance {tol})."))
    elif sub is not None and total is not None and tax is None and abs(sub - total) > tol:
        issues.append(Issue("VAL-SUB-002", "warning", "tax",
                            f"Total {total} differs from subtotal {sub} but no tax was extracted."))
    return issues


def needs_review(issues: list[Issue], fields: dict | None = None) -> bool:
    return any(i.severity in ("error", "warning") for i in issues)
