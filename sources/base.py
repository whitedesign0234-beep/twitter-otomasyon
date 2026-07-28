# sources/base.py
"""İçerik kaynakları için ortak veri sınıfı ve protokol."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from config.schema import SourceConfig
from store import StateStore


@dataclass(frozen=True)
class ContentItem:
    """Bir kaynaktan çekilen tek bir içerik parçası (haber, gönderi vb.)."""

    uid: str                     # kaynak-içi benzersiz kimlik (genelde link)
    title: str
    summary: str
    url: str                     # paylaşılacak orijinal bağlantı
    image_url: str | None
    published_at: datetime       # timezone-aware (UTC)
    source_name: str
    weight: int                  # kaynağın YAML'daki önceliği


@runtime_checkable
class ContentSource(Protocol):
    """Tüm kaynak eklentilerinin uygulaması gereken arayüz."""

    def fetch(self, config: SourceConfig, store: StateStore) -> list[ContentItem]:
        """Kaynaktan içerikleri çeker; hata durumunda boş liste döndürmelidir."""
        ...
