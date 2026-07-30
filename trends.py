# trends.py
"""Türkiye'nin güncel X (Twitter) gündem başlıklarını çeker (trends24.in).

X'in resmi trend API'si ücretli/kapalı olduğundan üçüncü taraf bir siteden
kazınır. Kırılgan olabilir (site değişirse boş döner) — bu durumda gündem
eklenmez, akış bozulmaz. Süreç içinde tek sefer çekilip önbelleğe alınır.
"""

from __future__ import annotations

import logging
import time

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

TRENDS_URL = "https://trends24.in/turkey/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
REQUEST_TIMEOUT_SECONDS = 15
MAX_TRENDS = 20
CACHE_TTL_SECONDS = 1800  # aynı koşuda/30 dk içinde tekrar çekme

# Modül düzeyi basit önbellek: (zaman_damgası, trend_listesi)
_cache: tuple[float, list[str]] | None = None


def _scrape_trends() -> list[str]:
    """trends24.in'den en güncel trend listesini kazır."""
    try:
        response = requests.get(
            TRENDS_URL, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Gündem çekilemedi: %s", exc)
        return []

    soup = BeautifulSoup(response.content, "html.parser")
    # En güncel trendler ilk trend-card listesindedir.
    container = soup.find("ol", class_="trend-card__list")
    if container is None:
        logger.warning("Gündem yapısı bulunamadı (site değişmiş olabilir)")
        return []
    trends: list[str] = []
    for link in container.find_all("a"):
        text = link.get_text(strip=True)
        if text and text not in trends:
            trends.append(text)
    return trends[:MAX_TRENDS]


def get_trends() -> list[str]:
    """Güncel Türkiye gündemini döndürür (önbellekli). Hata durumunda boş liste."""
    global _cache
    now = time.time()
    if _cache is not None and now - _cache[0] < CACHE_TTL_SECONDS:
        return _cache[1]
    trends = _scrape_trends()
    _cache = (now, trends)
    if trends:
        logger.info("Güncel gündem çekildi: %d başlık", len(trends))
    return trends
