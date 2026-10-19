"""Quote agent: builds draft quotations ONLY from owner-approved pricing and owner-supplied measurements.

Anything missing (price, quantity, GST status, customer email) is listed in `missing`,
and an incomplete quote cannot be approved or sent.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from ..approvals import ApprovalManager
from ..context import Context

CENT = Decimal("0.01")


def _money(x: Decimal) -> str:
    return f"${x.quantize(CENT, rounding=ROUND_HALF_UP):,.2f}"


def build_quote(ctx: Context, lead: dict[str, Any], lines: list[dict[str, Any]], scope_notes: str = "",
                exclusions: str = "", today: date | None = None) -> dict[str, Any]:
    pricing = ctx.settings.load_pricing()
    items = pricing.get("items", {}) if isinstance(pricing.get("items"), dict) else {}
    missing: list[str] = []
    if not pricing:
        missing.append("pricing file not found — create config/pricing.json from pricing.example.json")
    if not lead.get("email"):
        missing.append("customer email address")
    if not lines:
        missing.append("at least one quote line (item + measured quantity)")

    rows: list[dict[str, str]] = []
    subtotal = Decimal("0")
    for i, line in enumerate(lines, 1):
        key = str(line.get("item", "")).strip()
        item = items.get(key)
        if not item:
            missing.append(f"line {i}: unknown pricing item '{key}'")
            continue
        label = str(item.get("label", key))
        unit = str(item.get("unit", "each"))
        try:
            price = Decimal(str(item.get("unit_price")))
            if price < 0 or item.get("unit_price") is None:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            missing.append(f"{label}: owner-approved unit price")
            price = None  # type: ignore[assignment]
        try:
            qty = Decimal(str(line.get("quantity")))
            if qty <= 0 or line.get("quantity") is None:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            missing.append(f"{label}: measured quantity ({unit})")
            qty = None  # type: ignore[assignment]
        if price is not None and qty is not None:
            amount = (price * qty).quantize(CENT, rounding=ROUND_HALF_UP)
            subtotal += amount
            rows.append({"label": label, "qty": f"{qty.normalize():f} {unit}", "rate": _money(price),
                         "amount": _money(amount)})
        else:
            rows.append({"label": label, "qty": "[TO CONFIRM]", "rate": "[TO CONFIRM]", "amount": "[TO CONFIRM]"})

    gst_registered = pricing.get("gst_registered")
    if gst_registered is True:
        gst = (subtotal * Decimal("0.10")).quantize(CENT, rounding=ROUND_HALF_UP)
        totals = (f"Subtotal (ex GST): {_money(subtotal)}\nGST (10%): {_money(gst)}\n"
                  f"Total (inc GST): {_money(subtotal + gst)}")
    elif gst_registered is False:
        totals = f"Total: {_money(subtotal)}\n(No GST charged — business not registered for GST.)"
    else:
        missing.append("GST treatment (set gst_registered true/false in pricing file)")
        totals = f"Subtotal: {_money(subtotal)}\nGST: [TO CONFIRM]"

    today = today or date.today()
    validity = pricing.get("validity_days")
    valid_line = (f"This quote is valid until {(today + timedelta(days=int(validity))):%d %B %Y}."
                  if isinstance(validity, int) and validity > 0 else "")
    terms = []
    if pricing.get("payment_terms"):
        terms.append(f"Payment terms: {pricing['payment_terms']}")
    if pricing.get("warranty"):
        terms.append(f"Warranty: {pricing['warranty']}")

    s = ctx.settings
    first = (lead.get("name") or "").split(" ")[0] or "there"
    lines_txt = "\n".join(f"- {r['label']}: {r['qty']} @ {r['rate']} = {r['amount']}" for r in rows) or "- [TO CONFIRM]"
    body = (
        f"Hi {first},\n\nThanks for having us out. Here's your quote for the work at "
        f"{lead.get('suburb') or '[PROPERTY ADDRESS]'}.\n\n"
        f"Scope of work:\n{scope_notes or '[TO CONFIRM]'}\n\n"
        f"Quote:\n{lines_txt}\n\n{totals}\n\n"
        + (f"Exclusions:\n{exclusions}\n\n" if exclusions else "")
        + ("\n".join(terms) + "\n\n" if terms else "")
        + (valid_line + "\n\n" if valid_line else "")
        + "Next step: just reply to this email to go ahead or if you have any questions.\n\n"
        f"Cheers,\n{s.email_signature}" + (f"\nPh: {s.business_phone}" if s.business_phone else "")
    )
    return {"to": lead.get("email", ""), "subject": f"Your quote from {s.business_name}", "body": body,
            "rows": rows, "missing": missing, "complete": not missing}


class QuoteAgent:
    name = "quote"

    def __init__(self, ctx: Context, approvals: ApprovalManager) -> None:
        self.ctx = ctx
        self.approvals = approvals

    def draft(self, lead_id: int, lines: list[dict[str, Any]], scope_notes: str = "", exclusions: str = "",
              revision: int = 1) -> dict[str, Any]:
        lead = self.ctx.storage.get_lead(lead_id)
        if not lead:
            raise ValueError("lead not found")
        q = build_quote(self.ctx, lead, lines, scope_notes, exclusions)
        payload = {k: q[k] for k in ("to", "subject", "body", "missing")}
        res = self.approvals.propose("quote", f"Quote for {lead.get('name') or lead.get('email') or lead_id}"
                                     + ("" if q["complete"] else " (INCOMPLETE)"),
                                     payload, f"quote:{lead_id}:r{int(revision)}", lead_id)
        return {**q, "approval": res}
