# telegram_inbox.py
"""Telegram botuna gelen video/linkleri kuyruk olarak okur.

Kullanıcı telefonundan bir videoyu ya da linki (TikTok/YouTube/X vb.) bota
iletir; bu modül getUpdates ile bekleyen öğeleri sırayla verir. Öğe ancak
BAŞARIYLA paylaşıldıktan sonra tüketilmiş sayılır (offset ilerletilir), böylece
hata durumunda video kaybolmaz.

GÜVENLİK: Depo public olduğundan bot kullanıcı adı herkese açıktır. Yalnızca
TELEGRAM_ALLOWED_CHAT_IDS listesindeki chat'lerden gelen içerik kabul edilir;
diğerleri sessizce yok sayılır (yabancı biri hesabı kullanamaz).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT_SECONDS = 20
# Telegram bot API'si dosya indirmede 20 MB sınırı uygular.
MAX_TELEGRAM_FILE_BYTES = 20 * 1024 * 1024
URL_PATTERN = re.compile(r"https?://\S+")


@dataclass
class InboxItem:
    """Telegram'dan gelen tek bir paylaşım adayı."""

    update_id: int          # tüketim için sıra numarası
    kind: str               # "video" (dosya) veya "link"
    file_id: str | None     # kind="video" ise Telegram dosya kimliği
    url: str | None         # kind="link" ise indirilecek bağlantı
    caption: str            # kullanıcının yazdığı açıklama (varsa)


def _token() -> str | None:
    """Bot token'ını ortamdan okur; yoksa None döner."""
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or None


def _allowed_chat_ids() -> set[str]:
    """İzin verilen chat id kümesini ortamdan okur."""
    raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def _api(method: str, **params) -> dict | None:
    """Telegram API'sini çağırır; hata durumunda None döner."""
    token = _token()
    if not token:
        return None
    try:
        response = requests.get(
            f"{API_BASE}/bot{token}/{method}",
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Telegram %s çağrısı başarısız: %s", method, exc)
        return None
    if not data.get("ok"):
        logger.warning("Telegram %s hatası: %s", method, data.get("description"))
        return None
    return data


def _extract_item(update: dict, allowed: set[str]) -> InboxItem | None:
    """Bir update'ten paylaşılabilir öğe çıkarır; uygun değilse None döner."""
    update_id = update.get("update_id")
    message = update.get("message") or update.get("channel_post") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))

    # GÜVENLİK KAPISI: yalnızca izinli chat'ler.
    if allowed and chat_id not in allowed:
        logger.info("Telegram: izinsiz chat'ten gelen mesaj yok sayıldı")
        return None

    caption = (message.get("caption") or message.get("text") or "").strip()

    # 1) Doğrudan video dosyası (video veya video olarak gönderilen doküman).
    video = message.get("video")
    if video and int(video.get("file_size") or 0) <= MAX_TELEGRAM_FILE_BYTES:
        return InboxItem(update_id, "video", video.get("file_id"), None, caption)
    if video:
        logger.warning("Telegram: video 20 MB'tan büyük, link göndermek daha sağlam")

    document = message.get("document")
    if document and str(document.get("mime_type", "")).startswith("video/"):
        if int(document.get("file_size") or 0) <= MAX_TELEGRAM_FILE_BYTES:
            return InboxItem(update_id, "video", document.get("file_id"), None, caption)

    # 2) Metinde/altyazıda link varsa (TikTok, YouTube, X...) — boyut sınırı yok.
    match = URL_PATTERN.search(caption)
    if match:
        url = match.group(0)
        # Linki açıklamadan çıkar; kalan metin caption olarak kullanılır.
        text = URL_PATTERN.sub("", caption).strip()
        return InboxItem(update_id, "link", None, url, text)

    return None


def pending_items(offset: int) -> list[InboxItem]:
    """Verilen offset'ten sonraki bekleyen öğeleri sırayla döndürür."""
    if not _token():
        return []
    data = _api("getUpdates", offset=offset + 1, timeout=0, limit=20)
    if not data:
        return []
    allowed = _allowed_chat_ids()
    if not allowed:
        logger.warning(
            "TELEGRAM_ALLOWED_CHAT_IDS boş — güvenlik için Telegram kuyruğu devre dışı"
        )
        return []
    items: list[InboxItem] = []
    for update in data.get("result", []):
        item = _extract_item(update, allowed)
        if item:
            items.append(item)
    if items:
        logger.info("Telegram kuyruğunda %d öğe bekliyor", len(items))
    return items


def download_file(file_id: str, target_dir: str) -> str | None:
    """Telegram dosyasını indirir ve yerel yolunu döndürür."""
    token = _token()
    data = _api("getFile", file_id=file_id)
    if not data or not token:
        return None
    file_path = (data.get("result") or {}).get("file_path")
    if not file_path:
        return None
    url = f"{API_BASE}/file/bot{token}/{file_path}"
    target = os.path.join(target_dir, os.path.basename(file_path))
    try:
        with requests.get(url, stream=True, timeout=120) as response:
            response.raise_for_status()
            with open(target, "wb") as handle:
                for chunk in response.iter_content(chunk_size=1 << 16):
                    handle.write(chunk)
    except (requests.RequestException, OSError) as exc:
        logger.warning("Telegram dosyası indirilemedi: %s", exc)
        return None
    return target


def latest_chat_ids() -> list[str]:
    """Bota yazan chat id'lerini listeler (kurulumda chat id bulmak için)."""
    data = _api("getUpdates", timeout=0, limit=20)
    if not data:
        return []
    ids: list[str] = []
    for update in data.get("result", []):
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if chat.get("id") is not None:
            ids.append(str(chat["id"]))
    return sorted(set(ids))
