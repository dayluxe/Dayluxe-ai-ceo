"""WordPress REST API using an Application Password (Users → Profile → Application Passwords)."""
from __future__ import annotations

import html
from typing import Any

from ..config import Settings
from ..http_client import HttpClient, HttpError


class WordPress:
    def __init__(self, settings: Settings, http: HttpClient) -> None:
        self.s = settings
        self.http = http

    @property
    def configured(self) -> bool:
        return self.s.wordpress_configured

    def _call(self, method: str, path: str, **kw: Any) -> Any:
        return self.http.request(
            method,
            f"{self.s.wp_base_url}/wp-json/wp/v2/{path}",
            basic_auth=(self.s.wp_username, self.s.wp_app_password),
            **kw,
        )

    def health(self) -> tuple[str, str]:
        if not self.configured:
            return "not_configured", "WP_BASE_URL / WP_USERNAME / WP_APP_PASSWORD not set"
        try:
            me = self._call("GET", "users/me", params={"context": "edit"}, retry=False)
            caps = me.get("capabilities", {}) if isinstance(me, dict) else {}
            if caps and not caps.get("edit_pages"):
                return "error", "WordPress user cannot edit pages"
            return "ok", f"connected as {me.get('name', 'user')}"
        except HttpError as exc:
            return "error", str(exc)

    def list_pages(self) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        page = 1
        while page <= 10:
            batch = self._call(
                "GET",
                "pages",
                params={
                    "per_page": 100,
                    "page": page,
                    "status": "publish,draft,pending",
                    "context": "edit",
                    "_fields": "id,title,slug,status,link,content,excerpt",
                },
            )
            if not isinstance(batch, list) or not batch:
                break
            pages.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return pages

    @staticmethod
    def page_title(page: dict[str, Any]) -> str:
        t = page.get("title", "")
        if isinstance(t, dict):
            t = t.get("raw") or t.get("rendered") or ""
        return html.unescape(str(t)).strip()

    def find_by_slug(self, slug: str) -> dict[str, Any] | None:
        res = self._call(
            "GET", "pages", params={"slug": slug, "status": "publish,draft,pending", "context": "edit"}
        )
        return res[0] if isinstance(res, list) and res else None

    def create_draft_page(self, title: str, slug: str, content_html: str, excerpt: str = "") -> int:
        data = self._call(
            "POST",
            "pages",
            json_body={"title": title, "slug": slug, "content": content_html, "excerpt": excerpt, "status": "draft"},
            retry=False,
        )
        page_id = int((data or {}).get("id") or 0)
        if not page_id:
            raise HttpError("server", "WordPress did not return a page id")
        return page_id

    def publish_page(self, page_id: int) -> str:
        data = self._call("POST", f"pages/{int(page_id)}", json_body={"status": "publish"}, retry=False)
        if (data or {}).get("status") != "publish":
            raise HttpError("server", "WordPress did not confirm publication")
        return str(data.get("link", ""))
