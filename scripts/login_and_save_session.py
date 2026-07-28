# scripts/login_and_save_session.py
"""Yerelde headful tarayıcı açar, kullanıcı elle giriş yapar, oturumu base64 basar.

Kullanım:
    python scripts/login_and_save_session.py --profile haber

Tarayıcı açılır; X'e elle giriş yapın (2FA dahil). Giriş bitince terminale
ENTER'a basın. storage_state hem dosyaya yazılır hem base64 olarak ekrana basılır;
bu base64 değerini SESSION_B64_<PROFİL> adlı GitHub Secret'a yapıştırın.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

# Sabitler
LOGIN_URL = "https://x.com/login"
OUTPUT_DIR = Path(".sessions")


async def capture_session(profile: str) -> None:
    """Kalıcı profilli, normal Edge gibi bir tarayıcı açıp oturumu kaydeder."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    session_path = OUTPUT_DIR / f"{profile}.session.json"
    # Kalıcı tarayıcı profili: bir kez giriş yapınca burada saklanır, tekrar
    # sorulmaz. .sessions/ .gitignore'da olduğu için commit edilmez.
    profile_dir = OUTPUT_DIR / f"{profile}-browser-profile"

    # Windows'ta sistemdeki Edge kullanılır (indirilen Chromium bozuk);
    # kanal belirtilmemişse varsayılan olarak Edge denenir.
    channel = os.environ.get("PLAYWRIGHT_CHANNEL", "").strip() or "msedge"

    async with async_playwright() as playwright:
        # launch_persistent_context: gerçek bir kullanıcı profili gibi davranır;
        # tam ekran açılır, otomasyon izini azaltır, girişi hatırlar.
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel=channel,
            headless=False,
            locale="tr-TR",
            no_viewport=True,               # pencereyi normal boyutta bırak
            args=["--start-maximized"],     # tam ekran, normal tarayıcı hissi
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(LOGIN_URL)

        print("\n=== Açılan Edge penceresinde X hesabına giriş yapın (2FA dahil). ===")
        # asyncio içinde bloklayan input'u ayrı thread'de bekle.
        await asyncio.get_event_loop().run_in_executor(
            None, input, "Giriş tamamlandıysa buraya ENTER'a basın..."
        )

        await context.storage_state(path=str(session_path))
        await context.close()

    encoded = base64.b64encode(session_path.read_bytes()).decode("ascii")
    # Base64'ü ayrı bir dosyaya da yaz — terminalden kopyalamak yerine dosyadan
    # kopyalamak daha kolay. Bu dosya .gitignore ile korunur, ASLA commit edilmez.
    b64_path = OUTPUT_DIR / f"{profile}.b64.txt"
    b64_path.write_text(encoded, encoding="ascii")

    print(f"\nOturum kaydedildi: {session_path}")
    print(f"GitHub Secret adı: SESSION_B64_{profile.upper()}")
    print(f"Base64 değeri şu dosyada (aç, tümünü kopyala, secret'a yapıştır):\n  {b64_path}\n")
    print("Terminalden kopyalamak istersen değer aşağıda:\n")
    print(encoded)


def main() -> None:
    """CLI: --profile argümanını alır ve oturum yakalamayı başlatır."""
    load_dotenv()  # .env'deki PLAYWRIGHT_CHANNEL vb. değerleri yükle
    parser = argparse.ArgumentParser(description="X oturum çerezi üretici")
    parser.add_argument("--profile", required=True, help="Profil adı (ör. haber)")
    args = parser.parse_args()
    asyncio.run(capture_session(args.profile))


if __name__ == "__main__":
    main()
