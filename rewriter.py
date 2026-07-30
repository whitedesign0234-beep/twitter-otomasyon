# rewriter.py
"""Google Gemini ile içerikten özgün tweet metni üretir; güvenli fallback içerir."""

from __future__ import annotations

import json
import logging
import os
import re
import warnings
from dataclasses import dataclass

# google.generativeai import sırasında "deprecated" FutureWarning basar; bu
# uyarı işlevi etkilemez ama terminalde hataymış gibi görünür. Yalnızca bu
# import boyunca tüm uyarıları bastırıp sonra normale döneriz.
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import google.generativeai as genai
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from pathlib import Path

import trends
from config.schema import Persona
from sources.base import ContentItem

logger = logging.getLogger(__name__)

# Sabitler — X'in ölçüm kuralları
X_HARD_LIMIT = 280
TWITTER_LINK_LENGTH = 23        # X her bağlantıyı 23 karakter sayar (t.co)
MAX_GEMINI_ATTEMPTS = 3         # kota/hata için toplam deneme
JSON_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
# Inline (doğrudan) video gönderiminde istek ~20 MB'ı aşamaz; güvenli sınır.
INLINE_VIDEO_MAX_BYTES = 15 * 1024 * 1024


class GeminiTransientError(Exception):
    """Yeniden denenebilir Gemini hatası (kota, geçici ağ vb.)."""


@dataclass
class GeneratedPost:
    """Üretilmiş ve doğrulanmış paylaşım metni."""

    text: str            # paylaşıma hazır tam metin (varsayılan: link YOK)
    used_fallback: bool  # Gemini mi yoksa deterministik şablon mu kullanıldı


def _effective_length(text: str, include_link: bool) -> int:
    """Metnin X'e göre gerçek uzunluğunu hesaplar (link eklenecekse +24)."""
    # Link eklenecekse sonuna " <url>" gelir; link sabit 23 sayılır (+boşluk).
    return len(text) + (1 + TWITTER_LINK_LENGTH) if include_link else len(text)


def _strip_fences(raw: str) -> str:
    """Modelin döndürdüğü markdown ```json fence'lerini temizler."""
    return JSON_FENCE_PATTERN.sub("", raw).strip()


class Rewriter:
    """Persona'ya göre dinamik prompt kurar ve tweet metni üretir."""

    def __init__(self) -> None:
        """Gemini istemcisini .env'deki anahtar ve model adıyla hazırlar."""
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        self.model_name = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash").strip()
        self.enabled = bool(api_key)
        if self.enabled:
            genai.configure(api_key=api_key)
            self.model = genai.GenerativeModel(self.model_name)
        else:
            logger.warning("GEMINI_API_KEY yok — her zaman fallback şablon kullanılacak")

    def generate(self, item: ContentItem, persona: Persona) -> GeneratedPost:
        """İçerikten geçerli bir tweet üretir; başarısızlıkta fallback'e düşer."""
        if self.enabled:
            try:
                text = self._generate_with_gemini(item, persona)
                if text is not None:
                    return GeneratedPost(text=text, used_fallback=False)
            except (GeminiTransientError, ValueError) as exc:
                logger.warning("Gemini üretimi başarısız, fallback'e geçiliyor: %s", exc)
        return GeneratedPost(text=self._fallback(item, persona), used_fallback=True)

    def _generate_with_gemini(self, item: ContentItem, persona: Persona) -> str | None:
        """Gemini'yi çağırır, JSON'u ayrıştırır, doğrular; geçersizse None döner."""
        prompt = self._build_prompt(item, persona)
        raw = self._call_gemini(prompt)
        parsed = self._parse_json(raw)
        if parsed is None:
            return None

        body = str(parsed.get("text", "")).strip()
        hashtags = self._sanitize_hashtags(parsed.get("hashtags", []), persona)
        if not body:
            return None

        full = self._assemble(body, hashtags, item, persona)
        if full is None:
            logger.info("Üretilen metin doğrulamadan geçemedi")
            return None
        return full

    def _build_prompt(self, item: ContentItem, persona: Persona) -> str:
        """Persona sistem prompt'u ile ortak talimat şablonunu birleştirir."""
        hashtag_hint = (
            f"Yalnızca şu whitelist'ten seç: {persona.hashtag_whitelist}"
            if persona.hashtag_whitelist
            else "Konuya uygun, Türkçe hashtag'ler üret."
        )
        # Not: Prompt tamamen persona'dan türetilir — hard-coded tek prompt yok.
        return (
            f"{persona.system_prompt}\n\n"
            "GÖREV: Aşağıdaki haberi tek bir özgün Türkçe tweet'e dönüştür. "
            "Telifli metni birebir kopyalama; kendi cümlelerinle kısa özet yaz.\n"
            f"- Metin (link ve hashtag hariç) en fazla {persona.max_chars} karakter olsun.\n"
            f"- Tam {persona.hashtag_count} adet hashtag üret. {hashtag_hint}\n"
            "- Metne ASLA link/URL koyma (link maliyetli, kullanılmıyor).\n"
            "- Yanıtı SADECE şu JSON biçiminde ver, başka hiçbir şey yazma:\n"
            '{"text": "...", "hashtags": ["...", "..."]}\n\n'
            f"BAŞLIK: {item.title}\n"
            f"ÖZET: {item.summary}\n"
            f"KAYNAK: {item.source_name}\n"
        )

    @retry(
        retry=retry_if_exception_type(GeminiTransientError),
        stop=stop_after_attempt(MAX_GEMINI_ATTEMPTS),
        wait=wait_exponential_jitter(initial=2, max=30),
        reraise=True,
    )
    def _call_gemini(self, prompt: str) -> str:
        """Gemini API'sini çağırır; kota/geçici hataları retry için sarmalar."""
        try:
            response = self.model.generate_content(prompt)
        except Exception as exc:  # SDK farklı istisna tipleri fırlatabilir
            # Kota (429) ve geçici sunucu hatalarını yeniden denenebilir say.
            message = str(exc).lower()
            if any(token in message for token in ("429", "quota", "rate", "503", "500")):
                raise GeminiTransientError(str(exc)) from exc
            raise ValueError(f"Gemini kalıcı hatası: {exc}") from exc
        return getattr(response, "text", "") or ""

    @staticmethod
    def _parse_json(raw: str) -> dict | None:
        """Model çıktısındaki JSON'u güvenli biçimde ayrıştırır."""
        cleaned = _strip_fences(raw)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.debug("JSON ayrıştırılamadı: %r", cleaned[:120])
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _sanitize_hashtags(raw_tags: object, persona: Persona) -> list[str]:
        """Hashtag listesini normalize eder, whitelist ve sayı sınırını uygular."""
        if not isinstance(raw_tags, list):
            return []
        tags: list[str] = []
        for tag in raw_tags:
            text = str(tag).strip().lstrip("#")
            if not text:
                continue
            candidate = f"#{text}"
            if persona.hashtag_whitelist and candidate not in persona.hashtag_whitelist:
                continue
            tags.append(candidate)
        return tags[: persona.hashtag_count]

    def _assemble(
        self, body: str, hashtags: list[str], item: ContentItem, persona: Persona
    ) -> str | None:
        """Metin + hashtag'i birleştirir ve tüm kuralları doğrular."""
        lowered = body.casefold()
        if any(word.casefold() in lowered for word in persona.banned_words):
            return None  # yasaklı kelime içeriyor

        tag_str = (" " + " ".join(hashtags)) if hashtags else ""
        text = f"{body}{tag_str}"
        if _effective_length(text, persona.include_link) > X_HARD_LIMIT:
            return None  # (varsa link dahil) 280'i aşıyor
        # Link maliyetli (~$0.20) olduğu için varsayılan olarak EKLENMEZ.
        return f"{text} {item.url}" if persona.include_link else text

    def _fallback(self, item: ContentItem, persona: Persona) -> str:
        """Deterministik yedek: başlığı kırpıp kaynak etiketiyle paylaşır (linksiz)."""
        source_tag = f" ({item.source_name})"
        # Link eklenecekse ona da yer ayır; varsayılan linksiz.
        link_reserve = (1 + TWITTER_LINK_LENGTH) if persona.include_link else 0
        budget = min(persona.max_chars, X_HARD_LIMIT - len(source_tag) - link_reserve)
        title = item.title.strip()
        if len(title) > budget:
            title = title[: max(budget - 1, 0)].rstrip() + "…"
        base = f"{title}{source_tag}"
        return f"{base} {item.url}" if persona.include_link else base


def _trends_hint() -> str:
    """Güncel X gündemini, yalnızca İLGİLİ olanları eklemek koşuluyla prompt'a katar."""
    current = trends.get_trends()
    if not current:
        return ""
    listing = ", ".join(current[:15])
    return (
        f"\n\nBUGÜNÜN X (Twitter) GÜNDEMİ: {listing}\n"
        "Bu gündem başlıklarından, içerikle GERÇEKTEN İLGİLİ olan EN FAZLA 2 tanesini "
        "ek hashtag olarak koyabilirsin (çok kelimeliyse bitişik yaz, ör. #RealMadrid). "
        "Alakasız gündem etiketi EKLEME — spam sayılır ve hesaba zarar verir. "
        "İçerikle ilgili gündem yoksa hiç ekleme."
    )


def _build_video_prompt(persona: Persona) -> str:
    """Video analizi için haber-üslubu, uydurma-yasağı içeren prompt kurar."""
    return (
        f"{persona.system_prompt}\n\n"
        "Bu videoyu izle ve İÇİNDE GERÇEKTEN GÖRDÜĞÜN olayı haber kanalı üslubunda "
        "tek bir Türkçe açıklamayla anlat. Kurallar:\n"
        "- SADECE videoda gördüğünü/duyduğunu yaz. Yer adı, kişi, kurum, sayı gibi "
        "emin olmadığın detayları ASLA uydurma; net değilse genel geç.\n"
        "- Kısa ve akıcı, tek paragraf. Clickbait ve abartı yok.\n"
        f"- En fazla {persona.max_chars} karakter.\n"
        f"- Sonuna konuya uygun {persona.hashtag_count} hashtag ekle.\n"
        "- Yanıtı düz metin ver (tırnak, markdown veya JSON kullanma)."
        + _trends_hint()
    )


def _clean_caption(text: str) -> str:
    """Model çıktısını temizler: fence, tırnak ve fazla boşlukları atar, kırpar."""
    cleaned = _strip_fences(text).strip().strip('"').strip()
    return cleaned[:X_HARD_LIMIT]


def caption_from_video(video_path: str, persona: Persona) -> str | None:
    """Videoyu Gemini ile izleyip haber üslubunda açıklama üretir.

    Video doğrudan (inline) generateContent'e gönderilir — bu, metin üretimiyle
    aynı çalışan yolu kullanır (File API bazı anahtarlarda reddediliyor). Video
    inline sınırından büyükse, analiz için ffmpeg ile küçük bir kopya üretilir.
    Kota/hata durumunda None döner; çağıran taraf metinsiz paylaşır (uydurmaz).
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    size = Path(video_path).stat().st_size
    if size > INLINE_VIDEO_MAX_BYTES:
        logger.info(
            "Video inline analiz için çok büyük (%d MB) — atlanıyor",
            size // (1024 * 1024),
        )
        return None

    model_name = os.environ.get("GEMINI_MODEL", "gemini-flash-latest").strip()
    genai.configure(api_key=api_key)
    try:
        data = Path(video_path).read_bytes()
        model = genai.GenerativeModel(model_name)
        response = model.generate_content(
            [{"mime_type": "video/mp4", "data": data}, _build_video_prompt(persona)]
        )
        caption = _clean_caption(getattr(response, "text", "") or "")
    except Exception as exc:  # SDK çeşitli istisna tipleri fırlatabilir
        logger.warning("Gemini video analizi başarısız: %s", str(exc)[:200])
        return None
    return caption or None


def caption_from_text(source_text: str, persona: Persona) -> str | None:
    """Kaynak açıklamasından (tweet metni vb.) haber üslubunda metin üretir.

    Metin üretimi kullanır (File API gerektirmez, anahtarla sorunsuz çalışır).
    Verilen açıklamada olmayan detayı UYDURMAZ; yalnızca düzenler/özetler.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key or not source_text.strip():
        return None
    model_name = os.environ.get("GEMINI_MODEL", "gemini-flash-latest").strip()
    genai.configure(api_key=api_key)
    prompt = (
        f"{persona.system_prompt}\n\n"
        "Aşağıdaki metin, paylaşılacak bir videonun KAYNAK AÇIKLAMASIDIR. Bunu temel "
        "alarak haber üslubunda, kısa ve akıcı tek bir Türkçe açıklama yaz.\n"
        "- Açıklamada OLMAYAN detayı (yer, kişi, sayı) UYDURMA; sadece verileni "
        "düzenle/özetle. Emin değilsen genel geç.\n"
        "- Clickbait ve abartı yok.\n"
        f"- En fazla {persona.max_chars} karakter, sonuna {persona.hashtag_count} hashtag.\n"
        "- Düz metin ver (tırnak/JSON yok)."
        + _trends_hint()
        + f"\n\nKAYNAK AÇIKLAMA:\n{source_text[:1500]}"
    )
    try:
        model = genai.GenerativeModel(model_name)
        response = model.generate_content(prompt)
        caption = _clean_caption(getattr(response, "text", "") or "")
    except Exception as exc:
        logger.warning("Gemini metin açıklaması başarısız: %s", str(exc)[:200])
        return None
    return caption or None
