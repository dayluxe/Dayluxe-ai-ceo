"""Website & SEO agent: review WordPress pages, draft one suburb page at a time for approval."""
from __future__ import annotations

import html
import re
from typing import Any

from ..approvals import ApprovalManager
from ..context import Context


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def review_pages(pages: list[dict[str, Any]], title_of) -> list[str]:  # noqa: ANN001
    issues: list[str] = []
    seen: dict[str, int] = {}
    for p in pages:
        if p.get("status") != "publish":
            continue
        title = title_of(p)
        if not title:
            issues.append(f"Page {p.get('id')} has no title")
            continue
        if len(title) > 60:
            issues.append(f"Title too long ({len(title)} chars, aim ≤60): “{title}”")
        key = title.lower()
        if key in seen:
            issues.append(f"Duplicate title “{title}” on pages {seen[key]} and {p.get('id')}")
        seen[key] = p.get("id", 0)
        content = p.get("content", {})
        raw = content.get("raw") if isinstance(content, dict) else str(content or "")
        words = len(re.sub(r"<[^>]+>", " ", raw or "").split())
        if words and words < 250:
            issues.append(f"Thin content ({words} words): “{title}”")
        if raw and "tel:" not in raw and "contact" not in raw.lower():
            issues.append(f"No call-to-action/contact link: “{title}”")
    return issues


def fallback_page(ctx: Context, suburb: str) -> str:
    s = ctx.settings
    sub = html.escape(suburb)
    phone = html.escape(s.business_phone) if s.business_phone else "[PHONE]"
    tel = re.sub(r"\D", "", s.business_phone)
    tel_link = f'<a href="tel:{tel}">{phone}</a>' if tel else phone
    return f"""<h2>Roof restoration, repairs and gutters in {sub}</h2>
<p>{html.escape(s.business_name)} looks after homes in {sub} and across {html.escape(s.service_area)}. We handle
roof restoration, repairs, painting, cleaning and sealing, plus gutters, gutter guards, downpipes and fascia.</p>
<h3>How it works</h3>
<ol><li>Tell us what's going on and send a few photos.</li>
<li>We book a time to inspect the roof at your {sub} property.</li>
<li>You get a written quote to review — no pressure.</li></ol>
<h3>Services in {sub}</h3>
<ul><li>Roof restoration and roof painting</li><li>Roof leak repairs, rebedding and repointing</li>
<li>Gutter cleaning, repairs, replacement and gutter guards</li><li>Fascia replacement and painting</li></ul>
<p>[OWNER: add 1–2 genuine photos and a short note about a real job you completed in or near {sub}.]</p>
<p><strong>Book an inspection:</strong> call {tel_link} or <a href="/contact/">send us a message</a>.</p>"""


class SEOAgent:
    name = "seo"

    def __init__(self, ctx: Context, approvals: ApprovalManager) -> None:
        self.ctx = ctx
        self.approvals = approvals

    def run(self, run_id: int | None = None) -> dict[str, Any]:
        wp = self.ctx.wordpress
        st = self.ctx.storage
        summary: dict[str, Any] = {"issues": [], "drafted": ""}
        existing_titles: list[str] = []
        existing_slugs: set[str] = set()
        if wp.configured:
            pages = wp.list_pages()
            summary["issues"] = review_pages(pages, wp.page_title)[:20]
            existing_titles = [wp.page_title(p).lower() for p in pages]
            existing_slugs = {str(p.get("slug", "")) for p in pages}
        target = None
        for suburb in self.ctx.settings.target_suburbs:
            slug = f"roofing-{slugify(suburb)}"
            if slug in existing_slugs or st.content_exists(f"seo:{slug}"):
                continue
            if any(suburb.lower() in t for t in existing_titles):
                continue
            target = suburb
            break
        if not target:
            return summary

        slug = f"roofing-{slugify(target)}"
        title = f"Roof Restoration & Repairs in {target}"
        fallback = fallback_page(self.ctx, target)
        tel = re.sub(r"\D", "", self.ctx.settings.business_phone)
        prompt = (
            f"Write the HTML body (h2/h3/p/ul only, 350-500 words) for a service page titled '{title}' for "
            f"{self.ctx.settings.business_name}. Make it genuinely useful to {target} homeowners (housing styles, "
            "weather, what an inspection covers). No prices, guarantees, rankings, fake reviews or invented jobs. "
            f"Include the placeholder [OWNER: add genuine local job photos]. End with a call to action linking to "
            "/contact/" + (f" and tel:{tel}" if tel else "") + "."
        )
        res = self.ctx.llm.generate(prompt, fallback, max_tokens=1200)
        body = res.text
        content_id, created = st.add_content("seo_page", title, body, f"seo:{slug}", "draft")
        if not created:
            return summary
        summary["drafted"] = title
        if wp.configured:
            page_id = wp.create_draft_page(title, slug, body,
                                           f"Roof restoration, repairs and gutters in {target}. Book an inspection.")
            st.update_content(content_id, "pending_approval", str(page_id))
            self.approvals.propose("seo_page_publish", f"Publish page: {title}",
                                   {"wp_page_id": page_id, "content_id": content_id, "slug": slug},
                                   f"publish:{slug}", None, run_id)
            st.log_action(self.name, f"WordPress draft created: {title}", "completed", f"page #{page_id}", run_id)
        else:
            st.log_action(self.name, f"page drafted locally: {title}", "pending",
                          "WordPress not connected — copy from dashboard", run_id)
        return summary
