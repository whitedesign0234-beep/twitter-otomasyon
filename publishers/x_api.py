# publishers/x_api.py
"""X (Twitter) resmi API v2 ile paylaşım yapan yayıncı (tweepy tabanlı).

Tarayıcı, oturum çerezi veya giriş gerektirmez — 4 API anahtarıyla çalışır.
Free (ücretsiz) katman yazma/paylaşım için uygundur. Görsel yükleme v1.1
endpoint'i gerektirir ve free katmanda kapalı olabilir; başarısız olursa
metin-only paylaşılır (bot sessizce durmaz).
"""

from __future__ import annotations

import asyncio
import logging
import os

import tweepy

from publishers.base import PostResult

logger = logging.getLogger(__name__)

# Gerekli 4 anahtarın env taban adları. Profil bazlı için "_<PROFİL>" eki
# (ör. X_API_KEY_HABER) önce denenir, yoksa eksiz (X_API_KEY) kullanılır.
CRED_BASES = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")


def read_credentials(profile_name: str) -> dict[str, str] | None:
    """Profil için 4 API anahtarını env'den okur; biri bile eksikse None döner."""
    suffix = profile_name.upper()
    creds: dict[str, str] = {}
    for base in CRED_BASES:
        value = os.environ.get(f"{base}_{suffix}", "").strip() or os.environ.get(base, "").strip()
        if not value:
            return None
        creds[base] = value
    return creds


class XApiPublisher:
    """Verilen API anahtarlarıyla X'e resmi API üzerinden tweet gönderir."""

    def __init__(self, profile_name: str, credentials: dict[str, str]) -> None:
        """Yayıncıyı profil adı ve 4 API anahtarıyla hazırlar."""
        self.profile_name = profile_name
        self._creds = credentials

    async def publish(self, text: str, image_path: str | None) -> PostResult:
        """Tweet'i gönderir; tweepy senkron olduğu için ayrı thread'de çalıştırır."""
        return await asyncio.to_thread(self._publish_sync, text, image_path)

    async def publish_video(self, text: str, video_path: str) -> PostResult:
        """Videolu tweet'i gönderir (parçalı yükleme ayrı thread'de)."""
        return await asyncio.to_thread(self._publish_video_sync, text, video_path)

    def _publish_video_sync(self, text: str, video_path: str) -> PostResult:
        """Videoyu chunked upload ile yükler, işlenmesini bekler ve paylaşır."""
        try:
            auth = tweepy.OAuth1UserHandler(
                self._creds["X_API_KEY"],
                self._creds["X_API_SECRET"],
                self._creds["X_ACCESS_TOKEN"],
                self._creds["X_ACCESS_TOKEN_SECRET"],
            )
            api_v1 = tweepy.API(auth)
            # Video büyük olduğundan parçalı yükleme + async işleme beklenir.
            media = api_v1.media_upload(
                filename=video_path,
                chunked=True,
                media_category="tweet_video",
                wait_for_async_finalize=True,
            )
        except tweepy.TweepyException as exc:
            return PostResult(success=False, detail=f"Video yüklenemedi: {exc}")

        try:
            response = self._client().create_tweet(text=text, media_ids=[media.media_id])
        except tweepy.TooManyRequests:
            return PostResult(success=False, detail="Kota doldu (429)")
        except tweepy.Unauthorized:
            return PostResult(
                success=False, detail="API anahtarları geçersiz (401)", session_invalid=True
            )
        except tweepy.TweepyException as exc:
            return PostResult(success=False, detail=f"Videolu tweet hatası: {exc}")

        tweet_id = response.data.get("id") if response and response.data else None
        logger.info("[%s] videolu tweet gönderildi (id=%s)", self.profile_name, tweet_id)
        return PostResult(success=True, detail=f"tweet_id={tweet_id}")

    def _client(self) -> tweepy.Client:
        """Yapılandırılmış tweepy v2 istemcisini üretir."""
        return tweepy.Client(
            consumer_key=self._creds["X_API_KEY"],
            consumer_secret=self._creds["X_API_SECRET"],
            access_token=self._creds["X_ACCESS_TOKEN"],
            access_token_secret=self._creds["X_ACCESS_TOKEN_SECRET"],
        )

    def _publish_sync(self, text: str, image_path: str | None) -> PostResult:
        """Senkron paylaşım akışı: (varsa) görsel yükle, sonra tweet oluştur."""
        client = self._client()

        kwargs: dict = {"text": text}
        if image_path:
            media_ids = self._upload_media(image_path)
            if media_ids:
                kwargs["media_ids"] = media_ids

        try:
            response = client.create_tweet(**kwargs)
        except tweepy.TooManyRequests:
            return PostResult(success=False, detail="Aylık/anlık kota doldu (429)")
        except tweepy.Unauthorized:
            # Anahtarlar hatalı/expired — yeniden anahtar üretmek gerekir.
            return PostResult(
                success=False, detail="API anahtarları geçersiz (401)", session_invalid=True
            )
        except tweepy.Forbidden as exc:
            return PostResult(success=False, detail=f"İzin yok/duplicate (403): {exc}")
        except tweepy.TweepyException as exc:
            return PostResult(success=False, detail=f"API hatası: {exc}")

        tweet_id = response.data.get("id") if response and response.data else None
        logger.info("[%s] tweet gönderildi (id=%s)", self.profile_name, tweet_id)
        return PostResult(success=True, detail=f"tweet_id={tweet_id}")

    def _upload_media(self, image_path: str) -> list[str] | None:
        """Görseli v1.1 media/upload ile yükler; başarısızsa None (metin-only)."""
        try:
            auth = tweepy.OAuth1UserHandler(
                self._creds["X_API_KEY"],
                self._creds["X_API_SECRET"],
                self._creds["X_ACCESS_TOKEN"],
                self._creds["X_ACCESS_TOKEN_SECRET"],
            )
            api_v1 = tweepy.API(auth)
            media = api_v1.media_upload(filename=image_path)
            return [media.media_id]
        except tweepy.TweepyException as exc:
            # Free katmanda medya yükleme kapalı olabilir; metin-only devam.
            logger.warning(
                "[%s] görsel yüklenemedi, metin-only paylaşılıyor: %s",
                self.profile_name, exc,
            )
            return None
