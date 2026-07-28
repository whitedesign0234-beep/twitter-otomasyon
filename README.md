# Çok Hesaplı X (Twitter) Otomatik Paylaşım Motoru

Profil tabanlı, eklenti mimarili bir içerik otomasyon motoru. Her profil kendi RSS
kaynaklarından haber çeker, Google Gemini ile özgün metne dönüştürür ve tarayıcı
otomasyonuyla kendi X hesabına paylaşır.

> **Yeni başlayan mısın?** Teknik detaya girmeden başlamak için [BASLA-BURADAN.md](BASLA-BURADAN.md) oku.

> ⚠️ **Uyarı:** Tarayıcı otomasyonuyla otomatik paylaşım X'in kullanım şartlarına
> aykırıdır; hesap askıya alınabilir. Kendi hesabınla, kendi riskinle kullan.

---

## Tasarım ilkesi

**Yeni hesap eklemek = `profiles/` dizinine bir YAML dosyası + bir GitHub Secret.**
Python koduna veya workflow'a dokunulmaz. Kodda `if profile == "..."` dalı yoktur;
her şey YAML + registry üzerinden çözülür.

## Akış

```
profiles/*.yaml → sources/ (registry) → selector.py → rewriter.py → publishers/ → store.py
   (kalp)          RSS, hata izole       skor+dedupe   Gemini+fallback  Playwright/DryRun  state/<ns>.json
```

## Dizin yapısı

| Yol | Görev |
|---|---|
| `config/schema.py` | Pydantic profil şeması + yükleyici |
| `profiles/*.yaml` | Hesap tanımları (mimarinin kalbi) |
| `sources/` | İçerik kaynağı eklentileri (`@register` ile) |
| `selector.py` | Skorlama, benzerlik ve zamanlama kapıları |
| `rewriter.py` | Gemini ile metin + deterministik fallback |
| `publishers/` | `TwitterBrowserPublisher`, `DryRun`, Bluesky/Mastodon iskeleti |
| `store.py` | Profil-bazlı state, UTM-temiz SHA-256 dedupe, TTL |
| `main.py` | Orkestrasyon + CLI |
| `scripts/login_and_save_session.py` | Headful giriş → base64 oturum |
| `.github/workflows/main.yml` | Dinamik matrix ile zamanlama |

## CLI

```bash
python main.py --profile haber            # tek profil, canlı
python main.py --all                       # tüm aktif profiller
python main.py --profile haber --dry-run   # paylaşma, sadece logla
python main.py --profile haber --dry-run --ignore-schedule  # zamanlama kapılarını atla (test)
python main.py --profile haber --limit 1   # en çok 1 paylaşım
python main.py --profile haber --source "NTV Son Dakika"    # tek kaynak
```

## Kurulum (özet)

1. `.\1-KUR.ps1` (venv + paketler + Chromium) — veya elle:
   `python -m venv .venv && .venv\Scripts\pip install -r requirements.txt && python -m playwright install chromium`
2. `.env.example` → `.env`, `GEMINI_API_KEY` doldur.
3. `.\2-GIRIS-YAP.ps1` ile X oturumu üret, base64'ü `SESSION_B64_HABER` secret'ına ekle.
4. `.\3-TEST-ET.ps1` ile dry-run test et.
5. Repoyu (state dahil) push et; GitHub Actions `*/30`'da çalışır.

## Yeni hesap ekleme (kod değişikliği yok)

1. `profiles/haber.yaml` → `profiles/spor.yaml` kopyala, alanları değiştir
   (`name`, `account_handle`, `session_secret_name: SESSION_B64_SPOR`, `state.namespace: spor`).
2. `.\2-GIRIS-YAP.ps1 spor` → base64'ü `SESSION_B64_SPOR` secret'ına ekle.
3. `main.yml`'de `env:` altına tek satır: `SESSION_B64_SPOR: ${{ secrets.SESSION_B64_SPOR }}`.
4. Push et. Profil `discover` job'u tarafından otomatik matrix'e alınır.

## State kalıcılığı — neden repoya commit?

GitHub Actions runner'ları efemeraldir. Üç seçenek: (a) `actions/cache` — basit ama
7 günde silinebilir + yarış riski; (b) **repoya commit** — en dayanıklı, sıfır maliyet;
(c) Gist — dayanıklı ama ekstra token/kompleksite. **Seçim: (b).** Çakışma per-profil
ayrı dosya + `git pull --rebase` + retry ile yönetilir (bkz. `store.py`, `main.yml`).

## Sorun giderme

| Sorun | Belirti | Çözüm |
|---|---|---|
| Selektör değişikliği | "Editör/Paylaş düğmesi bulunamadı", artifact'te screenshot | `publishers/twitter_browser.py` selektör listelerine yeni `data-testid` ekle |
| Oturum düşmesi | "Oturum geçersiz", çıkış kodu 0 | `.\2-GIRIS-YAP.ps1` tekrar çalıştır, secret'ı güncelle |
| Gemini kotası | Log'da `fallback=True` | Beklenen; kota sıfırlanınca düzelir. `GEMINI_MODEL`'i değiştir |
| Actions cron gecikmesi | 30 dk'da değil, gecikmeli | Normaldir (cron garantisiz); kod state ile dayanıklı |
| State kaybı/çakışma | Mükerrer paylaşım/push hatası | `main.yml`'deki rebase+retry çözer |
| Bir kaynak 0 döndürüyor | "[Kaynak] 0 taze öğe" | Feed URL'si eskimiş veya `max_age_hours` çok kısa olabilir; YAML'da kontrol et |

## Notlar

- `google-generativeai` paketi deprecated (import'ta uyarı bastırılır). İleride
  `google-genai`'ye geçilebilir; `rewriter.py` izole olduğu için tek dosya değişir.
- Bluesky/Mastodon API'leri gerçekten ücretsizdir; `publishers/base.py` içindeki
  iskeletler aynı `Publisher` protokolünü uygular, doldurmak yeterli.
