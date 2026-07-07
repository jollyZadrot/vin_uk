"""
chipex_client.py

Клієнт для chipex.co.uk з використанням Playwright (реальний браузер).
Обходить бот-захист завдяки виконанню JavaScript та вірному fingerprint.
"""

import re
import json
import asyncio
import logging
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Конфігурація
# ----------------------------------------------------------------------
WORDPRESS_API_URL = "https://chipex.co.uk/wp-json/lookup/v2/reg/{reg_number}"
PRODUCT_URL = "https://chipex.co.uk/product/your-registration-touch-up-kit/?reg={reg_number}"
HOMEPAGE_URL = "https://chipex.co.uk/"

REG_JSON_PATTERN = re.compile(r"reg_json:\s*'({.*?})'", re.DOTALL)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ----------------------------------------------------------------------
# Винятки
# ----------------------------------------------------------------------
class ChipexLookupError(Exception):
    """Базовий виняток."""
    def __init__(self, message: str, status_code: int = None, details: dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details or {}


class ChipexAuthError(ChipexLookupError):
    """401/403 — бот-захист."""
    pass


class ChipexNotFoundError(ChipexLookupError):
    """404 — номер не знайдено."""
    pass


class ChipexNetworkError(ChipexLookupError):
    """Мережеві проблеми."""
    pass


# ----------------------------------------------------------------------
# Модель даних
# ----------------------------------------------------------------------
@dataclass
class VehicleInfo:
    reg: str
    manufacturer: str
    model: str
    colour: str
    fuel: str
    year: str
    vin: str

    @classmethod
    def from_dict(cls, data: dict) -> "VehicleInfo":
        return cls(
            reg=str(data.get("reg", "")),
            manufacturer=str(data.get("manufacturer", data.get("make", ""))),
            model=str(data.get("model", "")),
            colour=str(data.get("colour", data.get("color", ""))),
            fuel=str(data.get("fuel", "")),
            year=str(data.get("year", "")),
            vin=str(data.get("vin", "")),
        )

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------------
# Playwright методи
# ----------------------------------------------------------------------
async def _create_browser():
    """Створює headless Chromium з антидетект налаштуваннями."""
    from playwright.async_api import async_playwright

    p = await async_playwright().start()
    browser = await p.chromium.launch(
        headless=True,
        args=[
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-blink-features=AutomationControlled',
            '--disable-dev-shm-usage',
        ]
    )
    return p, browser


async def _create_context(browser):
    """Створює context з реалістичним fingerprint."""
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={'width': 1920, 'height': 1080},
        locale='en-GB',
        timezone_id='Europe/London',
        extra_http_headers={
            'Accept-Language': 'en-GB,en;q=0.9',
        }
    )
    # Прибираємо ознаки автоматизації
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-GB', 'en'] });
        window.chrome = { runtime: {} };
    """)
    return context


async def _warmup(page):
    """Відкриває головну сторінку, щоб отримати cookies та пройти початкові перевірки."""
    logger.info("Warming up: visiting homepage...")
    try:
        await page.goto(HOMEPAGE_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        logger.info("Warmup complete")
    except Exception as e:
        logger.warning(f"Warmup failed (continuing anyway): {e}")


async def fetch_via_api_async(reg_number: str) -> VehicleInfo:
    """Отримує дані через WordPress REST API."""
    reg_number = reg_number.strip().upper()
    if not reg_number:
        raise ChipexLookupError("Registration number cannot be empty")

    url = WORDPRESS_API_URL.format(reg_number=reg_number)
    logger.info(f"[API] GET {url}")

    p, browser = await _create_browser()
    try:
        context = await _create_context(browser)
        page = await context.new_page()

        await _warmup(page)

        logger.info(f"[API] Fetching: {url}")
        response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        if response is None:
            raise ChipexLookupError("No response from page.goto")

        status = response.status
        logger.info(f"[API] Response status: {status}")

        if status in (401, 403):
            body = await page.content()
            raise ChipexAuthError(
                f"Access denied ({status}). Bot protection active.",
                status_code=status,
                details={"html_snippet": body[:300]},
            )

        if status == 404:
            raise ChipexNotFoundError(
                f"Registration '{reg_number}' not found",
                status_code=status,
            )

        if status != 200:
            body = await page.content()
            raise ChipexLookupError(
                f"Unexpected status {status}",
                status_code=status,
                details={"html_snippet": body[:300]},
            )

        # Парсимо JSON
        body_text = await page.evaluate("() => document.body.innerText")
        logger.info(f"[API] Body: {body_text[:200]}")

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise ChipexLookupError(
                f"Invalid JSON: {exc}",
                status_code=200,
                details={"body": body_text[:300]},
            ) from exc

        # Обробка структури
        if isinstance(data, dict):
            if "data" in data and "success" in data:
                if not data.get("success"):
                    raise ChipexNotFoundError(
                        data.get("message", "API returned success=false"),
                        status_code=200,
                    )
                vehicle_data = data["data"]
            else:
                vehicle_data = data
        else:
            raise ChipexLookupError("Response is not a dictionary")

        if not isinstance(vehicle_data, dict) or not vehicle_data.get("reg"):
            raise ChipexNotFoundError("No vehicle data in response")

        return VehicleInfo.from_dict(vehicle_data)

    finally:
        await browser.close()
        await p.stop()


async def fetch_via_html_async(reg_number: str) -> VehicleInfo:
    """Fallback: парсинг HTML."""
    reg_number = reg_number.strip().upper()
    if not reg_number:
        raise ChipexLookupError("Registration number cannot be empty")

    url = PRODUCT_URL.format(reg_number=reg_number)
    logger.info(f"[HTML] GET {url}")

    p, browser = await _create_browser()
    try:
        context = await _create_context(browser)
        page = await context.new_page()

        await _warmup(page)

        response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        if response is None or response.status != 200:
            status = response.status if response else None
            raise ChipexLookupError(
                f"Failed to load page (status: {status})",
                status_code=status,
            )

        html = await page.content()

        # Шукаємо reg_json
        match = REG_JSON_PATTERN.search(html)
        if not match:
            raise ChipexNotFoundError(
                f"reg_json not found for '{reg_number}'",
                status_code=200,
                details={"html_length": len(html)},
            )

        data = json.loads(match.group(1))
        return VehicleInfo.from_dict(data)

    finally:
        await browser.close()
        await p.stop()


def lookup_vehicle(reg_number: str) -> VehicleInfo:
    """Синхронна функція: API -> HTML fallback."""
    reg_number = reg_number.strip().upper()
    if not reg_number:
        raise ChipexLookupError("Registration number cannot be empty")

    # Спроба 1: API
    try:
        logger.info(f"Attempting API for '{reg_number}'")
        return asyncio.run(fetch_via_api_async(reg_number))
    except ChipexLookupError as exc:
        logger.warning(f"API failed: {exc}")

    # Спроба 2: HTML
    try:
        logger.info(f"Attempting HTML for '{reg_number}'")
        return asyncio.run(fetch_via_html_async(reg_number))
    except ChipexLookupError as exc:
        logger.warning(f"HTML failed: {exc}")
        raise


# ----------------------------------------------------------------------
# Діагностика
# ----------------------------------------------------------------------
async def diagnose_async() -> Dict[str, Any]:
    """Запускає діагностику."""
    import time
    results = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "test_registration": "E366SJW",
        "tests": {},
    }

    # Test API
    try:
        result = await fetch_via_api_async("E366SJW")
        results["tests"]["api"] = {"status": "success", "data": result.to_dict()}
    except ChipexLookupError as exc:
        results["tests"]["api"] = {
            "status": "failed",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "status_code": exc.status_code,
        }

    # Test HTML
    try:
        result = await fetch_via_html_async("E366SJW")
        results["tests"]["html"] = {"status": "success", "data": result.to_dict()}
    except ChipexLookupError as exc:
        results["tests"]["html"] = {
            "status": "failed",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "status_code": exc.status_code,
        }

    return results


def diagnose() -> Dict[str, Any]:
    """Синхронна обгортка для діагностики."""
    return asyncio.run(diagnose_async())


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) > 1 and sys.argv[1] == "--diagnose":
        print(json.dumps(diagnose(), indent=2))
    elif len(sys.argv) > 1:
        try:
            print(json.dumps(lookup_vehicle(sys.argv[1]).to_dict(), indent=2))
        except ChipexLookupError as e:
            print(f"Error: {e}")
    else:
        print("Usage: python chipex_client.py <REG> | --diagnose")
