"""Shared runtime context passed to every agent."""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .http_client import HttpClient
from .integrations.gmail import Gmail
from .integrations.images import ImageGenerator
from .integrations.meta import Meta
from .integrations.wordpress import WordPress
from .llm import TextGenerator
from .storage import Storage


@dataclass
class Context:
    settings: Settings
    storage: Storage
    http: HttpClient
    llm: TextGenerator
    gmail: Gmail
    wordpress: WordPress
    meta: Meta
    images: ImageGenerator

    @classmethod
    def build(cls, settings: Settings, storage: Storage | None = None, http: HttpClient | None = None,
              media_dir: str = "data/media") -> "Context":
        http = http or HttpClient(timeout=settings.http_timeout_seconds, max_retries=settings.http_max_retries)
        storage = storage or Storage(settings.database_path)
        return cls(
            settings=settings,
            storage=storage,
            http=http,
            llm=TextGenerator(settings, http),
            gmail=Gmail(settings, http),
            wordpress=WordPress(settings, http),
            meta=Meta(settings, http),
            images=ImageGenerator(settings, http, media_dir=media_dir),
        )
