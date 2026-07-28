# store.py
"""Profil bazlı kalıcı durum (state) yönetimi.

Durum kalıcılığı sorunu — üç seçenek karşılaştırması:
  (a) actions/cache : basit, ama cache 7 günde silinebilir ve eş zamanlı
      matrix job'ları arasında yarış olur; kayıp = olası mükerrer paylaşım.
  (b) state/'i repoya geri commit'leme : en dayanıklı ve ücretsiz; geçmişi
      denetlenebilir. Tek zorluk eş zamanlı push çakışması — pull --rebase +
      retry ile çözülür (bkz. main.yml).
  (c) Gist'i uzak KV deposu : dayanıklı ama ekstra token/kompleksite getirir.

SEÇİM: (b). Dayanıklılık kritik (dedupe bütünlüğü), maliyet sıfır, çakışma
per-profil ayrı dosya + rebase-retry ile yönetilebilir. Bu yüzden state/
.gitignore'dan hariç tutulur ve her koşu sonunda commit edilir.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

logger = logging.getLogger(__name__)

# Sabitler
STATE_DIR = Path("state")
SECONDS_PER_DAY = 86_400
# UTM ve benzeri izleme parametreleri normalize sırasında atılır.
TRACKING_PARAM_PREFIXES = ("utm_",)
TRACKING_PARAM_EXACT = {"fbclid", "gclid", "ref", "ref_src", "igshid", "mc_cid", "mc_eid"}


def normalize_url(url: str) -> str:
    """URL'yi izleme parametrelerinden arındırıp kanonik biçime getirir."""
    parsed = urlparse(url.strip())
    kept_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if not key.lower().startswith(TRACKING_PARAM_PREFIXES)
        and key.lower() not in TRACKING_PARAM_EXACT
    ]
    normalized = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        query=urlencode(sorted(kept_query)),
        fragment="",
    )
    return urlunparse(normalized)


def url_hash(url: str) -> str:
    """Normalize edilmiş URL'nin SHA-256 özetini döndürür (dedupe anahtarı)."""
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


@dataclass
class ProfileState:
    """Tek bir profilin diskteki durum içeriğini temsil eder."""

    seen: dict[str, float] = field(default_factory=dict)  # url_hash -> ilk görülme ts
    last_post_ts: float = 0.0
    feed_meta: dict[str, dict[str, str]] = field(default_factory=dict)  # url -> {etag,...}


class StateStore:
    """Bir profilin durumunu JSON dosyasında saklar ve dedupe/TTL yönetir."""

    def __init__(self, namespace: str, ttl_days: int, base_dir: Path = STATE_DIR) -> None:
        """Namespace'e karşılık gelen state dosyasını hazırlar."""
        self.namespace = namespace
        self.ttl_seconds = ttl_days * SECONDS_PER_DAY
        self.path = base_dir / f"{namespace}.json"
        self.state = self._load()

    def _load(self) -> ProfileState:
        """State dosyasını okur; yoksa boş durum döndürür."""
        if not self.path.exists():
            return ProfileState()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return ProfileState(
                seen=dict(data.get("seen", {})),
                last_post_ts=float(data.get("last_post_ts", 0.0)),
                feed_meta=dict(data.get("feed_meta", {})),
            )
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            logger.warning("State okunamadı (%s), sıfırdan başlanıyor: %s", self.path, exc)
            return ProfileState()

    def has_seen(self, url: str) -> bool:
        """Bu URL daha önce işlenmiş mi?"""
        return url_hash(url) in self.state.seen

    def mark_seen(self, url: str) -> None:
        """URL'yi görülen olarak işaretler (mevcut zaman damgasıyla)."""
        self.state.seen.setdefault(url_hash(url), time.time())

    @property
    def last_post_ts(self) -> float:
        """Son başarılı paylaşımın zaman damgası."""
        return self.state.last_post_ts

    def set_last_post_now(self) -> None:
        """Son paylaşım zamanını şimdiye ayarlar."""
        self.state.last_post_ts = time.time()

    def get_feed_meta(self, feed_url: str) -> dict[str, str]:
        """Feed için saklı ETag/Last-Modified başlıklarını döndürür."""
        return self.state.feed_meta.get(feed_url, {})

    def set_feed_meta(self, feed_url: str, etag: str | None, modified: str | None) -> None:
        """Feed için koşullu istek başlıklarını saklar."""
        meta: dict[str, str] = {}
        if etag:
            meta["etag"] = etag
        if modified:
            meta["modified"] = modified
        if meta:
            self.state.feed_meta[feed_url] = meta

    def prune(self) -> None:
        """TTL süresini aşan 'seen' kayıtlarını temizler, dosya şişmesin."""
        cutoff = time.time() - self.ttl_seconds
        before = len(self.state.seen)
        self.state.seen = {
            key: ts for key, ts in self.state.seen.items() if ts >= cutoff
        }
        removed = before - len(self.state.seen)
        if removed:
            logger.info("State budandı: %d eski kayıt silindi", removed)

    def save(self) -> None:
        """Durumu diske atomik biçimde yazar (önce .tmp, sonra rename)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "seen": self.state.seen,
            "last_post_ts": self.state.last_post_ts,
            "feed_meta": self.state.feed_meta,
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
