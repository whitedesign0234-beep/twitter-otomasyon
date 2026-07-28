# main.py
"""Orkestrasyon ve CLI: profil yükle -> çek -> dedupe -> seç -> yaz -> yayınla -> kaydet."""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import sys
import tempfile
from pathlib import Path

import requests
from dotenv import load_dotenv

import sources  # noqa: F401  (yan etki: kaynak eklentilerini registry'ye kaydeder)
from config.schema import Profile, SourceConfig, discover_profiles, load_profile
from publishers.base import DryRunPublisher, Publisher, PostResult
from publishers.twitter_browser import TwitterBrowserPublisher
from publishers.x_api import XApiPublisher, read_credentials
from rewriter import Rewriter
from selector import posting_blocked, select
from sources.base import ContentItem
from sources.registry import create_source
from store import StateStore

# Sabitler
PROFILES_DIR = Path("profiles")
SESSION_DIR = Path(".sessions")            # base64'ten çözülen oturum dosyaları
IMAGE_TIMEOUT_SECONDS = 12
EXIT_OK = 0                                # kritik olmayan hata
EXIT_CONFIG_ERROR = 1                      # yapılandırma hatası

logger = logging.getLogger("bot")


class ProfileFilter(logging.Filter):
    """Her log kaydına 'profile' alanı ekler (yoksa '-')."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Kayıtta profile alanı yoksa varsayılan atar."""
        if not hasattr(record, "profile"):
            record.profile = "-"
        return True


def configure_logging() -> None:
    """Yapılandırılmış, profil etiketli loglamayı kurar. Sırlar loglanmaz."""
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(profile)s] %(name)s: %(message)s")
    )
    handler.addFilter(ProfileFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def _plog(profile_name: str) -> logging.LoggerAdapter:
    """Belirli bir profil için 'profile' alanını sabitleyen log adaptörü verir."""
    return logging.LoggerAdapter(logger, {"profile": profile_name})


def materialize_session(profile: Profile) -> Path | None:
    """Profilin base64 oturum sırrını dosyaya yazar; yoksa None döner."""
    encoded = os.environ.get(profile.session_secret_name, "").strip()
    if not encoded:
        return None
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    target = SESSION_DIR / f"{profile.name}.session.json"
    try:
        target.write_bytes(base64.b64decode(encoded))
    except (ValueError, OSError) as exc:
        _plog(profile.name).error("Oturum sırrı çözülemedi: %s", exc)
        return None
    return target


def fetch_all_sources(
    profile: Profile, store: StateStore, only_source: str | None
) -> list[ContentItem]:
    """Profilin kaynaklarını sırayla çeker; her kaynak hata izolasyonludur."""
    log = _plog(profile.name)
    items: list[ContentItem] = []
    for source_config in profile.sources:
        if only_source and source_config.name != only_source:
            continue
        try:
            source = create_source(source_config.type)
            items.extend(source.fetch(source_config, store))
        except KeyError as exc:
            log.error("Kaynak tipi hatası, atlanıyor: %s", exc)
        except Exception as exc:  # tek kaynak çökse de diğerleri devam etsin
            log.warning("Kaynak '%s' çöktü, atlanıyor: %s", source_config.name, exc)
    return items


def download_image(item: ContentItem, profile_name: str) -> str | None:
    """İçeriğin görselini geçici dosyaya indirir; başarısızsa None döner."""
    if not item.image_url:
        return None
    try:
        response = requests.get(item.image_url, timeout=IMAGE_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        _plog(profile_name).info("Görsel indirilemedi, metin-only: %s", exc)
        return None
    suffix = ".jpg"
    tmp = tempfile.NamedTemporaryFile(prefix=f"{profile_name}_", suffix=suffix, delete=False)
    tmp.write(response.content)
    tmp.close()
    return tmp.name


def build_publisher(profile: Profile, dry_run: bool) -> Publisher:
    """Profilin publisher_type'ına ve çalışma moduna göre yayıncı üretir."""
    log = _plog(profile.name)
    if dry_run:
        return DryRunPublisher()

    if profile.publisher_type == "x_api":
        creds = read_credentials(profile.name)
        if creds is None:
            log.warning("X API anahtarları eksik (.env) — DryRun'a düşülüyor")
            return DryRunPublisher()
        return XApiPublisher(profile.name, creds)

    # twitter_browser (eski yol — X tarafından engellenebilir)
    session_path = materialize_session(profile)
    if session_path is None:
        log.warning("Oturum sırrı yok — DryRun'a düşülüyor")
        return DryRunPublisher()
    return TwitterBrowserPublisher(profile.name, session_path, headless=True)


async def run_profile(profile: Profile, args: argparse.Namespace) -> None:
    """Tek bir profilin uçtan uca akışını yürütür (hataları içeride yakalar)."""
    log = _plog(profile.name)
    if not profile.enabled:
        log.info("Profil devre dışı, atlanıyor")
        return

    store = StateStore(profile.state.namespace, profile.state.dedupe_ttl_days)
    store.prune()
    # Dry-run bir TESTTİR: state'i (görülenler, ETag, son paylaşım) DEĞİŞTİRMEZ.
    # Aksi halde hiç paylaşılmamış haberler "görüldü" damgası yer ve yakılırdı.
    persist = not args.dry_run

    # Sık cron'da (ör. */5) boşuna RSS çekmemek için önce zamanlama penceresi.
    if not args.ignore_schedule:
        reason = posting_blocked(profile, store)
        if reason:
            log.info("Paylaşım penceresi kapalı (%s) — atlanıyor", reason)
            return

    candidates = fetch_all_sources(profile, store, args.source)
    chosen = select(profile, candidates, store, ignore_schedule=args.ignore_schedule)
    if args.limit is not None:
        chosen = chosen[: args.limit]
    if not chosen:
        log.info("Paylaşılacak içerik yok")
        if persist:
            store.save()
        return

    rewriter = Rewriter()
    publisher = build_publisher(profile, dry_run=args.dry_run)

    for item in chosen:
        post = rewriter.generate(item, profile.persona)
        log.info("Metin üretildi (fallback=%s)", post.used_fallback)
        # Görsel yalnızca profilde açıkça istenirse indirilir/eklenir (maliyet).
        image_path = download_image(item, profile.name) if profile.persona.include_image else None

        result: PostResult = await publisher.publish(post.text, image_path)
        # Görseli her durumda sil (kritik olmayan temizlik).
        if image_path:
            Path(image_path).unlink(missing_ok=True)

        if result.session_invalid:
            log.error("Oturum düştü — bu profil durduruluyor, yeniden giriş gerekli")
            break
        if result.success:
            store.mark_seen(item.url)
            store.set_last_post_now()
            log.info("Paylaşıldı: %s", item.url)
        else:
            log.warning("Paylaşım başarısız: %s", result.detail)
            # Başarısız içeriği görülen işaretleme — sonraki koşuda tekrar denensin.

    if persist:
        store.save()


def resolve_profiles(args: argparse.Namespace) -> list[Profile]:
    """CLI argümanlarına göre işlenecek profil listesini yükler ve doğrular."""
    if args.all:
        paths = discover_profiles(PROFILES_DIR)
    elif args.profile:
        paths = [PROFILES_DIR / f"{args.profile}.yaml"]
    else:
        raise SystemExit("--profile <ad> veya --all belirtilmeli")

    profiles: list[Profile] = []
    for path in paths:
        if not path.exists():
            raise ValueError(f"Profil dosyası bulunamadı: {path}")
        profiles.append(load_profile(path))
    return profiles


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Komut satırı argümanlarını ayrıştırır."""
    parser = argparse.ArgumentParser(description="Çok hesaplı X paylaşım motoru")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--profile", help="Tek bir profili çalıştır (ör. haber)")
    group.add_argument("--all", action="store_true", help="Tüm aktif profilleri çalıştır")
    parser.add_argument("--dry-run", action="store_true", help="Paylaşma, yalnızca logla")
    parser.add_argument("--limit", type=int, default=None, help="Bu koşuda en çok N paylaşım")
    parser.add_argument("--source", default=None, help="Yalnızca bu isimli kaynağı kullan")
    parser.add_argument(
        "--ignore-schedule",
        action="store_true",
        help="Sadece test: sessiz saat/gün/aralık kapılarını atla",
    )
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> int:
    """Asenkron ana akış: profilleri yükler ve tek tek çalıştırır."""
    # DRY_RUN ortam değişkeni de dry-run'ı tetikleyebilir.
    if os.environ.get("DRY_RUN", "").strip() == "1":
        args.dry_run = True

    try:
        profiles = resolve_profiles(args)
    except (ValueError, SystemExit) as exc:
        logger.error("Yapılandırma hatası: %s", exc, extra={"profile": "-"})
        return EXIT_CONFIG_ERROR

    for profile in profiles:
        try:
            await run_profile(profile, args)
        except Exception as exc:  # bir profil çökse diğerleri devam etsin
            _plog(profile.name).error("Profil beklenmedik hata ile çöktü: %s", exc)
    return EXIT_OK


def main() -> int:
    """Giriş noktası: loglamayı kurar ve asenkron akışı çalıştırır."""
    load_dotenv()
    configure_logging()
    args = parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
