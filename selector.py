# selector.py
"""Aday içerikleri skorlayıp paylaşılacak olanları seçen katman."""

from __future__ import annotations

import logging
import re
from datetime import datetime, time, timezone
from difflib import SequenceMatcher
from functools import lru_cache
from zoneinfo import ZoneInfo

from config.schema import Profile
from sources.base import ContentItem
from store import StateStore

logger = logging.getLogger(__name__)

# Sabitler
TIMEZONE = ZoneInfo("Europe/Istanbul")
FRESHNESS_HALF_LIFE_HOURS = 6.0      # tazelik skoru bu sürede yarıya iner
WEIGHT_MULTIPLIER = 1.0              # kaynak ağırlığının skora katkı çarpanı
TITLE_SIMILARITY_THRESHOLD = 0.72     # bu oranın üstü "aynı haber" (aynı koşuda)
CROSS_RUN_SIMILARITY_THRESHOLD = 0.72  # geçmiş paylaşımlarla metin benzerliği eşiği
TOPIC_TOKEN_OVERLAP_THRESHOLD = 0.5   # anlamlı kelime örtüşmesi (Jaccard) eşiği
MIN_SIGNIFICANT_TOKEN_LEN = 4         # kısa kelimeleri (ve, bir, ile...) yok say


def _title_similarity(a: str, b: str) -> float:
    """İki başlık arasındaki benzerlik oranını (0-1) döndürür."""
    return SequenceMatcher(None, a.casefold(), b.casefold()).ratio()


def _significant_tokens(title: str) -> set[str]:
    """Başlıktaki anlamlı (uzun) kelimeleri küçük harfli küme olarak döndürür."""
    return {w for w in re.findall(r"\w+", title.casefold()) if len(w) >= MIN_SIGNIFICANT_TOKEN_LEN}


def _is_duplicate_topic(title: str, recent_titles: list[str]) -> bool:
    """Başlık, yakında paylaşılan başlıklardan birine yeterince benziyor mu?

    İki ölçüt: (1) metin benzerliği (aynı/çok yakın başlık), (2) anlamlı kelime
    örtüşmesi (aynı konunun farklı ifadesi). Herhangi biri eşiği aşarsa mükerrer.
    """
    tokens = _significant_tokens(title)
    for prev in recent_titles:
        if _title_similarity(title, prev) >= CROSS_RUN_SIMILARITY_THRESHOLD:
            return True
        prev_tokens = _significant_tokens(prev)
        shared = tokens & prev_tokens
        union = tokens | prev_tokens
        # Yanlış eleme riskine karşı yüksek bar: en az 4 ORTAK anlamlı kelime VE
        # güçlü örtüşme (aynı olayın yakın ifadesi). Entity-adı çakışmalarını
        # (ör. iki farklı "Erdoğan/Türkiye" haberi) yanlışlıkla elemez.
        if len(shared) >= 4 and union and len(shared) / len(union) >= TOPIC_TOKEN_OVERLAP_THRESHOLD:
            return True
    return False


@lru_cache(maxsize=8)
def _compile_exclude(keywords: tuple[str, ...]) -> re.Pattern | None:
    """Yasaklı kelimeleri kelime-sınırlı (whole-word) tek regex'e derler."""
    if not keywords:
        return None
    # \b ile kelime sınırı: "mü oldu" sadece ayrı kelime olarak eşleşir,
    # "ölümü oldu" gibi kelime içinde YANLIŞ eşleşme yapmaz.
    return re.compile("|".join(r"\b" + re.escape(k.casefold()) + r"\b" for k in keywords))


def _is_excluded_title(title: str, keywords: list[str]) -> bool:
    """Başlıkta yasaklı (tıklama tuzağı) kelimelerden biri tam kelime olarak geçiyor mu?"""
    pattern = _compile_exclude(tuple(keywords))
    return bool(pattern and pattern.search(title.casefold()))


def _is_quiet_now(profile: Profile, now_local: datetime) -> bool:
    """Şu anki yerel saatin sessiz saat aralığında olup olmadığını söyler."""
    quiet = profile.schedule.quiet_hours
    # Sessiz saat yalnızca belirtilen günlerde (o sabahın takvim günü) uygulanır.
    if now_local.isoweekday() not in quiet.days:
        return False
    start, end, current = quiet.start_time, quiet.end_time, now_local.time()
    if start <= end:
        return start <= current < end
    # Aralık gece yarısını aşıyorsa (ör. 23:00 -> 07:00)
    return current >= start or current < end


def _score(item: ContentItem, now_utc: datetime) -> float:
    """Tazelik ve kaynak ağırlığını birleştiren basit skoru hesaplar."""
    age_hours = max((now_utc - item.published_at).total_seconds() / 3600.0, 0.0)
    freshness = 0.5 ** (age_hours / FRESHNESS_HALF_LIFE_HOURS)  # üssel azalma
    return freshness + WEIGHT_MULTIPLIER * item.weight


def posting_blocked(profile: Profile, store: StateStore) -> str | None:
    """Şu an paylaşım engelli mi? Engelliyse sebebini, değilse None döndürür.

    Gün / sessiz saat / minimum aralık kapılarını kontrol eder. Sık çalışan
    cron'da (ör. */5) boşuna kaynak çekmemek için fetch'ten ÖNCE kullanılır.
    """
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(TIMEZONE)
    if now_local.isoweekday() not in profile.schedule.active_days:
        return "aktif gün değil"
    if _is_quiet_now(profile, now_local):
        return "sessiz saat"
    min_gap = profile.schedule.min_minutes_between_posts * 60
    if now_utc.timestamp() - store.last_post_ts < min_gap:
        return "son paylaşımdan bu yana yeterli süre geçmedi"
    return None


def select(
    profile: Profile,
    candidates: list[ContentItem],
    store: StateStore,
    ignore_schedule: bool = False,
) -> list[ContentItem]:
    """Zamanlama kurallarını uygulayıp paylaşılacak içerikleri seçer.

    ignore_schedule=True yalnızca test içindir: gün/sessiz saat/minimum aralık
    kapılarını atlar; dedupe ve benzerlik kontrolleri korunur.
    """
    now_utc = datetime.now(timezone.utc)

    if not ignore_schedule:
        reason = posting_blocked(profile, store)
        if reason:
            logger.info("[%s] %s, seçim yok", profile.name, reason)
            return []
    else:
        logger.info("[%s] TEST modu: zamanlama kapıları atlanıyor", profile.name)

    # 3) Adayları ele: (a) daha önce görülen URL, (b) tıklama-tuzağı başlık,
    #    (c) yakında paylaşılan başlığa benzer konu (kaynaklar arası mükerrer).
    keywords = profile.exclude_title_keywords
    recent_titles = store.recent_posted_titles()
    unseen = [
        c for c in candidates
        if not store.has_seen(c.url)
        and not _is_excluded_title(c.title, keywords)
        and not _is_duplicate_topic(c.title, recent_titles)
    ]
    ranked = sorted(unseen, key=lambda c: _score(c, now_utc), reverse=True)

    # 4) Aynı koşuda birbirine çok benzeyen içerikleri ele.
    chosen: list[ContentItem] = []
    limit = profile.schedule.max_posts_per_run
    for item in ranked:
        if len(chosen) >= limit:
            break
        if any(_title_similarity(item.title, c.title) >= TITLE_SIMILARITY_THRESHOLD for c in chosen):
            continue
        chosen.append(item)

    logger.info("[%s] %d içerik seçildi (aday: %d)", profile.name, len(chosen), len(unseen))
    return chosen
