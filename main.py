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

import media
import sources  # noqa: F401  (yan etki: kaynak eklentilerini registry'ye kaydeder)
import telegram_inbox
from config.schema import Profile, SourceConfig, discover_profiles, load_profile
from publishers.base import DryRunPublisher, Publisher, PostResult
from publishers.instagram import InstagramPublisher
from publishers.instagram import read_config as instagram_read_config
from publishers.twitter_browser import TwitterBrowserPublisher
from publishers.x_api import XApiPublisher, read_credentials
from rewriter import Rewriter, caption_from_text, caption_from_video
from selector import posting_blocked, select
from sources.base import ContentItem
from sources.registry import create_source
from store import StateStore

# Sabitler
PROFILES_DIR = Path("profiles")
SESSION_DIR = Path(".sessions")            # base64'ten çözülen oturum dosyaları
IMAGE_TIMEOUT_SECONDS = 12
X_TEXT_LIMIT = 280                         # X'in mutlak metin sınırı
MIN_DESC_WORDS = 4                         # kaynak açıklaması bundan kısaysa kullanma
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


def build_instagram_publisher(profile: Profile, dry_run: bool) -> InstagramPublisher | None:
    """Instagram çapraz-paylaşım yayıncısını üretir; yapılandırılmamışsa None."""
    if dry_run:
        return None  # dry-run'da gerçek IG paylaşımı yapılmaz
    config = instagram_read_config()
    if config is None:
        return None  # IG anahtarları yoksa çapraz paylaşım pasif
    _plog(profile.name).info("Instagram çapraz paylaşım aktif")
    return InstagramPublisher(config)


def _telegram_caption(
    item: telegram_inbox.InboxItem, profile: Profile, video_path: str
) -> str:
    """Telegram videosu için paylaşım metnini üretir (öncelikli, uydurmasız).

    Sıra: (1) kullanıcının Telegram'da yazdığı açıklama; (2) link ise kaynak
    açıklamasından (tweet metni) haber üslubu üret — güvenilir metin yöntemi;
    (3) videoyu Gemini'ye izlet (yedek); (4) hiçbiri olmazsa metinsiz.
    """
    log = _plog(profile.name)
    if item.caption:
        return item.caption[:X_TEXT_LIMIT]

    # (2) Kaynak açıklaması (X/TikTok/Instagram post metni) — en güvenilir yol.
    if item.kind == "link":
        source_desc = media.video_description(item.url or "")
        if len(source_desc.split()) >= MIN_DESC_WORDS:
            caption = caption_from_text(source_desc, profile.persona)
            if caption:
                log.info("Kaynak açıklamasından metin üretildi")
                return caption

    # (3) Videoyu Gemini'ye izlet (açıklama yoksa / dosya ise).
    log.info("Kaynak açıklaması yok — video Gemini ile analiz ediliyor")
    caption = caption_from_video(video_path, profile.persona)
    if caption:
        log.info("Video analizinden açıklama üretildi")
        return caption

    log.warning("Açıklama üretilemedi — metinsiz paylaşılıyor")
    return ""


async def _cross_post_instagram(
    profile: Profile,
    store: StateStore,
    ig_publisher: InstagramPublisher,
    caption: str,
    ready: Path,
) -> None:
    """Videoyu Instagram'a paylaşır: sıralı alternasyon — bir Story, bir Reels.

    Son başarılı IG paylaşımı Story ise bu sefer Reels, Reels ise Story olur
    (ilk sefer profildeki `start_with`). Tip yalnızca paylaşım BAŞARILI olursa
    ilerler; başarısız olursa sonraki koşuda aynı tip tekrar denenir."""
    log = _plog(profile.name)
    start_with = "REELS" if profile.instagram.start_with == "reels" else "STORIES"
    media_type = store.next_ig_type(start_with)

    if media_type == "REELS":
        result = await ig_publisher.publish_video(caption, str(ready))
    else:
        story = media.prepare_story(ready)
        if story is None:
            log.warning("Story videosu hazırlanamadı — IG atlandı (sıra bozulmaz)")
            return
        result = await ig_publisher.publish_story("", str(story))

    label = "Reels" if media_type == "REELS" else "Story"
    if result.success:
        store.record_ig_post(media_type)
        log.info("Instagram %s OK (sıradaki: %s)",
                 label, "Story" if media_type == "REELS" else "Reels")
    else:
        log.warning("Instagram %s başarısız: %s", label, result.detail)


async def _try_telegram_queue(
    profile: Profile,
    store: StateStore,
    publisher: Publisher,
    ig_publisher: InstagramPublisher | None,
) -> bool:
    """Telegram kuyruğundaki ilk öğeyi X'e (ve varsa IG'ye) paylaşır."""
    log = _plog(profile.name)
    items = telegram_inbox.pending_items(store.telegram_offset)
    if not items:
        return False

    item = items[0]  # en eski öğe (FIFO kuyruk)
    with tempfile.TemporaryDirectory(prefix=f"tg_{profile.name}_") as workdir:
        target = Path(workdir)
        if item.kind == "video":
            raw = telegram_inbox.download_file(item.file_id or "", workdir)
            video = Path(raw) if raw else None
        else:
            log.info("Telegram linki indiriliyor")
            video = media.download_video(item.url or "", target)

        if video is None:
            log.warning("Telegram öğesi indirilemedi — tüketilip atlanıyor")
            store.consume_telegram_update(item.update_id)
            return False

        ready = media.prepare_for_x(video)
        if ready is None:
            log.warning("Video X'e uygun hale getirilemedi — tüketilip atlanıyor")
            store.consume_telegram_update(item.update_id)
            return False

        caption = _telegram_caption(item, profile, str(ready))
        # Birincil: X. Video dosyası temp dizini silinmeden ÖNCE her ikisi de yapılır.
        result = await publisher.publish_video(caption, str(ready))
        # Çapraz paylaşım: Instagram. Günde N feed (Reels), gün içine yayılmış;
        # kotayı/aralığı aşan videolar Story'ye gider (en iyi çaba).
        if ig_publisher is not None:
            await _cross_post_instagram(profile, store, ig_publisher, caption, ready)

    if result.success:
        store.consume_telegram_update(item.update_id)
        store.set_last_post_now()
        log.info("Telegram videosu paylaşıldı (X)")
        return True

    log.warning("Telegram videosu X'e paylaşılamadı: %s", result.detail)
    return False  # offset ilerlemez: sonraki koşuda tekrar denenir


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

    publisher = build_publisher(profile, dry_run=args.dry_run)
    ig_publisher = build_instagram_publisher(profile, dry_run=args.dry_run)

    # ÖNCELİK 1: Telegram kuyruğu (kullanıcının telefondan gönderdiği video).
    # Kuyrukta öğe varsa bu koşuda haber yerine o X'e (ve varsa IG'ye) paylaşılır.
    if await _try_telegram_queue(profile, store, publisher, ig_publisher):
        if persist:
            store.save()
        return

    # ÖNCELİK 2: Normal haber akışı (metin + görsel).
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
            store.add_posted_title(item.title)  # konu-bazlı mükerrer önleme
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
