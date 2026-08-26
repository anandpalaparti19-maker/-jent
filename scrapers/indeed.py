"""
Indeed scraper for JENT.

Strategy:
  1. Check if logged in via persistent Playwright session.
  2. If not, open headed browser for manual login.
  3. Navigate to search results for resume keywords.
  4. Intercept the internal mosaic-provider-reportingts / jobcards API calls
     that Indeed's SPA fires — these return clean structured JSON.
  5. Normalize and return JENT-compatible job dicts.

Indeed search URL format:
  https://in.indeed.com/jobs?q=<query>&l=<location>&sort=date&fromage=14
"""

import asyncio
import logging
import re
import urllib.parse
import warnings
from typing import List

warnings.filterwarnings("ignore", category=ResourceWarning)

log = logging.getLogger(__name__)

INDEED_DOMAIN = "https://in.indeed.com"   # India domain
LOGIN_URL = f"{INDEED_DOMAIN}/account/login"
LOGIN_CHECK_URL = f"{INDEED_DOMAIN}/"
# Indeed's home page varies — we detect login by URL only (no redirect to /account/login)
LOGGED_IN_INDICATOR = ""   # empty = URL-check only

# Broad patterns — catch any Indeed API that returns job data
INTERCEPT_PATTERNS = [
    "indeed.com/rpc/jobcards",
    "indeed.com/rpc/",
    "indeed.com/api/jobs",
    "mosaic/svc",
    "mosaic-provider",
    "indeed.com/jobs",
    "indeed.com/graphql",
    "indeed.com/m/jobs",
    "jobcards",
    "jobalert",
    "viewjob",
    "/rpc/",
]


def _make_search_url(query: str, location: str, days: int = 14) -> str:
    params = urllib.parse.urlencode({
        "q": query,
        "l": location,
        "sort": "date",
        "fromage": str(days),
        "radius": "50",
    })
    return f"{INDEED_DOMAIN}/jobs?{params}"


def _parse_jobcards(data: dict) -> List[dict]:
    """Parse Indeed job card API response."""
    jobs = []

    # Try multiple response shapes Indeed uses
    results = (
        data.get("results", [])
        or data.get("metaData", {}).get("mosaicProviderJobCardsModel", {}).get("results", [])
        or data.get("jobcards", [])
        or []
    )

    for item in results:
        if not isinstance(item, dict):
            continue

        # Flatten nested structures
        title = (
            item.get("title")
            or item.get("jobTitle")
            or item.get("displayTitle", "")
        )
        company = (
            item.get("company")
            or item.get("companyName")
            or item.get("companyBrandingAttributes", {}).get("headerImageAlt", "")
            or ""
        )
        location = (
            item.get("formattedLocation")
            or item.get("location")
            or item.get("jobLocationCity", "")
            or ""
        )
        job_key = item.get("jobkey", item.get("jobKey", item.get("id", "")))
        url = (
            item.get("link")
            or item.get("viewJobLink")
            or (f"{INDEED_DOMAIN}/viewjob?jk={job_key}" if job_key else "")
        )
        salary = item.get("extractedSalary", {})
        salary_str = ""
        if isinstance(salary, dict) and salary:
            mn = salary.get("min", "")
            mx = salary.get("max", "")
            typ = salary.get("type", "")
            salary_str = f"{mn}-{mx} {typ}".strip(" -")

        snippet = (
            item.get("snippet")
            or item.get("jobDescription", {}).get("text", "")
            or ""
        )
        # Strip HTML tags from snippet
        import re
        snippet = re.sub(r"<[^>]+>", " ", snippet)

        description = (
            f"{title} at {company}. "
            f"Location: {location}. "
            f"{'Salary: ' + salary_str + '. ' if salary_str else ''}"
            f"{snippet}"
        )

        if not title or not job_key:
            continue

        jobs.append({
            "id": f"indeed_{job_key}",
            "title": title,
            "company": company,
            "url": url,
            "location": location,
            "description": description[:3000],
            "source": "Indeed",
            "posted_at": item.get("pubDate", item.get("formattedRelativeTime", "")),
        })

    return jobs


def _parse_any_indeed_response(data: dict) -> List[dict]:
    """Try all known Indeed response shapes."""
    jobs = _parse_jobcards(data)
    if not jobs:
        # Try flat array of jobs
        items = data if isinstance(data, list) else data.get("jobs", [])
        for item in items:
            if isinstance(item, dict) and ("title" in item or "jobTitle" in item):
                jobs.extend(_parse_jobcards({"results": [item]}))
    return jobs


async def _fetch(query: str, location: str, headless: bool) -> List[dict]:
    from scrapers.browser_base import get_context, has_saved_session, wait_for_manual_login

    # Indeed India uses Cloudflare bot-detection that blocks headless Chromium.
    # Always run headed (visible window) to pass the JS challenge.
    INDEED_ALWAYS_HEADED = True
    force_headed = not has_saved_session("indeed")
    effective_headless = False  # Always headed for Indeed

    pw, context = await get_context("indeed", headless=effective_headless)
    jobs = []

    try:
        page = await context.new_page()

        # When session is saved, skip login check and go straight to scraping.
        # Indeed's Cloudflare can block even the home page check, causing false negatives.
        if force_headed:
            logged_in = False
        else:
            # Optimistic: assume session is valid if cookies file exists
            logged_in = True
            log.debug("[Indeed] Session file found — assuming logged in, will verify on first search")

        if not logged_in:
            log.info("[Indeed] Not logged in — launching headed browser for login...")
            ok = await wait_for_manual_login(
                page, LOGIN_URL, "Sign Out",
                "Indeed", timeout_seconds=300
            )
            if not ok:
                return []

        collected = []
        seen_ids: set = set()
        seen_urls: set = set()

        async def handle_response(response):
            url_lower = response.url.lower()
            if not any(p in url_lower for p in INTERCEPT_PATTERNS):
                return
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                return
            try:
                body = await response.json()
                results = _parse_any_indeed_response(body)
                if results:
                    new_results = [j for j in results if j["id"] not in seen_ids]
                    seen_ids.update(j["id"] for j in new_results)
                    collected.extend(new_results)
                    log.debug(f"[Indeed] Intercepted {len(new_results)} jobs from {response.url[:80]}")
            except Exception as e:
                log.debug(f"[Indeed] Parse error on {response.url[:60]}: {e}")

        page.on("response", handle_response)

        # --- Strategy 0: Pure requests with realistic headers (fastest, no Playwright needed) ---
        # Try this first before opening any browser pages — works when Cloudflare is not active.
        try:
            import urllib.request as _req
            import re as _re

            search_queries_s0 = [query]
            if query != "software intern":
                search_queries_s0.append("software intern")
            if "data" not in query.lower():
                search_queries_s0.append("data science intern")

            # Get any cookies already in the browser context (may be empty on first run)
            cookies = await context.cookies()
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("value"))

            for q in search_queries_s0[:3]:
                s_url = _make_search_url(q, location)
                r = _req.Request(s_url)
                if cookie_str:
                    r.add_header("Cookie", cookie_str)
                r.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
                r.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8")
                r.add_header("Accept-Language", "en-IN,en;q=0.9,hi;q=0.8")
                r.add_header("Referer", INDEED_DOMAIN + "/")
                r.add_header("sec-fetch-dest", "document")
                r.add_header("sec-fetch-mode", "navigate")
                r.add_header("sec-ch-ua", '"Chromium";v="124", "Google Chrome";v="124"')
                r.add_header("sec-ch-ua-platform", '"Windows"')
                try:
                    with _req.urlopen(r, timeout=20) as resp:
                        html = resp.read().decode("utf-8", errors="ignore")
                    log.debug(f"[Indeed] Strategy-0 HTML: {len(html)} chars for {q!r}")
                    # Match both lowercase hex AND mixed-case alphanumeric job keys
                    jks = list(dict.fromkeys(_re.findall(r'"jobkey"\s*:\s*"([a-zA-Z0-9]{8,20})"', html)))
                    if not jks:
                        jks = list(dict.fromkeys(_re.findall(r'jk=([a-zA-Z0-9]{8,20})', html)))
                    log.debug(f"[Indeed] Strategy-0 found {len(jks)} job keys for {q!r}")
                    # Try to extract title/company from embedded JSON
                    title_map = {}
                    company_map = {}
                    for m in _re.finditer(
                        r'"jobkey"\s*:\s*"([a-zA-Z0-9]{8,20})".*?"title"\s*:\s*"([^"]+)".*?"company"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"',
                        html, _re.DOTALL
                    ):
                        title_map[m.group(1)] = m.group(2)
                        company_map[m.group(1)] = m.group(3)
                    for jk in jks:
                        uid = f"indeed_{jk}"
                        if uid in seen_ids:
                            continue
                        seen_ids.add(uid)
                        href = f"{INDEED_DOMAIN}/viewjob?jk={jk}"
                        title = title_map.get(jk, f"Job on Indeed ({jk[:8]})")
                        company = company_map.get(jk, "")
                        collected.append({
                            "id": uid,
                            "title": title,
                            "company": company,
                            "url": href,
                            "location": location,
                            "description": f"{title} at {company} — Indeed India ({q})",
                            "source": "Indeed",
                            "posted_at": "",
                        })
                except Exception as e:
                    log.debug(f"[Indeed] Strategy-0 requests error for {q!r}: {e}")

            if collected:
                log.info(f"[Indeed] Strategy-0 (requests): {len(collected)} listings")
        except Exception as e:
            log.debug(f"[Indeed] Strategy-0 setup error: {e}")

        # Search queries (browser XHR interception)
        search_queries = [query]
        if query != "software intern":
            search_queries.append("software intern")
        if "data" not in query.lower():
            search_queries.append("data science intern")

        for q in search_queries:
            search_url = _make_search_url(q, location)
            log.info(f"[Indeed] Searching: {q!r} in {location!r}")
            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=40000)
                # Wait up to 30s for Cloudflare JS challenge to resolve
                try:
                    await page.wait_for_function(
                        "() => document.title !== 'Just a moment...' && document.title !== ''",
                        timeout=30000
                    )
                except Exception:
                    pass
                title = await page.title()
                log.debug(f"[Indeed] Search page title: {title!r}")
                # If still blocked, skip this query
                if "just a moment" in title.lower():
                    log.warning(f"[Indeed] Cloudflare blocked search for {q!r} — skipping")
                    continue
                await asyncio.sleep(5)
                for _ in range(5):
                    await page.evaluate("window.scrollBy(0, window.innerHeight)")
                    await asyncio.sleep(1.2)
                try:
                    next_btn = await page.query_selector('[data-testid="pagination-page-next"], a[aria-label="Next"]')
                    if next_btn:
                        await next_btn.click()
                        await asyncio.sleep(3)
                        for _ in range(3):
                            await page.evaluate("window.scrollBy(0, window.innerHeight)")
                            await asyncio.sleep(1)
                except Exception:
                    pass
            except Exception as e:
                log.warning(f"[Indeed] Navigation error for query {q!r}: {e}")

        # DOM / HTML fallback — parse page.content() with regex if XHR got nothing
        if not collected:
            log.info("[Indeed] XHR interception got 0 — trying HTML regex fallback...")
            import re as _re
            for q in search_queries[:2]:  # limit to 2 queries for speed
                try:
                    await page.goto(_make_search_url(q, location), wait_until="domcontentloaded", timeout=40000)
                    # Wait for Cloudflare to pass
                    try:
                        await page.wait_for_function(
                            "() => document.title !== 'Just a moment...' && document.title !== ''",
                            timeout=25000
                        )
                    except Exception:
                        pass
                    title_pg = await page.title()
                    if "just a moment" in title_pg.lower():
                        log.warning(f"[Indeed] Cloudflare still active for {q!r}, skipping")
                        continue
                    await asyncio.sleep(4)
                    html = await page.content()
                    log.debug(f"[Indeed] HTML regex: {len(html)} chars, title={title_pg!r}")

                    # Extract job keys from URLs: jk=XXXXXXXXXXXXXXXX
                    # Match both lowercase hex (old format) and mixed alphanumeric (new format)
                    jks = list(dict.fromkeys(
                        _re.findall(r'"jobkey"\s*:\s*"([a-zA-Z0-9]{8,20})"', html)
                        or _re.findall(r'jk=([a-zA-Z0-9]{8,20})', html)
                    ))
                    log.debug(f"[Indeed] Found {len(jks)} job keys in HTML")

                    # Extract titles: try embedded JSON first, then HTML proximity
                    title_map = {}
                    company_map = {}
                    for m in _re.finditer(
                        r'"jobkey"\s*:\s*"([a-zA-Z0-9]{8,20})".*?"title"\s*:\s*"([^"]+)".*?"company"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"',
                        html, _re.DOTALL
                    ):
                        title_map[m.group(1)] = m.group(2)
                        company_map[m.group(1)] = m.group(3)
                    # Fallback: data-jk HTML attribute
                    for m in _re.finditer(
                        r'data-jk="([a-zA-Z0-9]{8,20})"[^>]*>.*?<h2[^>]*><[^>]+>([^<]+)<',
                        html, _re.DOTALL
                    ):
                        if m.group(1) not in title_map:
                            title_map[m.group(1)] = m.group(2).strip()

                    for jk in jks:
                        uid = f"indeed_{jk}"
                        if uid in seen_ids:
                            continue
                        seen_ids.add(uid)
                        href = f"{INDEED_DOMAIN}/viewjob?jk={jk}"
                        title = title_map.get(jk, f"Job on Indeed ({jk[:8]})")
                        company = company_map.get(jk, "")
                        collected.append({
                            "id": uid,
                            "title": title,
                            "company": company,
                            "url": href,
                            "location": location,
                            "description": f"{title} at {company} — Indeed India",
                            "source": "Indeed",
                            "posted_at": "",
                        })
                except Exception as e:
                    log.debug(f"[Indeed] HTML regex fallback error: {e}")

        jobs.extend(collected)
        log.info(f"[Indeed] Collected {len(jobs)} listings")

    except Exception as e:
        log.warning(f"Indeed scraper error: {e}")
    finally:
        try:
            await context.close()
            await pw.stop()
        except Exception:
            pass

    return jobs


def fetch_indeed(
    query: str = "software engineer intern",
    location: str = "India",
    headless: bool = True,
) -> List[dict]:
    """Synchronous entry point called from job_search_agent.py."""
    try:
        return asyncio.run(_fetch(query, location, headless))
    except Exception as e:
        log.warning(f"Indeed fetch failed: {e}")
        return []
