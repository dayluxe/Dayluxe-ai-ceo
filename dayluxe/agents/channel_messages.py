"""Authenticated-channel message processing, independent of external API credentials.

A trusted channel adapter must authenticate the provider webhook before calling this
function. This module never sends a message to a customer by itself.
"""
from __future__ import annotations

from typing import Any

from ..approvals import ApprovalManager
from ..context import Context
from . import sales
from .support import escalation_reason

CHANNELS = frozenset({"facebook", "instagram", "whatsapp", "sms", "website_chat"})


def intake_message(ctx: Context, approvals: ApprovalManager, *,
                   channel: str, message_id: str, text: str,
                   sender_id: str = "", name: str = "", email: str = "",
                   phone: str = "") -> dict[str, Any]:
    """Record a channel enquiry, deduplicate, and prepare a reply for review.

    sender_id is an opaque provider identifier, never treated as an email address.
    This is a service-layer entry point, NOT a public unauthenticated webhook.
    """
    channel = channel.strip().lower()
    if channel not in CHANNELS:
        raise ValueError("unsupported message channel")
    if not isinstance(text, str) or not text.strip() or len(text) > 5000:
        raise ValueError("message must contain 1–5000 characters")
    if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 256:
        raise ValueError("invalid provider message id")
    if not (sender_id or email or phone):
        raise ValueError("sender identifier required")
    if email and ("@" not in email or len(email) > 254):
        raise ValueError("invalid email")
    source = "channel:" + channel
    external_id = message_id.strip()
    if ctx.storage.enquiry_exists(source, external_id):
        return {"status": "duplicate"}
    lead = sales.analyse(text, name=name, email=email, phone=phone)
    # The sender's opaque channel ID is not a substitute for a verified email
    # or phone number. Keep it on the enquiry record for human follow-up.
    lead_id, created = ctx.storage.upsert_lead(
        lead, {"source": source, "external_id": external_id,
               "subject": channel + " message", "body": text,
               "category": "enquiry"})
    summary = sales.owner_summary_ar(lead)
    result: dict[str, Any] = {
        "status": "ok", "lead_id": lead_id, "new_lead": created,
        "channel": channel, "summary_ar": summary,
    }
    if lead["status"] == "spam":
        result["spam"] = True
        return result
    reason = escalation_reason(text)
    if reason:
        approvals.propose("owner_attention", "Customer escalation: " + channel,
                          {"reason": reason, "note_ar": summary,
                           "sender_id": sender_id},
                          "attention:" + source + ":" + external_id, lead_id)
        result["escalation"] = reason
        return result
    reply, provider = sales.draft_reply(ctx, lead, text)
    # Channel replies are review-only. An email executor cannot safely send
    # them, and no provider send API is invoked here.
    approvals.propose("owner_attention", "Reply to " + channel + " enquiry",
                      {"note_ar": summary, "channel": channel,
                       "sender_id": sender_id, "reply_draft": reply,
                       "provider": provider},
                      "reply:" + source + ":" + external_id, lead_id)
    result["reply"] = "owner_review"
    return result
