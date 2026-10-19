"""Email agent: monitor Gmail, classify, create leads, draft replies for approval."""
from __future__ import annotations

import logging
from typing import Any

from ..approvals import ApprovalManager
from ..context import Context
from ..http_client import HttpError
from ..integrations.gmail import InboundEmail
from . import sales
from .support import escalation_reason

log = logging.getLogger(__name__)

PLATFORM_DOMAINS = {
    "hipages.com.au": "hipages",
    "serviceseeking.com.au": "ServiceSeeking",
    "oneflare.com.au": "Oneflare",
    "airtasker.com": "Airtasker",
    "nextdoor.com": "Nextdoor",
    "facebookmail.com": "Facebook",
}
NOTIFICATION_MARKERS = ("noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "notifications@")
SUPPLIER_WORDS = ("invoice", "statement", "order confirmation", "delivery", "dispatch", "remittance", "purchase order")
ENQUIRY_WORDS = ("quote", "quotation", "price", "inspect", "could you", "can you", "need someone", "looking for",
                 "how much", "available")


def platform_for(sender: str) -> str:
    domain = sender.rsplit("@", 1)[-1].lower()
    for d, name in PLATFORM_DOMAINS.items():
        if domain == d or domain.endswith("." + d):
            return name
    return ""


def classify(mail: InboundEmail) -> str:
    text = f"{mail.subject}\n{mail.body}"
    low = text.lower()
    if platform_for(mail.from_email):
        return "platform_lead"
    if sales.spam_score(text) >= 2:
        return "spam"
    if escalation_reason(text):
        return "escalation"
    is_enquiry = bool(sales.detect_service(text)) or any(w in low for w in ENQUIRY_WORDS)
    if any(m in mail.from_email for m in NOTIFICATION_MARKERS) or "unsubscribe" in low:
        return "notification"
    if any(w in low for w in SUPPLIER_WORDS) and not is_enquiry:
        return "supplier"
    if is_enquiry:
        return "enquiry"
    return "other"


def _reply_subject(subject: str) -> str:
    subject = subject.strip() or "Your enquiry"
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


class EmailAgent:
    name = "email"

    def __init__(self, ctx: Context, approvals: ApprovalManager) -> None:
        self.ctx = ctx
        self.approvals = approvals

    def run(self, run_id: int | None = None) -> dict[str, Any]:
        summary: dict[str, Any] = {"fetched": 0, "new_leads": 0, "drafts": 0, "escalations": 0,
                                   "supplier": 0, "ignored": 0, "owner_notes_ar": []}
        if not self.ctx.gmail.configured:
            summary["skipped"] = "Gmail not configured"
            return summary
        mails = self.ctx.gmail.list_new(skip=lambda mid: self.ctx.storage.is_processed("gmail", mid))  # HttpError propagates to the CEO (integration failure)
        summary["fetched"] = len(mails)
        for mail in mails:
            if self.ctx.storage.is_processed("gmail", mail.id):
                continue
            try:
                category = self.handle(mail, summary, run_id)
            except HttpError as exc:
                # Leave unprocessed so the next cycle retries this message.
                self.ctx.storage.log_action(self.name, f"process email {mail.id}", "failed", str(exc), run_id)
                continue
            self.ctx.storage.mark_processed("gmail", mail.id, category)
        return summary

    def handle(self, mail: InboundEmail, summary: dict[str, Any], run_id: int | None) -> str:
        st = self.ctx.storage
        category = classify(mail)
        text = f"{mail.subject}\n{mail.body}"
        if category in ("spam", "notification", "other"):
            summary["ignored"] += 1
            st.log_action(self.name, f"classified email as {category}", "skipped", mail.subject[:120], run_id)
            return category
        if category == "supplier":
            summary["supplier"] += 1
            st.log_action(self.name, "supplier message — owner to review in Gmail", "pending",
                          mail.subject[:120], run_id)
            return category

        if category == "platform_lead":
            platform = platform_for(mail.from_email)
            lead = sales.analyse(text)
            lead["next_action"] = f"Reply inside the {platform} app (platform emails can't be answered directly)"
            lead_id, created = st.upsert_lead(lead, {"source": platform, "external_id": mail.id,
                                                     "subject": mail.subject, "body": mail.body[:5000],
                                                     "thread_id": mail.thread_id, "category": category})
            summary["new_leads"] += int(created)
            note = f"[{platform}] " + sales.owner_summary_ar(lead)
            summary["owner_notes_ar"].append(note)
            self.approvals.propose("owner_attention", f"{platform} lead: {lead['service'] or mail.subject[:60]}",
                                   {"note_ar": note, "subject": mail.subject}, f"attn:gmail:{mail.id}",
                                   lead_id, run_id)
            return category

        sender_email = mail.reply_to or mail.from_email
        lead = sales.analyse(text, name=mail.from_name, email=sender_email)
        lead_id, created = st.upsert_lead(lead, {"source": "gmail", "external_id": mail.id, "subject": mail.subject,
                                                 "body": mail.body[:5000], "thread_id": mail.thread_id,
                                                 "category": category})
        summary["new_leads"] += int(created)

        if category == "escalation":
            reason = escalation_reason(text)
            summary["escalations"] += 1
            note = f"⚠️ {reason}: {mail.from_name or sender_email} — {mail.subject[:80]}. لازم ترد بنفسك."
            summary["owner_notes_ar"].append(note)
            self.approvals.propose("owner_attention", f"Escalation ({reason}): {mail.subject[:60]}",
                                   {"reason": reason, "note_ar": note, "from": sender_email},
                                   f"attn:gmail:{mail.id}", lead_id, run_id)
            return category

        summary["owner_notes_ar"].append(sales.owner_summary_ar(lead))
        body, provider = sales.draft_reply(self.ctx, lead, mail.body)
        subject = _reply_subject(mail.subject)
        payload: dict[str, Any] = {
            "to": sender_email, "subject": subject, "body": body, "thread_id": mail.thread_id,
            "in_reply_to": mail.message_id_header, "references": mail.references, "provider": provider,
            "summary_ar": sales.owner_summary_ar(lead),
        }
        try:
            payload["draft_id"] = self.ctx.gmail.create_draft(
                sender_email, subject, body, mail.thread_id, mail.message_id_header, mail.references)
        except HttpError as exc:
            # Draft in Gmail is a convenience; the approval still holds the full text.
            st.log_action(self.name, "create Gmail draft", "failed", str(exc), run_id)
        self.approvals.propose("email_reply", f"Reply to {lead['name'] or sender_email}: {subject[:60]}",
                               payload, f"reply:gmail:{mail.id}", lead_id, run_id)
        summary["drafts"] += 1
        return category
