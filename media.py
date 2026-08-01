# media.py
"""Video indirme, süre kontrolü ve X sınırına göre kırpma yardımcıları.

Link indirme yt-dlp ile yapılır (TikTok, YouTube, X, Instagram vb. destekler).
X'te (Premium olmayan hesapta) video süresi ~140 saniyeyi aşamaz; uzun videolar
ffmpeg varsa baştan kırpılır, yoksa öğe atlanır (bot sessizce durmaz).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# yt-dlp'yi modül olarak çağır: PATH'te binary olmasa da (venv, CI) çalışır.
YTDLP_CMD = [sys.executable, "-m", "yt_dlp"]

# Sabitler
# X (Premium olmayan hesap) 2 dakikadan uzun videoyu reddediyor (403). Güvenli
# olsun diye 118 sn'ye kırpıyoruz (120'nin biraz altı).
X_MAX_VIDEO_SECONDS = 118
# Instagram Story video sınırı ~60 sn; güvenli olsun diye 58 sn.
STORY_MAX_VIDEO_SECONDS = 58
DOWNLOAD_TIMEOUT_SECONDS = 240     # tek video indirme üst sınırı
FFMPEG_TIMEOUT_SECONDS = 300
# X uyumlu, makul boyutlu mp4 tercih et (720p'ye kadar).
YTDLP_FORMAT = "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b[ext=mp4]/b"


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess | None:
    """Harici komutu çalıştırır; hata/zaman aşımında None döner."""
    try:
        return subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Komut çalıştırılamadı (%s): %s", command[0], exc)
        return None


def download_video(url: str, target_dir: Path) -> Path | None:
    """Verilen linkteki videoyu yt-dlp ile indirir; yolunu döndürür."""
    target_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(target_dir / "tgvideo.%(ext)s")
    command = [
        *YTDLP_CMD,
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "-f", YTDLP_FORMAT,
        "--merge-output-format", "mp4",
        "-o", output_template,
        url,
    ]
    result = _run(command, DOWNLOAD_TIMEOUT_SECONDS)
    if result is None or result.returncode != 0:
        detail = (result.stderr or "").strip()[:200] if result else "komut yok"
        logger.warning("Video indirilemedi: %s", detail)
        return None
    files = sorted(target_dir.glob("tgvideo.*"))
    return files[0] if files else None


def video_description(url: str) -> str:
    """Linkteki videonun kaynak açıklamasını (tweet metni/post caption) döndürür.

    Önce %(description)s, boşsa %(title)s denenir. Alınamazsa boş metin döner.
    """
    result = _run(
        [*YTDLP_CMD, "--skip-download", "--no-warnings",
         "-O", "%(description)s", "-O", "%(title)s", url],
        60,
    )
    if result is None or result.returncode != 0:
        return ""
    lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip() and ln.strip() != "NA"]
    return lines[0][:1500] if lines else ""


def video_duration(path: Path) -> float | None:
    """Videonun saniye cinsinden süresini döndürür (ffprobe yoksa None)."""
    if not shutil.which("ffprobe"):
        return None
    result = _run(
        [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", str(path),
        ],
        60,
    )
    if result is None or result.returncode != 0:
        return None
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def trim_video(path: Path, seconds: int = X_MAX_VIDEO_SECONDS) -> Path | None:
    """Videoyu baştan `seconds` kadar kırpar; ffmpeg yoksa None döner.

    -c copy yerine yeniden kodlar: X uyumlu, keyframe'den başlayan temiz bir mp4
    üretir (kopyalama kırpması X'te bozuk/oynatılamaz video yapabiliyor).
    """
    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg yok — uzun video kırpılamıyor")
        return None
    output = path.with_name(f"trimmed_{path.stem}.mp4")
    result = _run(
        [
            "ffmpeg", "-y", "-i", str(path), "-t", str(seconds),
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output),
        ],
        FFMPEG_TIMEOUT_SECONDS,
    )
    if result is None or result.returncode != 0 or not output.exists():
        logger.warning("Video kırpılamadı")
        return None
    return output


def prepare_story(path: Path) -> Path | None:
    """Videoyu Instagram Story için hazırlar (≤58 sn). Uygunsa olduğu gibi döner."""
    duration = video_duration(path)
    if duration is not None and duration <= STORY_MAX_VIDEO_SECONDS:
        return path
    logger.info("Story için video ilk %d saniyeye kırpılıyor", STORY_MAX_VIDEO_SECONDS)
    trimmed = trim_video(path, STORY_MAX_VIDEO_SECONDS)
    if trimmed is None:
        return path if (duration is not None and duration <= STORY_MAX_VIDEO_SECONDS) else None
    return trimmed


def prepare_for_x(path: Path) -> Path | None:
    """Videoyu X'e uygun hale getirir: süre sınırını aşıyorsa (veya ölçülemezse) kırpar."""
    duration = video_duration(path)
    if duration is not None and duration <= X_MAX_VIDEO_SECONDS:
        logger.info("Video süresi uygun: %.0f sn", duration)
        return path
    if duration is None:
        # Süre ölçülemedi: X sınırını aşma riskine karşı GÜVENLİ tarafta kalıp
        # yine de kırpmayı dene (ffmpeg varsa). Böylece 403 (çok uzun) önlenir.
        logger.info("Video süresi ölçülemedi — güvenlik için %d sn'ye kırpılıyor", X_MAX_VIDEO_SECONDS)
    else:
        logger.info(
            "Video %.0f sn (> %d sn) — ilk %d saniyeye kırpılıyor",
            duration, X_MAX_VIDEO_SECONDS, X_MAX_VIDEO_SECONDS,
        )
    trimmed = trim_video(path)
    # ffmpeg yoksa kırpılamaz; süre biliniyor ve uygunsa orijinali döndür, değilse None.
    if trimmed is None:
        return path if (duration is not None and duration <= X_MAX_VIDEO_SECONDS) else None
    return trimmed
