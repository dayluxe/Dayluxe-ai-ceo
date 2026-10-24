"""Autopilot and approval policy (section 14 of the master prompt).

An action runs without review ONLY if:
  * its AUTO_* switch is true, AND
  * CEO_APPROVAL_REQUIRED is false, AND
  * the kind is eligible for autopilot at all (ads, outreach, escalations and
    incomplete quotes never are).
Everything else becomes a pending approval. Who approved, when, and whether the
execution succeeded are stored on the approval record.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .context import Context
from .http_client import HttpError

log = logging.getLogger(__name__)

KINDS = {
    "email_reply": "Email reply to customer",
    "followup": "Follow-up email",
    "quote": "Quotation",
    "social_post": "Social media post",
    "seo_page_publish": "Publish website page",
    "outreach_email": "Business-development outreach",
    "ad_campaign": "Paid advertising campaign",
    "owner_attention": "Needs owner attention",
}

EMAIL_KINDS = {"email_reply", "followup", "quote", "outreach_email"}


class ApprovalError(Exception):
    pass


class ApprovalManager:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------- policy
    def auto_allowed(self, kind: str, payload: dict[str, Any]) -> bool:
        s = self.ctx.settings
        # Social autopublishing is an explicit, independent opt-in. Other action
        # types still require CEO_APPROVAL_REQUIRED=false plus their own AUTO_* switch.
        if kind == "social_post":
            return s.auto_publish_social
        if s.ceo_approval_required:
            return False
        if kind in ("email_reply", "followup"):
            return s.auto_send_email and not payload.get("sensitive")
        if kind == "quote":
            return s.auto_send_quotes and not payload.get("missing")
        # seo_page_publish (significant website change), outreach_email (bulk/compliance),
        # ad_campaign (money) and owner_attention are never automatic.
        return False

    # ------------------------------------------------------------ propose
    def propose(self, kind: str, title: str, payload: dict[str, Any], key: str,
                lead_id: int | None = None, run_id: int | None = None) -> dict[str, Any]:
        if kind not in KINDS:
            raise ValueError(f"unknown approval kind {kind!r}")
        st = self.ctx.storage
        approval_id, created = st.create_approval(kind, title, payload, key, lead_id)
        if not created:
            return {"id": approval_id, "status": "duplicate"}
        if self.auto_allowed(kind, payload):
            st.claim_approval(approval_id, "approved", "autopilot")
            ok, result = self._execute(approval_id)
            st.log_action("autopilot", f"{kind}: {title}", "completed" if ok else "failed", result, run_id)
            return {"id": approval_id, "status": "executed" if ok else "failed", "result": result}
        st.log_action("ceo", f"{kind}: {title}", "approval_required", f"approval #{approval_id}", run_id)
        return {"id": approval_id, "status": "approval_required"}

    # ------------------------------------------------------------- decide
    def decide(self, approval_id: int, approve: bool, decided_by: str,
               edits: dict[str, Any] | None = None) -> dict[str, Any]:
        st = self.ctx.storage
        item = st.get_approval(approval_id)
        if not item:
            raise ApprovalError("approval not found")
        if item["status"] != "pending":
            raise ApprovalError(f"approval already {item['status']}")
        if edits:
            self._apply_edits(item, edits)
        if approve and item["kind"] == "quote" and item["payload"].get("missing"):
            raise ApprovalError("quote is incomplete: " + "; ".join(item["payload"]["missing"]))
        if not st.claim_approval(approval_id, "approved" if approve else "rejected", decided_by):
            raise ApprovalError("approval was decided by someone else")
        if not approve:
            st.log_action("owner", f"rejected {item['kind']}: {item['title']}", "skipped", f"by {decided_by}")
            return {"id": approval_id, "status": "rejected"}
        ok, result = self._execute(approval_id)
        st.log_action(
            "owner", f"approved {item['kind']}: {item['title']}", "completed" if ok else "failed",
            f"by {decided_by}: {result}",
        )
        final = st.get_approval(approval_id)
        return {"id": approval_id, "status": final["status"] if final else "unknown", "result": result}

    def _apply_edits(self, item: dict[str, Any], edits: dict[str, Any]) -> None:
        allowed = {"subject", "body", "message"}
        clean = {k: v.replace("\r\n", "\n") for k, v in edits.items()
                 if k in allowed and isinstance(v, str) and v.strip()}
        clean = {k: v for k, v in clean.items() if v != item["payload"].get(k)}
        if not clean:
            return
        payload = dict(item["payload"])
        payload.update(clean)
        # Edited text must not be sent through a stale Gmail draft.
        payload.pop("draft_id", None)
        with self.ctx.storage.tx() as c:
            c.execute("UPDATE approvals SET payload=? WHERE id=?", (json.dumps(payload), item["id"]))
        item["payload"] = payload

    # ------------------------------------------------------------ execute
    def _execute(self, approval_id: int) -> tuple[bool, str]:
        st = self.ctx.storage
        item = st.get_approval(approval_id)
        assert item is not None
        kind, p = item["kind"], item["payload"]
        try:
            if kind in EMAIL_KINDS:
                result = self._send_email(p)
                lead_id = item.get("lead_id")
                if lead_id:
                    st.update_lead_status(int(lead_id), "quoted" if kind == "quote" else "contacted")
                if kind == "outreach_email" and p.get("prospect_id"):
                    st.update_prospect_status(int(p["prospect_id"]), "contacted")
            elif kind == "social_post":
                result = self._publish_social(p)
            elif kind == "seo_page_publish":
                if not self.ctx.wordpress.configured:
                    raise ApprovalError("WordPress is not connected")
                link = self.ctx.wordpress.publish_page(int(p["wp_page_id"]))
                if p.get("content_id"):
                    st.update_content(int(p["content_id"]), "published", str(p["wp_page_id"]))
                result = f"published {link}"
            elif kind == "ad_campaign":
                result = ("Approved by owner. NOT launched: no ad platform is connected and this system never "
                          "spends money itself. Launch it manually in Meta Ads Manager.")
                st.finish_approval(approval_id, "approved", result)
                return True, result
            elif kind == "owner_attention":
                result = "Marked as handled by owner."
            else:
                raise ApprovalError(f"no executor for {kind}")
        except (HttpError, ApprovalError, KeyError, ValueError) as exc:
            msg = f"failed: {exc}"
            st.finish_approval(approval_id, "failed", msg)
            log.warning("approval %s %s", approval_id, msg)
            return False, msg
        st.finish_approval(approval_id, "executed", result)
        return True, result

    def _send_email(self, p: dict[str, Any]) -> str:
        gmail = self.ctx.gmail
        if not gmail.configured:
            raise ApprovalError("Gmail is not connected — copy the draft and send it manually")
        to = (p.get("to") or "").strip()
        if not to or "@" not in to:
            raise ApprovalError("no valid recipient email address")
        if p.get("draft_id"):
            msg_id = gmail.send_draft(p["draft_id"])
        else:
            msg_id = gmail.send(to, p["subject"], p["body"], p.get("thread_id", ""), p.get("in_reply_to", ""),
                                p.get("references", ""))
        return f"sent to {to} (Gmail id {msg_id})"

    def _publish_social(self, p: dict[str, Any]) -> str:
        meta = self.ctx.meta
        if not meta.configured:
            raise ApprovalError("Facebook/Instagram is not connected — post it manually")
        message = p["message"]
        image_url = self.ctx.images.public_url(p.get("image_file", ""))
        done: list[str] = []
        fb_id = meta.publish_facebook(message, image_url)
        done.append(f"Facebook post {fb_id}")
        ig_note = ""
        if self.ctx.settings.meta_ig_user_id:
            if image_url:
                try:
                    done.append(f"Instagram media {meta.publish_instagram(message, image_url)}")
                except HttpError as exc:
                    ig_note = f"; Instagram failed: {exc}"
            else:
                ig_note = "; Instagram skipped (needs an image with a public URL)"
        if p.get("content_id"):
            self.ctx.storage.update_content(int(p["content_id"]), "published", fb_id)
        return ", ".join(done) + ig_note
