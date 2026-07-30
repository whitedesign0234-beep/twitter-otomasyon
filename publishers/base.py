# publishers/base.py
"""Yayıncılar için ortak protokol, sonuç tipi ve basit uygulamalar."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class PostResult:
    """Bir paylaşım denemesinin sonucu."""

    success: bool
    detail: str = ""              # hata/başarı açıklaması
    session_invalid: bool = False  # oturum düştüyse True (yeniden giriş gerekir)


@runtime_checkable
class Publisher(Protocol):
    """Tüm yayıncıların uygulaması gereken asenkron arayüz."""

    async def publish(self, text: str, image_path: str | None) -> PostResult:
        """Metni (ve varsa görseli) yayınlar; PostResult döndürür."""
        ...

    async def publish_video(self, text: str, video_path: str) -> PostResult:
        """Metni video ekiyle yayınlar; PostResult döndürür."""
        ...


class DryRunPublisher:
    """Hiçbir şey paylaşmaz; yalnızca ne yapılacağını loglar (güvenli test)."""

    async def publish(self, text: str, image_path: str | None) -> PostResult:
        """Paylaşımı simüle eder ve içeriği loga yazar."""
        logger.info("[DRY-RUN] Paylaşılacaktı (görsel=%s):\n%s", image_path, text)
        return PostResult(success=True, detail="dry-run")

    async def publish_video(self, text: str, video_path: str) -> PostResult:
        """Video paylaşımını simüle eder ve içeriği loga yazar."""
        logger.info("[DRY-RUN] VİDEO paylaşılacaktı (%s):\n%s", video_path, text)
        return PostResult(success=True, detail="dry-run-video")


class BlueskyPublisher:
    """Bluesky (AT Protocol) için iskelet — API gerçekten ücretsizdir.

    Aynı Publisher protokolünü uygular. Etkinleştirmek için atproto istemcisi
    ile app-password tabanlı oturum açıp createRecord çağrısı eklenir.
    """

    async def publish(self, text: str, image_path: str | None) -> PostResult:
        """Henüz etkin değil; bilinçli olarak devre dışı sonuç döner."""
        logger.info("[Bluesky] iskelet uygulama — etkin değil")
        return PostResult(success=False, detail="bluesky-not-implemented")


class MastodonPublisher:
    """Mastodon için iskelet — REST API ücretsizdir.

    Aynı protokolü uygular. Etkinleştirmek için erişim token'ı ile
    POST /api/v1/statuses (ve medya için /api/v2/media) çağrıları eklenir.
    """

    async def publish(self, text: str, image_path: str | None) -> PostResult:
        """Henüz etkin değil; bilinçli olarak devre dışı sonuç döner."""
        logger.info("[Mastodon] iskelet uygulama — etkin değil")
        return PostResult(success=False, detail="mastodon-not-implemented")
