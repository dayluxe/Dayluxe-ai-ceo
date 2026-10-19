"""The CEO: runs the operating cycle (section 3) and produces the report (section 16).

Each step is isolated: one failing integration never stops independent steps, and the
run is reported as 'ok', 'partial' or 'failed' honestly.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from typing import Any, Callable

from .agents import sales
from .agents.bizdev import BizDevAgent
from .agents.email_agent import EmailAgent
from .agents.marketing import MarketingAgent
from .agents.seo import SEOAgent
from .approvals import ApprovalManager
from .context import Context
from .http_client import HttpError

log = logging.getLogger(__name__)


class CEO:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.approvals = ApprovalManager(ctx)
        self.email = EmailAgent(ctx, self.approvals)
        self.marketing = MarketingAgent(ctx, self.approvals)
        self.seo = SEOAgent(ctx, self.approvals)
        self.bizdev = BizDevAgent(ctx, self.approvals)
        self._run_lock = threading.Lock()
        self._health_cache: tuple[float, dict[str, dict[str, str]]] | None = None

    # ------------------------------------------------------------ health
    def health(self, max_age: float = 0) -> dict[str, dict[str, str]]:
        """Run health checks. With max_age > 0, reuse a recent result (dashboard page loads)."""
        if max_age and self._health_cache and time.monotonic() - self._health_cache[0] < max_age:
            return self._health_cache[1]
        result = self._health()
        self._health_cache = (time.monotonic(), result)
        return result

    def _health(self) -> dict[str, dict[str, str]]:
        st = self.ctx.storage
        storage_detail = "SQLite OK" + ("" if st.persistent else " (in-memory: data is NOT persisted)")
        checks: dict[str, Callable[[], tuple[str, str]]] = {
            "text_ai": self.ctx.llm.health,
            "gmail": self.ctx.gmail.health,
            "wordpress": self.ctx.wordpress.health,
            "meta": self.ctx.meta.health,
            "images": self.ctx.images.health,
        }
        out = {"storage": {"status": "ok" if st.healthy() else "error", "detail": storage_detail}}
        for name, fn in checks.items():
            try:
                status, detail = fn()
            except Exception as exc:  # noqa: BLE001 — a health check must never crash the run
                status, detail = "error", f"{exc.__class__.__name__}: {exc}"
            out[name] = {"status": status, "detail": detail}
        return out

    # --------------------------------------------------------------- run
    def run_cycle(self, trigger: str = "manual") -> dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return {"status": "skipped", "reason": "another run is in progress"}
        try:
            return self._run(trigger)
        finally:
            self._run_lock.release()

    def _step(self, report: dict[str, Any], name: str, fn: Callable[[], Any], run_id: int,
              enabled: bool = True, why_disabled: str = "") -> None:
        if not enabled:
            report["steps"][name] = {"status": "skipped", "detail": why_disabled}
            return
        try:
            result = fn()
            report["steps"][name] = {"status": "completed", "result": result}
        except HttpError as exc:
            report["steps"][name] = {"status": "failed", "detail": str(exc)}
            self.ctx.storage.log_action(name, "step failed", "failed", str(exc), run_id)
        except Exception as exc:  # noqa: BLE001 — continue independent tasks
            log.error("step %s crashed: %s", name, traceback.format_exc())
            report["steps"][name] = {"status": "failed", "detail": f"{exc.__class__.__name__}: {exc}"}
            self.ctx.storage.log_action(name, "step crashed", "failed", f"{exc.__class__.__name__}: {exc}", run_id)

    def _run(self, trigger: str) -> dict[str, Any]:
        st = self.ctx.storage
        run_id = st.start_run(trigger)
        self.ctx.llm.disabled_reason = ""  # give providers a fresh chance each cycle
        report: dict[str, Any] = {"run_id": run_id, "trigger": trigger, "steps": {}}

        health = self.health()
        report["health"] = health
        ok = lambda k: health[k]["status"] == "ok"  # noqa: E731

        self._step(report, "email", lambda: self.email.run(run_id), run_id,
                   ok("gmail"), f"Gmail {health['gmail']['status']}: {health['gmail']['detail']}")
        self._step(report, "followups", lambda: self.followups(run_id), run_id)
        self._step(report, "marketing", lambda: self.marketing.run(run_id), run_id)
        wp_ok = ok("wordpress") or health["wordpress"]["status"] == "not_configured"
        self._step(report, "seo", lambda: self.seo.run(run_id), run_id,
                   wp_ok, f"WordPress error: {health['wordpress']['detail']}")
        self._step(report, "bizdev", lambda: self.bizdev.run(run_id), run_id)

        report.update(self.dashboard_data())
        failed = [k for k, v in report["steps"].items() if v["status"] == "failed"]
        status = "ok" if not failed else ("failed" if len(failed) == len(report["steps"]) else "partial")
        report["status"] = status
        report["failed_steps"] = failed
        st.finish_run(run_id, status, report)
        st.log_action("ceo", f"cycle #{run_id} ({trigger})", "completed" if status == "ok" else "failed",
                      f"status={status}" + (f"; failed: {', '.join(failed)}" if failed else ""), run_id)
        return report

    # ---------------------------------------------------------- followups
    def followups(self, run_id: int | None = None) -> dict[str, Any]:
        st = self.ctx.storage
        s = self.ctx.settings
        drafted = 0
        for lead in st.stale_contacted_leads(s.followup_after_days)[:10]:
            if not lead["email"] or st.has_pending_approval(lead["id"]):
                continue
            n = len([e for e in st.lead_enquiries(lead["id"])]) + 1
            first = (lead["name"] or "").split(" ")[0] or "there"
            what = "the quote we sent" if lead["status"] == "quoted" else "your enquiry"
            fallback = (
                f"Hi {first},\n\nJust following up on {what}"
                + (f" for {lead['service'].lower()}" if lead["service"] else "")
                + ". Let us know if you have any questions or if you'd like to go ahead — happy to help.\n\n"
                f"Cheers,\n{s.email_signature}" + (f"\nPh: {s.business_phone}" if s.business_phone else "")
            )
            prompt = (f"Write a brief, friendly follow-up email (under 70 words) about {what}"
                      f"{' for ' + lead['service'] if lead['service'] else ''}. No pressure, no discounts, "
                      f"no new promises. Greet {first}. Sign off with:\n{s.email_signature}")
            body = self.ctx.llm.generate(prompt, fallback, max_tokens=200).text
            res = self.approvals.propose("followup", f"Follow-up: {lead['name'] or lead['email']}",
                                         {"to": lead["email"], "subject": f"Following up — {s.business_name}",
                                          "body": body}, f"followup:{lead['id']}:{lead['updated_at'][:10]}:{n}",
                                         lead["id"], run_id)
            if res["status"] != "duplicate":
                drafted += 1
                st.touch_lead(lead["id"])  # don't draft again until another FOLLOWUP_AFTER_DAYS passes
        return {"drafted": drafted}

    # ---------------------------------------------------------- reporting
    def dashboard_data(self) -> dict[str, Any]:
        st = self.ctx.storage
        new_leads = st.list_leads("new", 50)
        priorities = []
        for lead in new_leads[:5]:
            priorities.append({"lead_id": lead["id"], "name": lead["name"] or lead["email"] or lead["phone"],
                               "score": lead["score"], "next_action": lead["next_action"],
                               "summary_ar": sales.owner_summary_ar(lead)})
        pending = st.list_approvals("pending", 100)
        actions = st.recent_actions(40)
        counts = st.counts()
        next_steps = []
        if counts["approvals_pending"]:
            next_steps.append(f"Review {counts['approvals_pending']} item(s) waiting for your approval.")
        urgent = [l for l in new_leads if l["urgency"] == "urgent"]
        if urgent:
            next_steps.append(f"Call {len(urgent)} urgent lead(s) today.")
        if counts["actions_failed_24h"]:
            next_steps.append(f"Check {counts['actions_failed_24h']} failed action(s) in the last 24h.")
        if not next_steps:
            next_steps.append("Nothing urgent. Add genuine job photos for next week's posts.")
        return {
            "counts": counts,
            "priorities": priorities,
            "pending_approvals": [
                {"id": a["id"], "kind": a["kind"], "title": a["title"], "created_at": a["created_at"]}
                for a in pending
            ],
            "recent_actions": actions,
            "next_steps": next_steps,
        }
