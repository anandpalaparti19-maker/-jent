"""
Unstop scraper for JENT.

Strategy (in order):
  1. Direct REST API call (public endpoint — no auth needed for browsing)
  2. XHR interception from browser
  3. JS evaluation on live DOM
"""

import asyncio
import json
import logging
import re
import warnings
import urllib.request
import urllib.parse
from typing import List

warnings.filterwarnings("ignore", category=ResourceWarning)

log = logging.getLogger(__name__)

BASE_URL = "https://unstop.com"
LOGIN_URL = f"{BASE_URL}/auth/login"
LOGIN_CHECK_URL = f"{BASE_URL}/dashboard"
LOGGED_IN_INDICATOR = "dashboard"

# Public REST API (works without login for browsing)
_API_BASE = "https://unstop.com/api/public/opportunity/search-result"

INTERCEPT_PATTERNS = [
    "unstop.com/api/public/opportunity",
    "unstop.com/api/opportunity",
    "unstop.com/api/",
]


def _clean(s) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip()


def _parse_opportunity(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None

    opp_type = _clean(item.get("type") or item.get("opportunity_type") or "internship")

    title = _clean(
        item.get("title") or item.get("opportunity_title") or item.get("name") or ""
    )
    if not title:
        return None

    org = _clean(
        (item.get("organisation_details") or {}).get("org_name") or
        item.get("organisation_name") or
        (item.get("organisation") or {}).get("name") or ""
    )

    # URL
    slug = item.get("public_url") or item.get("slug") or ""
    url = (
        f"{BASE_URL}/{slug}" if slug and not slug.startswith("http")
        else slug or f"{BASE_URL}/opportunities"
    )

    # Location
    locs = item.get("locations", [])
    if isinstance(locs, list):
        location = ", ".join(
            _clean(l.get("city") or l.get("name") or l) if isinstance(l, dict) else _clean(l)
            for l in locs[:3]
        )
    else:
        location = _clean(str(locs)) if locs else ""
    if item.get("work_from_home") or item.get("is_remote"):
        location = "Remote" if not location else f"Remote / {location}"

    stipend = _clean(item.get("stipend") or item.get("salary") or "")
    deadline = _clean(item.get("end_date") or item.get("deadline") or "")
    posted_at = _clean(item.get("start_date") or item.get("published_at") or "")
    raw_desc = item.get("about") or item.get("description") or ""
    clean_desc = _clean(re.sub(r"<[^>]+>", " ", str(raw_desc)))[:600]

    desc_parts = [f"{opp_type.title()}: {title} at {org}."]
    if location:
        desc_parts.append(f"Location: {location}.")
    if stipend:
        desc_parts.append(f"Stipend/Salary: {stipend}.")
    if deadline:
        desc_parts.append(f"Apply by: {deadline}.")
    if clean_desc:
        desc_parts.append(clean_desc)

    opp_id = str(item.get("id") or item.get("opportunity_id") or slug or title)

    return {
        "id": f"unstop_{opp_id}",
        "title": title,
        "company": org,
        "url": url,
        "location": location,
        "description": " ".join(desc_parts)[:3000],
        "source": "Unstop",
        "posted_at": posted_at,
    }


def _parse_response(data) -> List[dict]:
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        # Try nested shapes Unstop uses
        inner = data.get("data", data)
        if isinstance(inner, dict):
            items = (
                inner.get("data") or
                inner.get("opportunities") or
                inner.get("results") or
                list(inner.values())[:200]
            )
            if isinstance(items, dict):
                items = list(items.values())
        elif isinstance(inner, list):
            items = inner
        else:
            items = []
    else:
        items = []

    jobs = []
    for item in (items or []):
        j = _parse_opportunity(item)
        if j:
            jobs.append(j)
    return jobs


def _api_fetch_direct(opportunity_type: str, per_page: int = 50) -> List[dict]:
    """Hit Unstop's public REST API directly (no browser needed)."""
    params = urllib.parse.urlencode({
        "opportunity": opportunity_type,
        "per_page": per_page,
        "oppstatus": "open",
        "page": 1,
    })
    url = f"{_API_BASE}?{params}"
    try:
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/json, text/plain, */*")
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        req.add_header("Referer", f"{BASE_URL}/opportunities/jobs-and-internships")
        req.add_header("Origin", BASE_URL)
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        return _parse_response(data)
    except Exception as e:
        log.debug(f"[Unstop] Direct API ({opportunity_type}) error: {e}")
        return []


# JS to extract opportunity cards from the Unstop Angular SPA
_JS_EXTRACT = """
() => {
  const results = [];
  const cards = document.querySelectorAll(
    '[class*="opportunity"], [class*="card"], app-opportunity-card, ' +
    '[class*="listing-item"], [class*="OppCard"]'
  );
  cards.forEach(card => {
    const titleEl = card.querySelector('h2, h3, [class*="title"], [class*="heading"]');
    const compEl  = card.querySelector('[class*="org"], [class*="company"], [class*="sponsor"]');
    const linkEl  = card.querySelector('a[href*="/p/"], a[href*="/programs/"], a[href]');
    const title   = (titleEl?.innerText || '').trim().split('\\n')[0];
    const company = (compEl?.innerText  || '').trim().split('\\n')[0];
    const url     = linkEl?.href || '';
    if (title && url && url.includes('unstop.com')) results.push({ title, company, url });
  });
  return results;
}
"""


async def _fetch(include_hackathons: bool, headless: bool) -> List[dict]:
    from scrapers.browser_base import get_context, has_saved_session, is_logged_in, wait_for_manual_login

    jobs: List[dict] = []
    seen_ids: set = set()

    # --- Strategy 1: Direct public REST API (fastest, no browser needed) ---
    log.debug("[Unstop] Trying public REST API...")
    for opp_type in (["jobs", "internships"] + (["hackathons"] if include_hackathons else [])):
        results = _api_fetch_direct(opp_type)
        for j in results:
            if j["id"] not in seen_ids:
                seen_ids.add(j["id"])
                jobs.append(j)
        log.debug(f"[Unstop] REST API ({opp_type}): {len(results)} items")

    if jobs:
        log.info(f"[Unstop] Collected {len(jobs)} listings (REST API)")
        return jobs

    # --- Strategy 2: Browser with XHR interception + JS eval ---
    log.info("[Unstop] REST API returned 0 — trying browser fallback...")

    force_headed = not has_saved_session("unstop")
    effective_headless = False if force_headed else headless

    pw, context = await get_context("unstop", headless=effective_headless)
    try:
        page = await context.new_page()

        logged_in = False if force_headed else await is_logged_in(page, LOGIN_CHECK_URL, LOGGED_IN_INDICATOR)
        if not logged_in:
            if not force_headed:
                await page.close()
                await context.close()
                await pw.stop()
                pw, context = await get_context("unstop", headless=False)
                page = await context.new_page()
            ok = await wait_for_manual_login(
                page, LOGIN_URL, LOGGED_IN_INDICATOR,
                "Unstop", timeout_seconds=120
            )
            if not ok:
                return []

        collected = []

        async def handle_response(response):
            if not any(p in response.url for p in INTERCEPT_PATTERNS):
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                body = await response.json()
                parsed = _parse_response(body)
                for j in parsed:
                    if j["id"] not in seen_ids:
                        seen_ids.add(j["id"])
                        collected.append(j)
                if parsed:
                    log.debug(f"[Unstop] XHR: +{len(parsed)} from {response.url[:70]}")
            except Exception:
                pass

        page.on("response", handle_response)

        pages_to_visit = ["https://unstop.com/opportunities/jobs-and-internships"]
        if include_hackathons:
            pages_to_visit += [
                "https://unstop.com/opportunities/hackathons",
                "https://unstop.com/opportunities/competitions",
            ]

        for url in pages_to_visit:
            try:
                log.info(f"[Unstop] Visiting {url}")
                await page.goto(url, wait_until="networkidle", timeout=40000)
                await asyncio.sleep(5)
                for _ in range(5):
                    await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
                    await asyncio.sleep(1)
            except Exception as e:
                log.debug(f"[Unstop] Nav error ({url}): {e}")

        # JS eval fallback
        if not collected:
            log.info("[Unstop] XHR got 0 — trying JS eval on DOM...")
            for url in pages_to_visit[:1]:
                try:
                    await page.goto(url, wait_until="networkidle", timeout=40000)
                    await asyncio.sleep(4)
                    cards = await page.evaluate(_JS_EXTRACT)
                    for card in (cards or []):
                        href = card.get("url", "")
                        uid = f"unstop_js_{hash(href)}"
                        if href and uid not in seen_ids:
                            seen_ids.add(uid)
                            collected.append({
                                "id": uid,
                                "title": card.get("title", ""),
                                "company": card.get("company", ""),
                                "url": href,
                                "location": "",
                                "description": f"{card.get('title', '')} at {card.get('company', '')} — Unstop",
                                "source": "Unstop",
                                "posted_at": "",
                            })
                    log.debug(f"[Unstop] JS eval: {len(cards or [])} cards")
                except Exception as e:
                    log.debug(f"[Unstop] JS eval error: {e}")

        jobs.extend(collected)
        log.info(f"[Unstop] Collected {len(jobs)} listings")

    except Exception as e:
        log.warning(f"Unstop scraper error: {e}")
    finally:
        try:
            await context.close()
            await pw.stop()
        except Exception:
            pass

    return jobs


def fetch_unstop(include_hackathons: bool = True, headless: bool = True) -> List[dict]:
    """Synchronous entry point called from job_search_agent.py."""
    try:
        return asyncio.run(_fetch(include_hackathons, headless))
    except Exception as e:
        log.warning(f"Unstop fetch failed: {e}")
        return []
