"""Image generation (OpenAI Images API) with backoff and quota-aware fallback.

Generated images are illustrative marketing graphics only — never presented as
photos of real completed work (no fabricated before/after evidence).
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from ..config import Settings
from ..http_client import HttpClient, HttpError

SAFE_SUFFIX = (
    " Illustrative marketing graphic in a clean modern style. Not a photograph of a real job. "
    "No text, no logos, no people's faces, no before/after comparison."
)


class ImageGenerator:
    def __init__(self, settings: Settings, http: HttpClient, media_dir: str = "data/media") -> None:
        self.s = settings
        self.http = http
        self.media_dir = Path(media_dir)
        self.disabled_reason = ""

    @property
    def configured(self) -> bool:
        return self.s.openai_configured and self.s.generate_images

    def health(self) -> tuple[str, str]:
        if not self.s.generate_images:
            return "not_configured", "GENERATE_IMAGES=false (text-only content)"
        if not self.s.openai_configured:
            return "not_configured", "OPENAI_API_KEY not set — text-only content"
        if self.disabled_reason:
            return "error", self.disabled_reason
        return "ok", f"model {self.s.openai_image_model} (checked on use)"

    def generate(self, prompt: str) -> tuple[str, str]:
        """Return (local_filename, error). On failure filename is '' and error explains why."""
        if not self.configured:
            return "", "image generation not configured"
        if self.disabled_reason:
            return "", self.disabled_reason
        try:
            data = self.http.request(
                "POST",
                "https://api.openai.com/v1/images/generations",
                bearer=self.s.openai_api_key,
                json_body={"model": self.s.openai_image_model, "prompt": prompt + SAFE_SUFFIX,
                           "size": "1024x1024", "n": 1},
            )
            item = (data or {}).get("data", [{}])[0]
            b64 = item.get("b64_json")
            if not b64:
                return "", "image API returned no image data"
            raw = base64.b64decode(b64)
        except HttpError as exc:
            if exc.kind in ("quota", "auth") or (exc.kind == "client" and exc.status == 404):
                # Out of credit, bad key or model unavailable: stop trying for this process.
                self.disabled_reason = f"image generation unavailable: {exc}"
            return "", str(exc)
        except (ValueError, IndexError, TypeError) as exc:
            return "", f"unexpected image response: {exc.__class__.__name__}"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(raw).hexdigest()[:24] + ".png"
        (self.media_dir / name).write_bytes(raw)
        return name, ""

    def public_url(self, filename: str) -> str:
        if not filename or not self.s.public_base_url:
            return ""
        return f"{self.s.public_base_url.rstrip('/')}/media/{filename}"
