"""Business development: outreach drafts to owner-entered prospects (property managers, strata, agents).

Complies with the Spam Act 2003 basics: a recorded consent basis is required, the sender is
identified, and every message has a working opt-out. Outreach is NEVER sent automatically.
"""
from __future__ import annotations

from typing import Any

from ..approvals import ApprovalManager
from ..context import Context

MAX_DRAFTS_PER_RUN = 5

PARTNER_IDEAS = [
    "Property managers at local real-estate agencies (rental maintenance, end-of-lease gutter cleans)",
    "Strata managers (common-property roofs, gutters, fascia)",
    "Real-estate sales agents (pre-sale roof restorations and painting)",
    "Building & pest inspectors (referrals after inspection reports)",
    "Insurance assessors / builders (storm damage repairs)",
]


def outreach_fallback(ctx: Context, prospect: dict[str, Any]) -> str:
    s = ctx.settings
    first = (prospect.get("contact_name") or "").split(" ")[0] or "there"
    phone = f" on {s.business_phone}" if s.business_phone else ""
    return (
        f"Hi {first},\n\nI'm reaching out from {s.business_name}. We look after roofs, gutters, fascia and "
        f"painting for properties across {s.service_area}.\n\n"
        f"If {prospect['organisation']} ever needs a reliable trade for maintenance or repair jobs on your "
        f"properties, we'd be glad to help — quick inspections and clear written quotes.\n\n"
        f"Happy to have a quick chat whenever suits{phone}.\n\n"
        f"Cheers,\n{s.email_signature}\n{s.business_website}\n\n"
        "If you'd rather not hear from us, just reply 'unsubscribe' and we won't contact you again."
    )


class BizDevAgent:
    name = "bizdev"

    def __init__(self, ctx: Context, approvals: ApprovalManager) -> None:
        self.ctx = ctx
        self.approvals = approvals

    def run(self, run_id: int | None = None) -> dict[str, Any]:
        st = self.ctx.storage
        drafted, skipped = 0, []
        for p in st.list_prospects("new"):
            if drafted >= MAX_DRAFTS_PER_RUN:
                break
            if not p["email"]:
                skipped.append(f"{p['organisation']}: no email")
                continue
            if not p["consent_basis"]:
                skipped.append(f"{p['organisation']}: no consent basis recorded (Spam Act)")
                continue
            fallback = outreach_fallback(self.ctx, p)
            prompt = (
                f"Write a short, warm B2B intro email (under 120 words) from a Sydney roofing business to "
                f"{p['contact_name'] or 'the team'} at {p['organisation']} ({p['category'] or 'property business'}). "
                "Offer reliable roof/gutter/painting maintenance. No prices, no guarantees, no invented clients. "
                "End with the signature and this exact opt-out line: "
                "\"If you'd rather not hear from us, just reply 'unsubscribe' and we won't contact you again.\"\n"
                f"Signature:\n{self.ctx.settings.email_signature}\n{self.ctx.settings.business_website}"
            )
            body = self.ctx.llm.generate(prompt, fallback).text
            if "unsubscribe" not in body.lower():
                body += "\n\nIf you'd rather not hear from us, just reply 'unsubscribe' and we won't contact you again."
            self.approvals.propose(
                "outreach_email", f"Intro email to {p['organisation']}",
                {"to": p["email"], "subject": f"Roof & gutter maintenance for {p['organisation']}", "body": body,
                 "prospect_id": p["id"], "consent_basis": p["consent_basis"]},
                f"outreach:{p['id']}", None, run_id)
            st.update_prospect_status(p["id"], "drafted")
            drafted += 1
        return {"drafted": drafted, "skipped": skipped, "partner_ideas": PARTNER_IDEAS}
