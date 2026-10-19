"""Gmail via REST API + OAuth 2.0 refresh token.

Required OAuth scopes: https://www.googleapis.com/auth/gmail.readonly and
https://www.googleapis.com/auth/gmail.compose (drafts + sending approved replies).
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any, Callable
from urllib.parse import quote

from ..config import Settings
from ..http_client import HttpClient, HttpError

API = "https://gmail.googleapis.com/gmail/v1/users"
TOKEN_URL = "https://oauth2.googleapis.com/token"


@dataclass
class InboundEmail:
    id: str
    thread_id: str
    from_name: str
    from_email: str
    reply_to: str
    subject: str
    body: str
    message_id_header: str
    references: str = ""
    labels: list[str] = field(default_factory=list)


def _b64url_decode(data: str) -> str:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad).decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    import html as html_mod
    import re

    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def extract_body(payload: dict[str, Any]) -> str:
    """Prefer text/plain; fall back to stripped text/html. Walks multipart trees."""
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime == "text/plain":
            plain.append(_b64url_decode(data))
        elif data and mime == "text/html":
            html.append(_b64url_decode(data))
        for sub in part.get("parts") or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    if html:
        return _strip_html("\n".join(html))
    return ""


def parse_message(msg: dict[str, Any]) -> InboundEmail:
    payload = msg.get("payload") or {}
    headers = {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers") or []}
    from_name, from_email = parseaddr(headers.get("from", ""))
    _, reply_to = parseaddr(headers.get("reply-to", ""))
    return InboundEmail(
        id=msg.get("id", ""),
        thread_id=msg.get("threadId", ""),
        from_name=from_name,
        from_email=from_email.lower(),
        reply_to=(reply_to or from_email).lower(),
        subject=headers.get("subject", ""),
        body=extract_body(payload)[:20000],
        message_id_header=headers.get("message-id", ""),
        references=headers.get("references", ""),
        labels=list(msg.get("labelIds") or []),
    )


def build_raw(
    to: str, subject: str, body: str, sender: str = "", in_reply_to: str = "", references: str = ""
) -> str:
    m = EmailMessage()
    m["To"] = to
    if sender:
        m["From"] = sender
    m["Subject"] = subject
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = f"{references} {in_reply_to}".strip()
    m.set_content(body)
    return base64.urlsafe_b64encode(m.as_bytes()).decode("ascii")


class Gmail:
    def __init__(self, settings: Settings, http: HttpClient) -> None:
        self.s = settings
        self.http = http
        self._token = ""
        self._token_exp = 0.0

    @property
    def configured(self) -> bool:
        return self.s.gmail_configured

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        data = self.http.request(
            "POST",
            TOKEN_URL,
            form={
                "client_id": self.s.google_client_id,
                "client_secret": self.s.google_client_secret,
                "refresh_token": self.s.google_refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if not isinstance(data, dict) or "access_token" not in data:
            raise HttpError("auth", "Google token endpoint returned no access_token")
        self._token = data["access_token"]
        self._token_exp = time.time() + int(data.get("expires_in", 3600))
        return self._token

    def _url(self, path: str) -> str:
        return f"{API}/{quote(self.s.gmail_user, safe='@.')}/{path}"

    def _call(self, method: str, path: str, **kw: Any) -> Any:
        try:
            return self.http.request(method, self._url(path), bearer=self._access_token(), **kw)
        except HttpError as exc:
            if exc.kind == "auth" and self._token:
                # Token may have been revoked/expired early: refresh once and retry.
                self._token = ""
                return self.http.request(method, self._url(path), bearer=self._access_token(), **kw)
            raise

    def health(self) -> tuple[str, str]:
        if not self.configured:
            return "not_configured", "GOOGLE_CLIENT_ID / SECRET / REFRESH_TOKEN not set"
        try:
            prof = self._call("GET", "profile", retry=False)
            return "ok", f"connected as {prof.get('emailAddress', 'unknown')}"
        except HttpError as exc:
            return "error", str(exc)

    def list_new(self, skip: Callable[[str], bool] | None = None) -> list[InboundEmail]:
        """Fetch matching messages; `skip(id)` avoids downloading ones already processed."""
        data = self._call(
            "GET", "messages", params={"q": self.s.gmail_query, "maxResults": self.s.gmail_max_messages}
        )
        out: list[InboundEmail] = []
        for ref in (data or {}).get("messages", []) or []:
            if skip and skip(ref["id"]):
                continue
            msg = self._call("GET", f"messages/{ref['id']}", params={"format": "full"})
            out.append(parse_message(msg))
        return out

    def create_draft(self, to: str, subject: str, body: str, thread_id: str = "", in_reply_to: str = "",
                     references: str = "") -> str:
        message: dict[str, Any] = {"raw": build_raw(to, subject, body, "", in_reply_to, references)}
        if thread_id:
            message["threadId"] = thread_id
        data = self._call("POST", "drafts", json_body={"message": message}, retry=False)
        draft_id = (data or {}).get("id", "")
        if not draft_id:
            raise HttpError("server", "Gmail did not return a draft id")
        return draft_id

    def send_draft(self, draft_id: str) -> str:
        data = self._call("POST", "drafts/send", json_body={"id": draft_id}, retry=False)
        msg_id = (data or {}).get("id", "")
        if not msg_id:
            raise HttpError("server", "Gmail did not confirm the send")
        return msg_id

    def send(self, to: str, subject: str, body: str, thread_id: str = "", in_reply_to: str = "",
             references: str = "") -> str:
        message: dict[str, Any] = {"raw": build_raw(to, subject, body, "", in_reply_to, references)}
        if thread_id:
            message["threadId"] = thread_id
        # retry=False: never risk a duplicate send on an ambiguous network failure.
        data = self._call("POST", "messages/send", json_body=message, retry=False)
        msg_id = (data or {}).get("id", "")
        if not msg_id:
            raise HttpError("server", "Gmail did not confirm the send")
        return msg_id
