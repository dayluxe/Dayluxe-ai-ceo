"""Text generation via OpenAI or Gemini, with a safe template fallback.

If no provider is configured or the provider fails, callers receive their own
pre-written fallback text, so lead management and drafting keep working.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Settings
from .http_client import HttpClient, HttpError

log = logging.getLogger(__name__)


def business_system_prompt(s: Settings) -> str:
    phone = s.business_phone or "[phone not confirmed]"
    return f"""You write on behalf of {s.business_name}, a roofing and property-services business in {s.service_area}.
Website: {s.business_website}. Phone: {phone}.
Services: roof restoration, repairs, painting, cleaning, sealing and maintenance; gutter cleaning, repairs,
replacement, gutter guards and downpipes; fascia installation, replacement and painting; interior and exterior
painting (walls, ceilings, fences); pergola and carport repairs.

Rules you must follow:
- Write in natural, friendly, professional Australian English. Short sentences. Sound like a real person on the team.
- NEVER invent prices, warranties, insurance, licences, availability dates, timeframes or any commitment.
  If a detail is needed and not given to you, say we'll confirm it after an inspection, or write [TO CONFIRM].
- Never promise guaranteed results or search rankings. No fake reviews, testimonials or before/after claims.
- Escalate (do not resolve) complaints, safety issues, payment disputes and legal matters.
- Output only the requested text, with no preamble and no markdown headings unless asked."""


@dataclass
class GenResult:
    text: str
    provider: str  # openai | gemini | template
    error: str = ""


class TextGenerator:
    def __init__(self, settings: Settings, http: HttpClient) -> None:
        self.s = settings
        self.http = http
        self.disabled_reason: str = ""  # set after a non-retryable failure (auth/quota) this run

    @property
    def provider(self) -> str:
        p = self.s.text_provider
        if p == "none":
            return "none"
        if p == "openai":
            return "openai" if self.s.openai_configured else "none"
        if p == "gemini":
            return "gemini" if self.s.gemini_configured else "none"
        if self.s.openai_configured:
            return "openai"
        if self.s.gemini_configured:
            return "gemini"
        return "none"

    def health(self) -> tuple[str, str]:
        """Return (status, detail). status in ok | not_configured | error."""
        p = self.provider
        if p == "none":
            return "not_configured", "No text provider configured — template fallback active."
        try:
            if p == "openai":
                self.http.request(
                    "GET", "https://api.openai.com/v1/models", bearer=self.s.openai_api_key, retry=False
                )
            else:
                self.http.request(
                    "GET",
                    f"https://generativelanguage.googleapis.com/v1beta/models/{self.s.gemini_text_model}",
                    headers={"x-goog-api-key": self.s.gemini_api_key},
                    retry=False,
                )
            return "ok", f"{p} reachable"
        except HttpError as exc:
            return "error", f"{p}: {exc}"

    def generate(self, prompt: str, fallback: str, max_tokens: int = 600) -> GenResult:
        p = self.provider
        if p == "none":
            return GenResult(fallback, "template")
        if self.disabled_reason:
            return GenResult(fallback, "template", self.disabled_reason)
        system = business_system_prompt(self.s)
        try:
            if p == "openai":
                data = self.http.request(
                    "POST",
                    "https://api.openai.com/v1/chat/completions",
                    bearer=self.s.openai_api_key,
                    json_body={
                        "model": self.s.openai_text_model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": prompt},
                        ],
                        "max_completion_tokens": max_tokens,
                    },
                )
                text = data["choices"][0]["message"]["content"]
            else:
                data = self.http.request(
                    "POST",
                    f"https://generativelanguage.googleapis.com/v1beta/models/{self.s.gemini_text_model}:generateContent",
                    headers={"x-goog-api-key": self.s.gemini_api_key},
                    json_body={
                        "systemInstruction": {"parts": [{"text": system}]},
                        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.4},
                    },
                )
                parts = data["candidates"][0]["content"]["parts"]
                text = "".join(part.get("text", "") for part in parts)
            text = (text or "").strip()
            if not text:
                return GenResult(fallback, "template", f"{p} returned empty text")
            return GenResult(text, p)
        except HttpError as exc:
            if exc.kind in ("auth", "quota"):
                # Don't hammer a provider that is out of credit or rejecting our key.
                self.disabled_reason = f"{p} unavailable: {exc}"
            log.warning("text generation failed, using template: %s", exc)
            return GenResult(fallback, "template", str(exc))
        except (KeyError, IndexError, TypeError) as exc:
            return GenResult(fallback, "template", f"{p} unexpected response shape: {exc.__class__.__name__}")
