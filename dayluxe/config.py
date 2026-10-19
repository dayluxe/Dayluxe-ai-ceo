"""Configuration loaded from environment variables (and an optional .env file).

Every safety switch defaults to the safe value from the master system prompt:
AUTO_SEND_EMAIL=false, AUTO_PUBLISH_SOCIAL=false, AUTO_SPEND_AD_BUDGET=false,
AUTO_SEND_QUOTES=false, CEO_APPROVAL_REQUIRED=true.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}


def load_dotenv(path: str | os.PathLike[str] = ".env") -> None:
    """Minimal .env loader. Existing environment variables always win."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    raise ValueError(f"{name} must be true/false, got {raw!r}")


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


SECRET_FIELDS = {
    "admin_token",
    "webhook_secret",
    "openai_api_key",
    "gemini_api_key",
    "google_client_secret",
    "google_refresh_token",
    "wp_app_password",
    "meta_page_access_token",
}


@dataclass
class Settings:
    # Business (verified facts only — supplied by the owner)
    business_name: str = "Dayluxe Roofing & Property Services"
    business_website: str = "https://dayluxeroofing.com/"
    business_phone: str = ""
    service_area: str = "Sydney and Western Sydney, NSW, Australia"
    email_signature: str = "Dayluxe Roofing & Property Services"
    timezone: str = "Australia/Sydney"

    # Security
    admin_token: str = ""
    webhook_secret: str = ""

    # Storage
    database_path: str = "data/dayluxe.db"

    # Approval policy
    auto_send_email: bool = False
    auto_publish_social: bool = False
    auto_spend_ad_budget: bool = False
    auto_send_quotes: bool = False
    ceo_approval_required: bool = True

    # AI providers
    text_provider: str = "auto"  # auto | openai | gemini | none
    openai_api_key: str = ""
    openai_text_model: str = "gpt-6-luna"
    openai_image_model: str = "gpt-image-2.5-flare"
    gemini_api_key: str = ""
    gemini_text_model: str = "gemini-3.5-flash-lite"

    # Gmail (OAuth 2.0 refresh-token flow)
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    gmail_user: str = "me"
    gmail_query: str = "is:unread in:inbox newer_than:7d"
    gmail_max_messages: int = 20

    # WordPress REST API (Application Password)
    wp_base_url: str = ""
    wp_username: str = ""
    wp_app_password: str = ""

    # Meta Graph API
    meta_page_id: str = ""
    meta_page_access_token: str = ""
    meta_ig_user_id: str = ""
    meta_graph_version: str = "v25.0"

    # Content
    generate_images: bool = False
    public_base_url: str = ""  # e.g. https://dayluxe-ai-ceo.onrender.com (needed to post images)
    seo_target_suburbs: str = "Parramatta,Penrith,Blacktown,Liverpool,Campbelltown,Castle Hill,Rouse Hill,Fairfield"
    posts_per_week: int = 3
    followup_after_days: int = 3

    # Owner-approved pricing (JSON file). Quotes never invent prices.
    pricing_file: str = "config/pricing.json"

    # Runtime
    host: str = "0.0.0.0"
    port: int = 8000
    run_interval_minutes: int = 0  # 0 = scheduler off; trigger runs manually
    http_timeout_seconds: int = 20
    http_max_retries: int = 3
    log_level: str = "INFO"

    extra: dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------------- helpers
    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def gmail_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.google_refresh_token)

    @property
    def wordpress_configured(self) -> bool:
        return bool(self.wp_base_url and self.wp_username and self.wp_app_password)

    @property
    def meta_configured(self) -> bool:
        return bool(self.meta_page_id and self.meta_page_access_token)

    def validate(self) -> list[str]:
        """Return a list of configuration problems (empty list = OK)."""
        problems: list[str] = []
        if not self.admin_token or len(self.admin_token) < 16:
            problems.append("ADMIN_TOKEN is missing or shorter than 16 characters (dashboard is locked).")
        if self.text_provider not in {"auto", "openai", "gemini", "none"}:
            problems.append("TEXT_PROVIDER must be one of auto, openai, gemini, none.")
        if self.text_provider == "openai" and not self.openai_configured:
            problems.append("TEXT_PROVIDER=openai but OPENAI_API_KEY is empty.")
        if self.text_provider == "gemini" and not self.gemini_configured:
            problems.append("TEXT_PROVIDER=gemini but GEMINI_API_KEY is empty.")
        if self.wp_base_url and not self.wp_base_url.startswith("https://"):
            problems.append("WP_BASE_URL must start with https://")
        if not self.ceo_approval_required and any(
            (self.auto_send_email, self.auto_publish_social, self.auto_spend_ad_budget, self.auto_send_quotes)
        ):
            problems.append(
                "Warning: CEO_APPROVAL_REQUIRED=false with AUTO_* enabled — consequential actions run without review."
            )
        return problems

    def redacted(self) -> dict[str, Any]:
        """Settings safe to show in logs/dashboard (no secrets)."""
        out: dict[str, Any] = {}
        for f in fields(self):
            if f.name == "extra":
                continue
            val = getattr(self, f.name)
            if f.name in SECRET_FIELDS:
                out[f.name] = "set" if val else "not set"
            else:
                out[f.name] = val
        return out

    @property
    def target_suburbs(self) -> list[str]:
        return [x.strip() for x in self.seo_target_suburbs.split(",") if x.strip()]

    def load_pricing(self) -> dict[str, Any]:
        p = Path(self.pricing_file)
        if not p.is_file():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}


def load_settings(env_file: str | None = ".env") -> Settings:
    if env_file:
        load_dotenv(env_file)
    s = Settings(
        business_name=_str("BUSINESS_NAME", Settings.business_name),
        business_website=_str("BUSINESS_WEBSITE", Settings.business_website),
        business_phone=_str("BUSINESS_PHONE"),
        service_area=_str("SERVICE_AREA", Settings.service_area),
        email_signature=_str("EMAIL_SIGNATURE", Settings.email_signature).replace("\\n", "\n"),
        timezone=_str("TIMEZONE", Settings.timezone),
        admin_token=_str("ADMIN_TOKEN"),
        webhook_secret=_str("WEBHOOK_SECRET"),
        database_path=_str("DATABASE_PATH", Settings.database_path),
        auto_send_email=_bool("AUTO_SEND_EMAIL", False),
        auto_publish_social=_bool("AUTO_PUBLISH_SOCIAL", False),
        auto_spend_ad_budget=_bool("AUTO_SPEND_AD_BUDGET", False),
        auto_send_quotes=_bool("AUTO_SEND_QUOTES", False),
        ceo_approval_required=_bool("CEO_APPROVAL_REQUIRED", True),
        text_provider=_str("TEXT_PROVIDER", "auto").lower(),
        openai_api_key=_str("OPENAI_API_KEY"),
        openai_text_model=_str("OPENAI_TEXT_MODEL", Settings.openai_text_model),
        openai_image_model=_str("OPENAI_IMAGE_MODEL", Settings.openai_image_model),
        gemini_api_key=_str("GEMINI_API_KEY"),
        gemini_text_model=_str("GEMINI_TEXT_MODEL", Settings.gemini_text_model),
        google_client_id=_str("GOOGLE_CLIENT_ID"),
        google_client_secret=_str("GOOGLE_CLIENT_SECRET"),
        google_refresh_token=_str("GOOGLE_REFRESH_TOKEN"),
        gmail_user=_str("GMAIL_USER", "me"),
        gmail_query=_str("GMAIL_QUERY", Settings.gmail_query),
        gmail_max_messages=_int("GMAIL_MAX_MESSAGES", 20, minimum=1),
        wp_base_url=_str("WP_BASE_URL").rstrip("/"),
        wp_username=_str("WP_USERNAME"),
        wp_app_password=_str("WP_APP_PASSWORD"),
        meta_page_id=_str("META_PAGE_ID"),
        meta_page_access_token=_str("META_PAGE_ACCESS_TOKEN"),
        meta_ig_user_id=_str("META_IG_USER_ID"),
        meta_graph_version=_str("META_GRAPH_VERSION", Settings.meta_graph_version),
        generate_images=_bool("GENERATE_IMAGES", False),
        public_base_url=_str("PUBLIC_BASE_URL").rstrip("/"),
        seo_target_suburbs=_str("SEO_TARGET_SUBURBS", Settings.seo_target_suburbs),
        posts_per_week=_int("POSTS_PER_WEEK", 3),
        followup_after_days=_int("FOLLOWUP_AFTER_DAYS", 3, minimum=1),
        pricing_file=_str("PRICING_FILE", Settings.pricing_file),
        host=_str("HOST", "0.0.0.0"),
        port=_int("PORT", 8000, minimum=1),
        run_interval_minutes=_int("RUN_INTERVAL_MINUTES", 0),
        http_timeout_seconds=_int("HTTP_TIMEOUT_SECONDS", 20, minimum=1),
        http_max_retries=_int("HTTP_MAX_RETRIES", 3),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
    )
    return s
