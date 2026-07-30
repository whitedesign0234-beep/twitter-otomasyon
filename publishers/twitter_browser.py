# publishers/twitter_browser.py
"""Playwright ile X (Twitter) web arayüzünden paylaşım yapan yayıncı.

Şifre kullanmaz; her profilin kendi storage_state JSON'u ile izole bir
browser_context açar. Selektörler kırılgan olduğundan her adımda çoklu
fallback ve açık timeout kullanılır; hata durumunda ekran görüntüsü alınır.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from pathlib import Path

from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from publishers.base import PostResult

logger = logging.getLogger(__name__)

# Sabitler
COMPOSE_URL = "https://x.com/compose/post"
HOME_URL = "https://x.com/home"
NAV_TIMEOUT_MS = 30_000
ACTION_TIMEOUT_MS = 15_000
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
VIEWPORT = {"width": 1280, "height": 900}
SCREENSHOT_DIR = Path("screenshots")

# İnsan benzeri gecikme aralıkları (saniye)
TYPE_DELAY_RANGE = (0.03, 0.12)      # karakter başına
STEP_DELAY_RANGE = (0.6, 1.8)        # adımlar arası

# Çoklu fallback selektörler (X arayüzü sık değişir).
EDITOR_SELECTORS = [
    'div[data-testid="tweetTextarea_0"]',
    'div[role="textbox"][data-testid^="tweetTextarea"]',
    'div[contenteditable="true"][role="textbox"]',
]
POST_BUTTON_SELECTORS = [
    'button[data-testid="tweetButton"]',
    'button[data-testid="tweetButtonInline"]',
    'div[data-testid="tweetButton"]',
]
FILE_INPUT_SELECTOR = 'input[type="file"]'
# Oturumun geçerli olduğunu gösteren işaret (giriş yapılmışsa görünür).
LOGGED_IN_MARKER = 'a[data-testid="AppTabBar_Home_Link"]'


async def _human_pause() -> None:
    """Adımlar arasında rastgele, insan benzeri bir bekleme uygular."""
    await asyncio.sleep(random.uniform(*STEP_DELAY_RANGE))


class TwitterBrowserPublisher:
    """Bir profilin storage_state'ini kullanarak X'e tweet gönderir."""

    def __init__(self, profile_name: str, storage_state_path: Path, headless: bool = True) -> None:
        """Yayıncıyı profil adı ve oturum dosyası ile hazırlar."""
        self.profile_name = profile_name
        self.storage_state_path = storage_state_path
        self.headless = headless

    async def publish_video(self, text: str, video_path: str) -> PostResult:
        """Tarayıcı yönteminde video paylaşımı desteklenmez (API kullanın)."""
        logger.warning("[%s] tarayıcı yayıncısı video desteklemiyor", self.profile_name)
        return PostResult(success=False, detail="video-not-supported-in-browser")

    async def publish(self, text: str, image_path: str | None) -> PostResult:
        """İzole bir tarayıcı bağlamında tweet'i gönderir."""
        if not self.storage_state_path.exists():
            return PostResult(
                success=False,
                detail=f"Oturum dosyası yok: {self.storage_state_path}",
                session_invalid=True,
            )

        async with async_playwright() as playwright:
            # PLAYWRIGHT_CHANNEL ayarlıysa sistemdeki Edge/Chrome kullanılır
            # (ör. Windows'ta "msedge"); boşsa Playwright'ın indirdiği Chromium
            # (CI/Ubuntu için ideal). Böylece bozuk Chromium binary'si aşılır.
            launch_kwargs: dict = {"headless": self.headless}
            channel = os.environ.get("PLAYWRIGHT_CHANNEL", "").strip()
            if channel:
                launch_kwargs["channel"] = channel
            browser = await playwright.chromium.launch(**launch_kwargs)
            try:
                # Her profil için ayrı, izole bağlam — oturumlar birbirini görmez.
                context = await browser.new_context(
                    storage_state=str(self.storage_state_path),
                    user_agent=USER_AGENT,
                    viewport=VIEWPORT,
                    locale="tr-TR",
                )
                context.set_default_timeout(ACTION_TIMEOUT_MS)
                return await self._do_publish(context, text, image_path)
            except PlaywrightTimeoutError as exc:
                return PostResult(success=False, detail=f"Zaman aşımı: {exc}")
            finally:
                await browser.close()

    async def _do_publish(
        self, context: BrowserContext, text: str, image_path: str | None
    ) -> PostResult:
        """Sayfayı açar, oturumu doğrular ve paylaşım akışını yürütür."""
        page = await context.new_page()
        await page.goto(HOME_URL, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")

        if not await self._is_logged_in(page):
            await self._screenshot(page, "session_invalid")
            return PostResult(
                success=False,
                detail="Oturum geçersiz — yeniden giriş gerekli",
                session_invalid=True,
            )

        await _human_pause()
        await page.goto(COMPOSE_URL, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")

        editor = await self._find_first(page, EDITOR_SELECTORS)
        if editor is None:
            await self._screenshot(page, "editor_not_found")
            return PostResult(success=False, detail="Editör alanı bulunamadı (selektör değişmiş olabilir)")

        await editor.click()
        await self._type_like_human(page, text)

        if image_path:
            await self._attach_image(page, image_path)

        return await self._click_post(page)

    async def _is_logged_in(self, page: Page) -> bool:
        """Ana sayfadaki oturum işaretine bakarak girişli olup olmadığını anlar."""
        try:
            await page.wait_for_selector(LOGGED_IN_MARKER, timeout=ACTION_TIMEOUT_MS)
            return True
        except PlaywrightTimeoutError:
            return False

    @staticmethod
    async def _find_first(page: Page, selectors: list[str]):
        """Verilen selektörlerden ilk görünür olanın locator'ını döndürür."""
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=ACTION_TIMEOUT_MS)
                return locator
            except PlaywrightTimeoutError:
                continue
        return None

    async def _type_like_human(self, page: Page, text: str) -> None:
        """Metni karakter karakter, rastgele gecikmelerle yazar."""
        for char in text:
            await page.keyboard.type(char)
            await asyncio.sleep(random.uniform(*TYPE_DELAY_RANGE))

    async def _attach_image(self, page: Page, image_path: str) -> None:
        """Görsel dosyasını gizli file input'a yükler."""
        path = Path(image_path)
        if not path.exists():
            logger.warning("[%s] görsel bulunamadı, metin-only devam: %s", self.profile_name, path)
            return
        try:
            file_input = page.locator(FILE_INPUT_SELECTOR).first
            await file_input.set_input_files(str(path))
            await _human_pause()  # yüklemenin işlenmesini bekle
        except PlaywrightTimeoutError:
            logger.warning("[%s] görsel yüklenemedi, metin-only devam", self.profile_name)

    async def _click_post(self, page: Page) -> PostResult:
        """Paylaş düğmesine basar ve sonucu doğrular."""
        button = await self._find_first(page, POST_BUTTON_SELECTORS)
        if button is None:
            await self._screenshot(page, "post_button_not_found")
            return PostResult(success=False, detail="Paylaş düğmesi bulunamadı")

        await _human_pause()
        await button.click()

        # Editörün temizlenmesi/kaybolması paylaşımın gittiğine işaret eder.
        try:
            await page.wait_for_selector(
                EDITOR_SELECTORS[0], state="detached", timeout=ACTION_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            # Kesin doğrulanamadı; screenshot al ama başarısız sayma riskli olabilir.
            await self._screenshot(page, "post_unconfirmed")
            logger.warning("[%s] paylaşım doğrulanamadı", self.profile_name)
            return PostResult(success=False, detail="Paylaşım doğrulanamadı")

        logger.info("[%s] tweet gönderildi", self.profile_name)
        return PostResult(success=True, detail="posted")

    async def _screenshot(self, page: Page, tag: str) -> None:
        """Hata teşhisi için tam sayfa ekran görüntüsü alır."""
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        target = SCREENSHOT_DIR / f"{self.profile_name}_{tag}.png"
        try:
            await page.screenshot(path=str(target), full_page=True)
            logger.info("[%s] ekran görüntüsü: %s", self.profile_name, target)
        except PlaywrightTimeoutError:
            logger.debug("[%s] ekran görüntüsü alınamadı", self.profile_name)
