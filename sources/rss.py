# sources/rss.py
"""RSS/Atom kaynağı eklentisi: feedparser ile haber çeker, görsel çıkarır."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from time import mktime

import feedparser
import requests
from bs4 import BeautifulSoup

from config.schema import SourceConfig
from sources.base import ContentItem
from sources.registry import register
from store import StateStore

logger = logging.getLogger(__name__)

# Sabitler
FEED_TIMEOUT_SECONDS = 15          # feed indirme zaman aşımı
OG_IMAGE_TIMEOUT_SECONDS = 8       # makale sayfasından og:image çekme zaman aşımı
USER_AGENT = "Mozilla/5.0 (compatible; NewsAutomationBot/1.0)"
MAX_SUMMARY_CHARS = 400            # ham özeti bu uzunlukta kırp


@register("rss")
class RSSSource:
    """Bir RSS/Atom feed'inden ContentItem listesi üretir."""

    def fetch(self, config: SourceConfig, store: StateStore) -> list[ContentItem]:
        """Feed'i koşullu istekle indirir, taze ve yeni öğeleri döndürür."""
        raw = self._download(config, store)
        if raw is None:
            return []  # 304 Not Modified veya ağ hatası: sessizce boş dön

        items: list[ContentItem] = []
        now = datetime.now(timezone.utc)
        max_age_seconds = config.max_age_hours * 3600

        for entry in raw.entries:
            try:
                item = self._to_item(entry, config, now, max_age_seconds)
            except (AttributeError, ValueError, KeyError) as exc:
                logger.debug("[%s] öğe atlandı: %s", config.name, exc)
                continue
            if item is not None:
                items.append(item)

        logger.info("[%s] %d taze öğe bulundu", config.name, len(items))
        return items

    def _download(
        self, config: SourceConfig, store: StateStore
    ) -> feedparser.FeedParserDict | None:
        """Feed'i indirir; ETag/Last-Modified ile koşullu istek yapar."""
        meta = store.get_feed_meta(config.url)
        headers = {"User-Agent": USER_AGENT}
        if meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if meta.get("modified"):
            headers["If-Modified-Since"] = meta["modified"]

        try:
            response = requests.get(
                config.url, headers=headers, timeout=FEED_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            logger.warning("[%s] feed indirilemedi: %s", config.name, exc)
            return None

        if response.status_code == 304:
            logger.debug("[%s] değişmemiş (304)", config.name)
            return None
        if response.status_code != 200:
            logger.warning("[%s] beklenmedik durum: %s", config.name, response.status_code)
            return None

        # Bir sonraki koşu için koşullu istek başlıklarını sakla.
        store.set_feed_meta(
            config.url,
            response.headers.get("ETag"),
            response.headers.get("Last-Modified"),
        )
        return feedparser.parse(response.content)

    def _to_item(
        self,
        entry: feedparser.FeedParserDict,
        config: SourceConfig,
        now: datetime,
        max_age_seconds: int,
    ) -> ContentItem | None:
        """Tek bir feed girdisini ContentItem'a çevirir; eski/eksikse None döner."""
        link = getattr(entry, "link", "").strip()
        title = getattr(entry, "title", "").strip()
        if not link or not title:
            return None

        published = self._parse_date(entry, now)
        if (now - published).total_seconds() > max_age_seconds:
            return None  # max_age_hours'tan eski: atla

        summary = self._clean_html(getattr(entry, "summary", ""))[:MAX_SUMMARY_CHARS]
        image_url = self._extract_image(entry, link)

        return ContentItem(
            uid=link,
            title=title,
            summary=summary,
            url=link,
            image_url=image_url,
            published_at=published,
            source_name=config.name,
            weight=config.weight,
        )

    @staticmethod
    def _parse_date(entry: feedparser.FeedParserDict, fallback: datetime) -> datetime:
        """Girdinin yayın tarihini UTC olarak çözer; yoksa 'şimdi' kullanır."""
        for key in ("published_parsed", "updated_parsed"):
            parsed = getattr(entry, key, None)
            if parsed:
                return datetime.fromtimestamp(mktime(parsed), tz=timezone.utc)
        return fallback

    @staticmethod
    def _clean_html(html: str) -> str:
        """HTML özetten düz metin çıkarır."""
        if not html:
            return ""
        return BeautifulSoup(html, "html.parser").get_text(separator=" ").strip()

    def _extract_image(self, entry: feedparser.FeedParserDict, article_url: str) -> str | None:
        """Görseli sırayla dener: media:content -> enclosure -> og:image."""
        media = getattr(entry, "media_content", None)
        if media and isinstance(media, list) and media[0].get("url"):
            return media[0]["url"]

        for link in getattr(entry, "links", []):
            if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image"):
                return link.get("href")

        return self._fetch_og_image(article_url)

    @staticmethod
    def _fetch_og_image(article_url: str) -> str | None:
        """Makale sayfasından og:image meta etiketini çeker (best-effort)."""
        try:
            response = requests.get(
                article_url,
                headers={"User-Agent": USER_AGENT},
                timeout=OG_IMAGE_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except requests.RequestException:
            return None  # görsel yoksa metin-only paylaşılacak

        soup = BeautifulSoup(response.content, "html.parser")
        tag = soup.find("meta", property="og:image")
        if tag and tag.get("content"):
            return tag["content"].strip()
        return None
