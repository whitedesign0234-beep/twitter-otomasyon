# scripts/_api_check.py
"""X API anahtarlarını PAYLAŞMADAN doğrular: kimlik + yazma izni kontrolü."""

import os

import tweepy
from dotenv import load_dotenv

load_dotenv()

keys = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")
vals = {k: os.environ.get(k, "").strip() for k in keys}
for k in keys:
    v = vals[k]
    print(f"{k}: {'DOLU (' + str(len(v)) + ' karakter)' if v else 'BOŞ!'}")
if not all(vals.values()):
    print("\nHATA: Eksik anahtar var.")
    raise SystemExit(1)

client = tweepy.Client(
    consumer_key=vals["X_API_KEY"],
    consumer_secret=vals["X_API_SECRET"],
    access_token=vals["X_ACCESS_TOKEN"],
    access_token_secret=vals["X_ACCESS_TOKEN_SECRET"],
)

print("\n--- Kimlik dogrulama testi (get_me, okuma) ---")
try:
    me = client.get_me()
    print("BASARILI: Giris yapan hesap ->", me.data.username if me and me.data else me)
except tweepy.Unauthorized as e:
    print("401 UNAUTHORIZED: anahtarlar hatali/eksik (OCR hatasi olabilir) ->", str(e)[:150])
except tweepy.Forbidden as e:
    print("403 FORBIDDEN: kimlik OK ama izin/erisim kisitli ->", str(e)[:150])
except tweepy.TooManyRequests as e:
    print("429: kota/limit ->", str(e)[:150])
except tweepy.HTTPException as e:
    # 403/402/453 gibi odeme/kredi hatalari burada da cikabilir
    print("HTTP HATASI:", getattr(e, "api_codes", ""), "->", str(e)[:200])
except Exception as e:
    print("DIGER HATA:", type(e).__name__, "->", str(e)[:200])
