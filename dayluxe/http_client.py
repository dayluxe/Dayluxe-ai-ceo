"""Small HTTP client on top of urllib with timeouts, classified errors and backoff.

Retry rules (section 15 of the master prompt):
- Retries with exponential backoff: timeouts, network errors, HTTP 408/429 (rate limit)/5xx.
- Never retried: authentication errors (401/403), quota / insufficient credit (402, or
  429 whose body says quota/billing/credits), and other 4xx client errors.
Error messages never include request headers, bodies or query strings (secrets live there).
"""
from __future__ import annotations

import base64
import json
import logging
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

log = logging.getLogger(__name__)

Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, dict[str, str], bytes]]

QUOTA_MARKERS = (
    "insufficient_quota",
    "exceeded your current quota",
    "billing",
    "credit",
    "quota exceeded",
    "out of quota",
)


class HttpError(Exception):
    def __init__(self, kind: str, message: str, status: int = 0, retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind  # auth | quota | rate_limit | timeout | network | client | server
        self.status = status
        self.retryable = retryable

    def __str__(self) -> str:
        code = f" HTTP {self.status}" if self.status else ""
        return f"[{self.kind}{code}] {self.args[0]}"


def safe_url(url: str) -> str:
    """URL without query string/fragment — safe to log."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https URLs from config)
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        hdrs = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
        return exc.code, hdrs, exc.read() or b""


def _short(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
        err = data.get("error", data) if isinstance(data, dict) else data
        if isinstance(err, dict):
            text = str(err.get("message") or err.get("error_description") or err.get("status") or err)
        else:
            text = str(err)
    except (json.JSONDecodeError, AttributeError):
        pass
    return text.strip().replace("\n", " ")[:300]


def classify(status: int, body: bytes) -> HttpError:
    msg = _short(body) or f"HTTP {status}"
    lower = body.decode("utf-8", errors="replace").lower()
    if status in (401, 403):
        return HttpError("auth", msg, status, retryable=False)
    if status == 402:
        return HttpError("quota", msg, status, retryable=False)
    if status == 429:
        if any(m in lower for m in QUOTA_MARKERS):
            return HttpError("quota", msg, status, retryable=False)
        return HttpError("rate_limit", msg, status, retryable=True)
    if status == 408:
        return HttpError("timeout", msg, status, retryable=True)
    if status >= 500:
        return HttpError("server", msg, status, retryable=True)
    return HttpError("client", msg, status, retryable=False)


class HttpClient:
    def __init__(
        self,
        timeout: float = 20,
        max_retries: int = 3,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.transport = transport or _urllib_transport
        self.sleep = sleep
        self.base_delay = base_delay
        self.max_delay = max_delay

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        form: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        basic_auth: tuple[str, str] | None = None,
        bearer: str | None = None,
        retry: bool = True,
    ) -> Any:
        """Perform a request and return parsed JSON (or text). Raises HttpError."""
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params)}"
        hdrs = {"Accept": "application/json", "User-Agent": "dayluxe-ai-ceo/1.0"}
        hdrs.update(headers or {})
        body: bytes | None = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        elif form is not None:
            body = urllib.parse.urlencode(form).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        if basic_auth:
            token = base64.b64encode(f"{basic_auth[0]}:{basic_auth[1]}".encode()).decode()
            hdrs["Authorization"] = f"Basic {token}"
        if bearer:
            hdrs["Authorization"] = f"Bearer {bearer}"

        attempts = (self.max_retries + 1) if retry else 1
        last: HttpError | None = None
        for attempt in range(attempts):
            try:
                status, resp_headers, raw = self.transport(method, url, hdrs, body, self.timeout)
            except (socket.timeout, TimeoutError) as exc:
                last = HttpError("timeout", f"timeout calling {safe_url(url)}", retryable=True)
                log.debug("timeout: %s", exc)
            except (urllib.error.URLError, ConnectionError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                last = HttpError("network", f"{safe_url(url)}: {reason}", retryable=True)
            else:
                if 200 <= status < 300:
                    if not raw:
                        return {}
                    text = raw.decode("utf-8", errors="replace")
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return text
                last = classify(status, raw)
                retry_after = resp_headers.get("retry-after", "")
                if last.retryable and retry_after.isdigit():
                    if attempt < attempts - 1:
                        self.sleep(min(float(retry_after), self.max_delay))
                        continue
            if not last.retryable or attempt >= attempts - 1:
                break
            delay = min(self.max_delay, self.base_delay * (2**attempt)) * (0.75 + random.random() * 0.5)
            log.info("retrying %s %s in %.1fs after %s", method, safe_url(url), delay, last.kind)
            self.sleep(delay)
        assert last is not None
        raise last
