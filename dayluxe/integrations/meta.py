"""Meta Graph API: Facebook Page posts and Instagram Business publishing.

Needs a long-lived Page access token with pages_manage_posts (+ instagram_content_publish
for Instagram). Instagram requires a publicly reachable image URL.
"""
from __future__ import annotations

from ..config import Settings
from ..http_client import HttpClient, HttpError


class Meta:
    def __init__(self, settings: Settings, http: HttpClient) -> None:
        self.s = settings
        self.http = http

    @property
    def configured(self) -> bool:
        return self.s.meta_configured

    @property
    def base(self) -> str:
        return f"https://graph.facebook.com/{self.s.meta_graph_version}"

    def health(self) -> tuple[str, str]:
        if not self.configured:
            return "not_configured", "META_PAGE_ID / META_PAGE_ACCESS_TOKEN not set"
        try:
            page = self.http.request(
                "GET", f"{self.base}/{self.s.meta_page_id}", params={"fields": "id,name"},
                bearer=self.s.meta_page_access_token, retry=False,
            )
            return "ok", f"page: {page.get('name', page.get('id', '?'))}"
        except HttpError as exc:
            return "error", str(exc)

    def publish_facebook(self, message: str, image_url: str = "") -> str:
        if image_url:
            data = self.http.request(
                "POST", f"{self.base}/{self.s.meta_page_id}/photos",
                form={"url": image_url, "caption": message}, bearer=self.s.meta_page_access_token, retry=False,
            )
            post_id = data.get("post_id") or data.get("id")
        else:
            data = self.http.request(
                "POST", f"{self.base}/{self.s.meta_page_id}/feed",
                form={"message": message}, bearer=self.s.meta_page_access_token, retry=False,
            )
            post_id = data.get("id")
        if not post_id:
            raise HttpError("server", "Facebook did not confirm publication")
        return str(post_id)

    def publish_instagram(self, caption: str, image_url: str) -> str:
        if not self.s.meta_ig_user_id:
            raise HttpError("client", "META_IG_USER_ID not set")
        if not image_url:
            raise HttpError("client", "Instagram posts need a public image URL")
        container = self.http.request(
            "POST", f"{self.base}/{self.s.meta_ig_user_id}/media",
            form={"image_url": image_url, "caption": caption}, bearer=self.s.meta_page_access_token, retry=False,
        )
        creation_id = container.get("id")
        if not creation_id:
            raise HttpError("server", "Instagram did not return a media container")
        published = self.http.request(
            "POST", f"{self.base}/{self.s.meta_ig_user_id}/media_publish",
            form={"creation_id": creation_id}, bearer=self.s.meta_page_access_token, retry=False,
        )
        media_id = published.get("id")
        if not media_id:
            raise HttpError("server", "Instagram did not confirm publication")
        return str(media_id)
