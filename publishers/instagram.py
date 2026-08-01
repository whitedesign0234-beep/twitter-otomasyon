# publishers/instagram.py
"""Instagram'a (Graph API / Instagram Login) Reels olarak video paylaşan yayıncı.

Instagram dosya yüklemeyi kabul etmez; videoyu bir PUBLIC URL'den çeker. Bu
yüzden indirilen video önce geçici bir dosya-host'a yüklenir, URL'si Instagram'a
verilir. Akış: public URL -> medya konteyneri (REELS) -> işlenmeyi bekle -> yayınla.
Ücretsizdir (tweet gibi işlem başına ücret yoktur); token 60 günde bir yenilenmeli.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import requests

from publishers.base import PostResult

logger = logging.getLogger(__name__)

# Facebook Login yolu: içerik yayınlama graph.facebook.com + Page token ile
# çalışıyor (graph.instagram.com / "Instagram Login" yolu işlemede takılıyordu).
GRAPH_BASE = "https://graph.facebook.com/v21.0"
CATBOX_URL = "https://catbox.moe/user/api.php"      # ücretsiz geçici dosya-host
UPLOAD_TIMEOUT_SECONDS = 60      # başarısız host'ta uzun beklememek için kısa
UPLOAD_ATTEMPTS = 2
API_TIMEOUT_SECONDS = 60
CONTAINER_POLL_SECONDS = 5
CONTAINER_POLL_MAX = 40                              # ~200 sn işleme beklemesi


def read_config() -> dict[str, str] | None:
    """Instagram token + kullanıcı ID'sini env'den okur; eksikse None döner."""
    token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "").strip()
    user_id = os.environ.get("INSTAGRAM_USER_ID", "").strip()
    if token and user_id:
        return {"token": token, "user_id": user_id}
    return None


_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def _up_0x0(path: str) -> str | None:
    """0x0.st'ye yükler (sunucu dostu)."""
    with open(path, "rb") as f:
        r = requests.post("https://0x0.st", files={"file": f},
                          headers={"User-Agent": _UA}, timeout=UPLOAD_TIMEOUT_SECONDS)
    r.raise_for_status()
    url = r.text.strip()
    return url if url.startswith("http") else None


def _up_tmpfiles(path: str) -> str | None:
    """tmpfiles.org'a yükler; doğrudan indirme (/dl/) URL'sine çevirir."""
    with open(path, "rb") as f:
        r = requests.post("https://tmpfiles.org/api/v1/upload",
                          files={"file": (os.path.basename(path), f)},
                          headers={"User-Agent": _UA}, timeout=UPLOAD_TIMEOUT_SECONDS)
    r.raise_for_status()
    url = (r.json().get("data") or {}).get("url", "")
    # tmpfiles.org/123/x.mp4 -> tmpfiles.org/dl/123/x.mp4 (doğrudan dosya)
    return url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1) if url.startswith("http") else None


def _up_catbox(path: str) -> str | None:
    """catbox.moe'ye yükler (bazı sunucu IP'lerini engelleyebilir)."""
    with open(path, "rb") as f:
        r = requests.post(CATBOX_URL, data={"reqtype": "fileupload"},
                          files={"fileToUpload": f},
                          headers={"User-Agent": _UA}, timeout=UPLOAD_TIMEOUT_SECONDS)
    r.raise_for_status()
    url = r.text.strip()
    return url if url.startswith("http") else None


def _upload_public(video_path: str) -> str | None:
    """Videoyu birden fazla public host'a sırayla dener; ilk başaranın URL'si döner.

    GitHub runner'ın sunucu IP'si bazı host'larca engellenebildiği için (ör. catbox
    412) çoklu host + yeniden deneme kullanılır.
    """
    hosts = [("0x0.st", _up_0x0), ("tmpfiles", _up_tmpfiles), ("catbox", _up_catbox)]
    for name, uploader in hosts:
        for attempt in range(UPLOAD_ATTEMPTS):
            try:
                url = uploader(video_path)
                if url:
                    logger.info("Public host OK: %s", name)
                    return url
                logger.warning("%s beklenmedik yanıt", name)
                break
            except (requests.RequestException, OSError, ValueError) as exc:
                logger.warning("%s deneme %d başarısız: %s", name, attempt + 1, str(exc)[:120])
                time.sleep(2)
    return None


class InstagramPublisher:
    """Instagram hesabına Reels olarak video paylaşır (X'e ek çapraz paylaşım)."""

    def __init__(self, config: dict[str, str]) -> None:
        """Yayıncıyı token + user_id ile hazırlar."""
        self._token = config["token"]
        self._user_id = config["user_id"]

    async def publish_video(self, text: str, video_path: str) -> PostResult:
        """Videoyu Instagram Reels olarak paylaşır (senkron akış ayrı thread'de)."""
        return await asyncio.to_thread(self._publish_sync, text, video_path)

    async def publish(self, text: str, image_path: str | None) -> PostResult:
        """Instagram metin-only paylaşımı desteklemez (görsel/video gerekir)."""
        return PostResult(success=False, detail="instagram-text-only-desteklenmiyor")

    def _publish_sync(self, text: str, video_path: str) -> PostResult:
        """Public URL -> konteyner -> işlenmeyi bekle -> yayınla akışını yürütür."""
        public_url = _upload_public(video_path)
        if not public_url:
            return PostResult(success=False, detail="IG: public URL oluşturulamadı")

        creation_id = self._create_container(public_url, text)
        if creation_id is None:
            return PostResult(success=False, detail="IG: medya konteyneri oluşmadı")

        if not self._wait_ready(creation_id):
            return PostResult(success=False, detail="IG: video işlenemedi/zaman aşımı")

        return self._publish_container(creation_id)

    def _create_container(self, video_url: str, caption: str) -> str | None:
        """REELS medya konteyneri oluşturur, creation_id döndürür."""
        try:
            data = requests.post(
                f"{GRAPH_BASE}/{self._user_id}/media",
                data={
                    "media_type": "REELS",
                    "video_url": video_url,
                    "caption": caption,
                    "access_token": self._token,
                },
                timeout=API_TIMEOUT_SECONDS,
            ).json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("IG konteyner isteği hatası: %s", exc)
            return None
        if "id" in data:
            return data["id"]
        logger.warning("IG konteyner hatası: %s", data.get("error", data))
        return None

    def _wait_ready(self, creation_id: str) -> bool:
        """Konteynerin işlenmesini (FINISHED) bekler; hata/timeout'ta False."""
        for _ in range(CONTAINER_POLL_MAX):
            time.sleep(CONTAINER_POLL_SECONDS)
            try:
                data = requests.get(
                    f"{GRAPH_BASE}/{creation_id}",
                    params={"fields": "status_code", "access_token": self._token},
                    timeout=API_TIMEOUT_SECONDS,
                ).json()
            except (requests.RequestException, ValueError):
                continue
            status = data.get("status_code")
            if status == "FINISHED":
                return True
            if status == "ERROR":
                logger.warning("IG video işleme hatası (ERROR)")
                return False
        logger.warning("IG video işleme zaman aşımı")
        return False

    def _publish_container(self, creation_id: str) -> PostResult:
        """İşlenmiş konteyneri yayınlar."""
        try:
            data = requests.post(
                f"{GRAPH_BASE}/{self._user_id}/media_publish",
                data={"creation_id": creation_id, "access_token": self._token},
                timeout=API_TIMEOUT_SECONDS,
            ).json()
        except (requests.RequestException, ValueError) as exc:
            return PostResult(success=False, detail=f"IG yayın hatası: {exc}")
        if "id" in data:
            logger.info("Instagram'a paylaşıldı (id=%s)", data["id"])
            return PostResult(success=True, detail=f"ig_id={data['id']}")
        return PostResult(success=False, detail=f"IG yayın reddi: {data.get('error', data)}")
