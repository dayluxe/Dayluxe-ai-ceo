"""Marketing agent: weekly Facebook/Instagram calendar, image concepts, ad plans for review."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..approvals import ApprovalManager
from ..context import Context

try:  # zoneinfo needs tz data; fall back to a fixed AEST offset if it's missing.
    from zoneinfo import ZoneInfo

    def local_today(tz: str) -> date:
        try:
            return datetime.now(ZoneInfo(tz)).date()
        except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError or bad name
            return datetime.now(timezone(timedelta(hours=10))).date()
except ImportError:  # pragma: no cover
    def local_today(tz: str) -> date:
        return datetime.now(timezone(timedelta(hours=10))).date()


SEASONS = {
    # Southern hemisphere (Sydney)
    "spring": ((9, 10, 11), "Storm season is coming — get gutters cleared and the roof checked before summer storms."),
    "summer": ((12, 1, 2), "Summer storms and heat — a quick roof and gutter check can catch problems early."),
    "autumn": ((3, 4, 5), "Leaves are dropping — blocked gutters and downpipes are the #1 cause of overflow."),
    "winter": ((6, 7, 8), "Winter rain shows up leaks — book an inspection before small leaks become big repairs."),
}

THEMES = [
    ("tip", "Roofing tip", "Share one practical roof or gutter maintenance tip homeowners can check from the ground "
                           "(never suggest climbing on the roof)."),
    ("service", "Service spotlight", "Explain one of our services (rotate: roof restoration, gutter guards, fascia, "
                                     "roof painting, pergola/carport repairs) and who it suits."),
    ("seasonal", "Seasonal", "A seasonal reminder for Sydney homeowners."),
    ("faq", "FAQ", "Answer a common customer question about how inspections and quotes work."),
    ("signs", "Warning signs", "List 3 signs a roof needs attention (cracked tiles, rust, stains on ceilings, "
                               "overflowing gutters)."),
]
SERVICE_ROTATION = ["roof restoration", "gutter guards", "fascia replacement", "roof painting",
                    "pergola and carport repairs", "gutter cleaning", "roof repairs"]
POST_WEEKDAYS = [0, 2, 4, 1, 3]  # Mon, Wed, Fri, then Tue, Thu


def season_for(d: date) -> tuple[str, str]:
    for name, (months, line) in SEASONS.items():
        if d.month in months:
            return name, line
    return "spring", SEASONS["spring"][1]


def fallback_caption(ctx: Context, theme: str, d: date, idx: int) -> str:
    s = ctx.settings
    _, season_line = season_for(d)
    svc = SERVICE_ROTATION[(d.isocalendar()[1] + idx) % len(SERVICE_ROTATION)]
    cta = f"📞 {s.business_phone} | " if s.business_phone else ""
    tags = "#SydneyRoofing #WesternSydney #RoofRestoration #Gutters #Dayluxe"
    bodies = {
        "tip": "Quick tip: after heavy rain, have a look at your ceilings and eaves from inside and the ground. "
               "Fresh stains or peeling paint can mean water is getting in somewhere.",
        "service": f"Thinking about {svc}? We look after homes across {s.service_area}. "
                   "Every job starts with an inspection so the quote matches what your place actually needs.",
        "seasonal": season_line,
        "faq": "How do quotes work? Send us your address and a few photos, we'll book a time to inspect, "
               "then you get a written quote to look over — no pressure.",
        "signs": "3 signs your roof needs attention:\n1) Cracked or slipped tiles\n2) Rust or lifting sheets\n"
                 "3) Gutters overflowing when it rains",
    }
    return f"{bodies[theme]}\n\n{cta}{s.business_website}\n{tags}"


class MarketingAgent:
    name = "marketing"

    def __init__(self, ctx: Context, approvals: ApprovalManager) -> None:
        self.ctx = ctx
        self.approvals = approvals

    def week_slots(self, today: date | None = None) -> list[tuple[date, int]]:
        today = today or local_today(self.ctx.settings.timezone)
        monday = today - timedelta(days=today.weekday())
        n = max(0, min(self.ctx.settings.posts_per_week, len(POST_WEEKDAYS)))
        days = sorted(POST_WEEKDAYS[:n])
        # Plan next week once we're past Thursday so the owner has time to review.
        if today.weekday() >= 4:
            monday += timedelta(days=7)
        return [(monday + timedelta(days=wd), i) for i, wd in enumerate(days)]

    def run(self, run_id: int | None = None, today: date | None = None) -> dict[str, Any]:
        st = self.ctx.storage
        summary: dict[str, Any] = {"planned": 0, "awaiting_approval": 0, "images": 0, "image_errors": []}
        for slot_date, idx in self.week_slots(today):
            year, week, _ = slot_date.isocalendar()
            theme_key, theme_label, theme_brief = THEMES[(week + idx) % len(THEMES)]
            key = f"social:{year}-W{week:02d}:{idx}"
            fallback = fallback_caption(self.ctx, theme_key, slot_date, idx)
            _, season_line = season_for(slot_date)
            prompt = (
                f"Write a Facebook/Instagram caption (60-110 words) for a Sydney roofing business. Theme: "
                f"{theme_label}. Brief: {theme_brief} Season context: {season_line} "
                "Friendly Aussie tone, 1-2 emojis max, end with a call to action to message us for an inspection, "
                f"then the website {self.ctx.settings.business_website} and 4-6 local hashtags. "
                "No prices, no discounts, no guarantees, no claims about specific past jobs."
            )
            if st.content_exists(key):
                continue
            res = self.ctx.llm.generate(prompt, fallback, max_tokens=300)
            caption = res.text
            image_file = ""
            if self.ctx.images.configured:
                image_file, err = self.ctx.images.generate(
                    f"Sydney suburban house roof, theme: {theme_label.lower()}, warm daylight")
                if image_file:
                    summary["images"] += 1
                elif err:
                    summary["image_errors"].append(err)
            content_id, created = st.add_content("social_post", f"{theme_label} — {slot_date.isoformat()}",
                                                 caption, key, "draft", slot_date.isoformat())
            if not created:
                continue
            summary["planned"] += 1
            if self.ctx.meta.configured:
                res_p = self.approvals.propose(
                    "social_post", f"Post for {slot_date:%a %d %b}: {theme_label}",
                    {"message": caption, "image_file": image_file, "content_id": content_id,
                     "scheduled_for": slot_date.isoformat()}, f"post:{key}", None, run_id)
                if res_p["status"] == "approval_required":
                    st.update_content(content_id, "pending_approval")
                    summary["awaiting_approval"] += 1
                elif res_p["status"] == "failed":
                    st.update_content(content_id, "failed")
            else:
                st.log_action(self.name, f"caption drafted for {slot_date}", "pending",
                              "Meta not connected — copy from dashboard and post manually", run_id)
        self._monthly_ad_plan(run_id, today)
        return summary

    def _monthly_ad_plan(self, run_id: int | None, today: date | None) -> None:
        today = today or local_today(self.ctx.settings.timezone)
        key = f"ads:{today:%Y-%m}"
        plan = (
            f"Proposed Meta ad campaign for {today:%B %Y} (FOR REVIEW — nothing is live, no money spent)\n"
            f"Objective: Leads (Messenger / lead form)\n"
            f"Audience: homeowners 30-65 within {self.ctx.settings.service_area}\n"
            f"Creative: genuine before/after photos of OUR completed jobs only (owner to supply)\n"
            f"Message: {season_for(today)[1]}\n"
            f"Daily budget: [OWNER TO DECIDE]\nDuration: [OWNER TO DECIDE]\n"
            f"Success metric: cost per qualified lead, measured from CRM, not clicks."
        )
        _, created = self.ctx.storage.add_content("ad_plan", f"Ad plan {today:%b %Y}", plan, key, "draft")
        if created:
            self.approvals.propose("ad_campaign", f"Ad plan {today:%B %Y}", {"plan": plan}, f"approve:{key}",
                                   None, run_id)
