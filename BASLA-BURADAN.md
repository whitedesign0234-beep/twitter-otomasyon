# 👋 Başla Buradan (X Resmi API ile)

Bu bot, X (Twitter) hesabına **resmi API üzerinden** otomatik haber paylaşır.
Tarayıcı yok, giriş yok, ban riski yok. Kurulumu ben yaptım; sana **API anahtarı almak** kaldı.

---

## ✅ Benim yaptıklarım
- Sanal ortam + tüm paketler (tweepy dahil) kuruldu.
- Sistem canlı haberle test edildi, çalışıyor.
- 30 dakikada 1 tweet, 7/24 olacak şekilde ayarlandı (aylık ~1.440 — ücretsiz katmana sığar).

## 📝 Sana kalan: X API anahtarlarını al (tek seferlik, ~10 dk)

### 1. Geliştirici hesabı aç
1. **developer.x.com** adresine git, **paylaşım yapacağın X hesabıyla** giriş yap.
2. **"Sign up for Free Account"** (Ücretsiz) ile başvur.
3. Kullanım açıklaması isterse şöyle bir şey yaz (İngilizce):
   *"Posting my own automated news summaries to my account."*

### 2. İZİNLERİ AYARLA (en kritik adım!)
1. Projene/uygulamana gir → **Settings** → **User authentication settings** → **Set up**.
2. **App permissions: "Read and write"** seç. *(Sadece "Read" olursa paylaşım yapamaz!)*
3. Type of App: **"Web App, Automated App or Bot"**.
4. Callback URL / Website: geçici olarak `https://localhost` yazabilirsin. Kaydet.

### 3. Anahtarları kopyala
1. **Keys and tokens** sekmesine git.
2. Şunları oluştur/kopyala:
   | Panel'deki adı | .env'deki karşılığı |
   |---|---|
   | API Key | `X_API_KEY` |
   | API Key Secret | `X_API_SECRET` |
   | Access Token | `X_ACCESS_TOKEN` |
   | Access Token Secret | `X_ACCESS_TOKEN_SECRET` |

   ⚠️ **Access Token'ı, 2. adımdaki "Read and write" iznini AYARLADIKTAN SONRA oluştur.**
   Önce oluşturursan token salt-okunur olur ve paylaşım 403 hatası verir. Gerekirse
   "Regenerate" ile yenile.

### 4. Anahtarları `.env`'e yaz
`.env` dosyasını Not Defteri ile aç, şu satırları doldur:
```
X_API_KEY=buraya
X_API_SECRET=buraya
X_ACCESS_TOKEN=buraya
X_ACCESS_TOKEN_SECRET=buraya
```

---

## 🧪 Test et (paylaşmadan)
```powershell
.\3-TEST-ET.ps1
```
"[DRY-RUN] Paylaşılacaktı: ..." görürsen içerik hazır demektir.

## 🚀 Gerçekten paylaş (yerel deneme)
`.env` içindeki `DRY_RUN=1`'i **`DRY_RUN=0`** yap, sonra:
```powershell
.\.venv\Scripts\python.exe main.py --profile haber --limit 1
```
X profilini kontrol et — tweet atıldıysa 🎉. (Sonra `DRY_RUN=1`'e geri alabilirsin.)

## ♾️ 7/24 otomatik (GitHub)
1. GitHub'da özel bir depo aç, bu klasörü yükle.
2. **Settings → Secrets and variables → Actions**'a şunları ekle:
   `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`, `GEMINI_API_KEY`, `GEMINI_MODEL`.
3. Bot her 30 dakikada bir otomatik paylaşır. Elle çalıştırma: **Actions → bot → Run workflow**.

---

## ⚠️ Önemli notlar
- **Ücretsiz katman ~1.500 paylaşım/ay.** Senin ayarın ~1.440/ay — sığar ama sınıra yakın; ay içinde elle çok tweet atma.
- **Görsel:** Ücretsiz katmanda görsel yükleme kapalı olabilir; o durumda bot otomatik **metin+link** olarak paylaşır (sorun değil).
- **Gemini anahtarı** (isteğe bağlı): daha akıcı metin için `.env`'de `GEMINI_API_KEY` doldur. Boşsa bot başlığı kısaltıp paylaşır.
- Hesap ayarları (kaynaklar, saatler): [profiles/haber.yaml](profiles/haber.yaml). Teknik detay: [README.md](README.md).
