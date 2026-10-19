"""Website-form lead intake (called by the webhook)."""
from __future__ import annotations

import hashlib
import re
from typing import Any

from ..approvals import ApprovalManager
from ..context import Context
from . import sales
from .support import escalation_reason

FIELD_ALIASES = {
    "name": ("name", "full_name", "your-name", "your_name", "first_name", "fullname"),
    "email": ("email", "your-email", "your_email", "email_address", "e-mail"),
    "phone": ("phone", "your-phone", "mobile", "tel", "telephone", "phone_number"),
    "suburb": ("suburb", "location", "address", "your-suburb", "city"),
    "service": ("service", "services", "service_required", "your-service", "job_type"),
    "message": ("message", "your-message", "comments", "details", "enquiry", "description"),
    "id": ("id", "entry_id", "submission_id", "form_entry_id"),
}
_EMAIL_OK = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class IntakeError(ValueError):
    pass


def normalise_form(data: dict[str, Any]) -> dict[str, str]:
    lowered = {str(k).strip().lower(): v for k, v in data.items()}
    out: dict[str, str] = {}
    for field, aliases in FIELD_ALIASES.items():
        for a in aliases:
            v = lowered.get(a)
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            if v not in (None, ""):
                out[field] = str(v).strip()[:5000]
                break
        out.setdefault(field, "")
    return out


def intake_web_form(ctx: Context, approvals: ApprovalManager, data: dict[str, Any]) -> dict[str, Any]:
    f = normalise_form(data)
    if f["email"] and not _EMAIL_OK.match(f["email"]):
        raise IntakeError("invalid email address")
    if not (f["email"] or f["phone"]):
        raise IntakeError("an email or phone number is required")
    if not (f["message"] or f["service"]):
        raise IntakeError("a message or service is required")

    ext = f["id"] or hashlib.sha256(
        f"{f['email']}|{f['phone']}|{f['message']}|{f['service']}".lower().encode()).hexdigest()[:32]
    st = ctx.storage
    if st.enquiry_exists("website", ext):
        return {"status": "duplicate"}

    text = f"{f['service']}\n{f['suburb']}\n{f['message']}"
    lead = sales.analyse(text, name=f["name"], email=f["email"], phone=f["phone"],
                         suburb=sales.detect_suburb(f["suburb"]) or f["suburb"],
                         service=sales.detect_service(f["service"]) or f["service"])
    lead_id, created = st.upsert_lead(lead, {"source": "website", "external_id": ext,
                                             "subject": f"Website enquiry: {f['service']}",
                                             "body": f["message"], "category": "enquiry"})
    result: dict[str, Any] = {"status": "ok", "lead_id": lead_id, "new_lead": created,
                              "summary_ar": sales.owner_summary_ar(lead)}
    if lead["status"] == "spam":
        st.log_action("sales", "website form flagged as spam", "skipped", f"lead #{lead_id}")
        result["spam"] = True
        return result

    reason = escalation_reason(text)
    if reason:
        approvals.propose("owner_attention", f"Escalation ({reason}) from website form",
                          {"reason": reason, "note_ar": result["summary_ar"]}, f"attn:web:{ext}", lead_id)
        result["escalation"] = reason
        return result

    if f["email"]:
        body, provider = sales.draft_reply(ctx, lead, f["message"] or f["service"])
        subject = "Your enquiry with " + ctx.settings.business_name
        res = approvals.propose("email_reply", f"Reply to {lead['name'] or f['email']} (website form)",
                                {"to": f["email"], "subject": subject, "body": body, "provider": provider,
                                 "summary_ar": result["summary_ar"]},
                                f"reply:web:{ext}", lead_id)
        result["reply"] = res["status"]
    else:
        approvals.propose("owner_attention", f"Call {lead['name'] or 'new lead'} — phone only",
                          {"note_ar": result["summary_ar"] + " — ماكو إيميل، لازم تتصل."},
                          f"attn:web:{ext}", lead_id)
        result["reply"] = "owner_to_call"
    return result
