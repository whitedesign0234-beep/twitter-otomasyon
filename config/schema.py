# config/schema.py
"""Profil YAML şemasını pydantic ile doğrular ve yükler."""

from __future__ import annotations

import re
from datetime import time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

# Sabitler: sihirli sayılar yerine adlandırılmış değerler.
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")  # "HH:MM" biçimi
DEFAULT_MAX_CHARS = 240
X_HARD_LIMIT = 280  # X'in mutlak karakter sınırı


def _parse_hhmm(value: str) -> time:
    """'HH:MM' biçimindeki metni datetime.time'a çevirir."""
    if not TIME_PATTERN.match(value):
        raise ValueError(f"Geçersiz saat biçimi (HH:MM bekleniyor): {value!r}")
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour=hour, minute=minute)


class QuietHours(BaseModel):
    """Paylaşım yapılmayacak sessiz saat aralığı (Europe/Istanbul).

    days: Sessiz saatin uygulanacağı ISO günleri (Pzt=1..Paz=7). Sessiz saat
    sabaha (gece yarısından sonraya) denk geldiğinden, gün = o sabahın takvim
    günüdür. Örn. [1,2,3,4,5] => Pzt-Cum sabahları sessiz; Cmt/Paz sabahları
    (yani Cuma/Cumartesi geceleri) açık. Boş/tam liste => her gün.
    """

    start: str = "01:00"
    end: str = "07:30"
    days: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5, 6, 7])

    @field_validator("start", "end")
    @classmethod
    def _validate_time(cls, value: str) -> str:
        """Saat alanlarının HH:MM biçiminde olduğunu doğrular."""
        _parse_hhmm(value)
        return value

    @field_validator("days")
    @classmethod
    def _validate_days(cls, value: list[int]) -> list[int]:
        """days değerlerinin 1-7 (Pzt-Paz) aralığında olduğunu doğrular."""
        if any(day < 1 or day > 7 for day in value):
            raise ValueError("quiet_hours.days yalnızca 1-7 (Pzt=1..Paz=7) içermeli")
        return value

    @property
    def start_time(self) -> time:
        """Başlangıç saatini time nesnesi olarak döndürür."""
        return _parse_hhmm(self.start)

    @property
    def end_time(self) -> time:
        """Bitiş saatini time nesnesi olarak döndürür."""
        return _parse_hhmm(self.end)


class Persona(BaseModel):
    """Metin üretimini yönlendiren kişilik ve stil ayarları."""

    system_prompt: str
    max_chars: int = Field(default=DEFAULT_MAX_CHARS, ge=1, le=X_HARD_LIMIT)
    hashtag_count: int = Field(default=2, ge=0, le=10)
    hashtag_whitelist: list[str] = Field(default_factory=list)
    banned_words: list[str] = Field(default_factory=list)
    # X, link içeren tweet'i pahalı ücretlendirir (~$0.20). Varsayılan: link YOK.
    include_link: bool = False
    # Görsel ücretsizdir ve etkileşimi artırır. Varsayılan: görsel VAR.
    include_image: bool = True


class SourceConfig(BaseModel):
    """Tek bir içerik kaynağının yapılandırması."""

    type: str  # registry'deki eklenti anahtarı (ör. "rss")
    name: str
    url: str
    weight: int = Field(default=1, ge=1)
    max_age_hours: int = Field(default=24, ge=1)


class Schedule(BaseModel):
    """Bir koşuda kaç paylaşım yapılacağını ve zamanlama kurallarını tutar."""

    max_posts_per_run: int = Field(default=1, ge=1)
    min_minutes_between_posts: int = Field(default=25, ge=0)
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    active_days: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5, 6, 7])

    @field_validator("active_days")
    @classmethod
    def _validate_days(cls, value: list[int]) -> list[int]:
        """active_days değerlerinin 1-7 (Pzt-Paz) aralığında olduğunu doğrular."""
        if any(day < 1 or day > 7 for day in value):
            raise ValueError("active_days yalnızca 1-7 (Pzt=1..Paz=7) içermeli")
        return value


class StateConfig(BaseModel):
    """Durum (state) deposunun profil bazlı ayarları."""

    namespace: str
    dedupe_ttl_days: int = Field(default=7, ge=1)


class Profile(BaseModel):
    """Tek bir X hesabını besleyen tam profil tanımı."""

    name: str
    enabled: bool = True
    account_handle: str
    # Hangi yayıncı kullanılacak: "x_api" (resmi API, önerilen) veya
    # "twitter_browser" (tarayıcı otomasyonu — X tarafından engellenebilir).
    publisher_type: Literal["x_api", "twitter_browser"] = "x_api"
    session_secret_name: str = ""  # yalnızca twitter_browser için gerekir
    # Başlığında bu kelimelerden biri geçen haberi ELE (tıklama tuzağı/belirsiz
    # başlık filtresi, ör. "deprem mi oldu"). Büyük/küçük harf duyarsız.
    exclude_title_keywords: list[str] = Field(default_factory=list)
    persona: Persona
    sources: list[SourceConfig]
    schedule: Schedule = Field(default_factory=Schedule)
    state: StateConfig

    @model_validator(mode="after")
    def _validate_consistency(self) -> "Profile":
        """Kaynak listesi boş olmamalı ve namespace/name tutarlı olmalı."""
        if not self.sources:
            raise ValueError(f"Profil '{self.name}' en az bir kaynak içermeli")
        return self


def load_profile(path: Path) -> Profile:
    """Verilen YAML dosyasını okuyup doğrulanmış bir Profile döndürür."""
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Profil dosyası geçerli bir eşleme değil: {path}")
    return Profile.model_validate(raw)


def discover_profiles(profiles_dir: Path) -> list[Path]:
    """profiles/ dizinindeki tüm .yaml dosyalarının yollarını sıralı döndürür."""
    return sorted(profiles_dir.glob("*.yaml"))
