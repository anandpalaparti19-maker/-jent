"""
Shared Playwright browser engine for JENT browser scrapers.

Manages:
- A persistent browser profile per platform (saves cookies/session)
- First-run headed login flow (you log in once manually)
- Headless reuse on all subsequent runs
- API response interception helper
"""
import json
import asyncio
from pathlib import Path
from typing import Callable, List, Optional

try:
    from playwright.async_api import async_playwright, BrowserContext, Page, Route
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

BASE_DIR = Path(__file__).parent.parent
BROWSER_DATA_DIR = BASE_DIR / "browser_data"


def check_playwright() -> bool:
    """Return True if playwright is installed and usable."""
    return PLAYWRIGHT_AVAILABLE


async def get_context(platform: str, headless: bool = True) -> tuple:
    """
    Return (playwright_instance, browser, context) for the given platform.
    Uses a persistent profile directory so sessions survive between runs.
    Caller is responsible for closing all three.
    """
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("playwright not installed — run: pip install playwright && playwright install chromium")

    profile_dir = BROWSER_DATA_DIR / platform
    profile_dir.mkdir(parents=True, exist_ok=True)

    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
        viewport={"width": 1280, "height": 800},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        ignore_https_errors=True,
    )
    # Remove navigator.webdriver fingerprint
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
        window.chrome = { runtime: {} };
    """)
    return pw, context


def has_saved_session(platform: str) -> bool:
    """Return True only if a real browser session (cookies) exists for this platform."""
    profile_dir = BROWSER_DATA_DIR / platform / "Default"
    # Modern Chromium stores cookies in Default/Network/Cookies
    # Older versions use Default/Cookies directly
    candidates = [
        profile_dir / "Network" / "Cookies",
        profile_dir / "Cookies",
        profile_dir / "Login Data",
    ]
    for path in candidates:
        if path.exists() and path.stat().st_size > 4096:
            return True
    return False


async def is_logged_in(page: Page, check_url: str, logged_in_indicator: str) -> bool:
    """
    Navigate to check_url and verify login by:
    1. Checking the final URL is NOT a login/auth/signin page (redirect check)
    2. Checking logged_in_indicator appears in page content
    Both must pass.
    """
    try:
        await page.goto(check_url, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)
        final_url = page.url.lower()
        # If redirected to a login/auth page — definitely not logged in
        if any(kw in final_url for kw in ("login", "signin", "sign-in", "auth", "register", "signup")):
            return False
        content = await page.content()
        return logged_in_indicator in content
    except Exception:
        return False


async def wait_for_manual_login(page: Page, login_url: str, success_indicator: str,
                                 platform_name: str, timeout_seconds: int = 120):
    """
    Open login_url in a headed browser and wait for the user to log in manually.
    Detects success by checking for success_indicator in page content.
    """
    print(f"\n[{platform_name}] Opening browser for manual login...")
    print(f"[{platform_name}] Please log in within {timeout_seconds} seconds.")
    print(f"[{platform_name}] The window will close automatically once login is detected.\n")
    await page.goto(login_url, wait_until="domcontentloaded", timeout=20000)

    for _ in range(timeout_seconds):
        await asyncio.sleep(1)
        try:
            content = await page.content()
            if success_indicator in content:
                print(f"[{platform_name}] Login detected! Session saved.")
                return True
        except Exception:
            pass
    print(f"[{platform_name}] Login timeout — skipping this source.")
    return False


async def intercept_and_collect(
    page: Page,
    trigger_url: str,
    intercept_pattern: str,
    parse_fn: Callable,
    wait_ms: int = 8000,
    extra_setup: Optional[Callable] = None,
) -> List[dict]:
    """
    Navigate to trigger_url, intercept all responses matching intercept_pattern,
    call parse_fn(response_json) on each, and collect results.
    """
    collected = []

    async def handle_response(response):
        if intercept_pattern in response.url:
            try:
                body = await response.json()
                results = parse_fn(body)
                if results:
                    collected.extend(results)
            except Exception:
                pass

    page.on("response", handle_response)

    if extra_setup:
        await extra_setup(page)

    await page.goto(trigger_url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(wait_ms / 1000)

    return collected
