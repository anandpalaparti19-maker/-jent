"""
Internshala scraper for JENT.

Strategy (in order):
  1. XHR interception — catches Internshala's AJAX calls when they fire
  2. JS evaluation — runs JavaScript on the live page to extract card data
  3. Requests + cookies — sends direct HTTP API call with browser cookies
"""

import asyncio
import json
import logging
import re
import warnings
from typing import List

warnings.filterwarnings("ignore", category=ResourceWarning)

log = logging.getLogger(__name__)

LOGIN_URL = "https://internshala.com/login"
INTERNSHIP_URL = "https://internshala.com/internships/"
JOBS_URL = "https://internshala.com/jobs/"
LOGIN_CHECK_URL = "https://internshala.com/dashboard"
LOGGED_IN_INDICATOR = "dashboard"


def _clean(s) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip()


def _make_job(item: dict) -> dict | None:
    title = _clean(
        item.get("profile_name") or item.get("profileName") or
        item.get("title") or item.get("job_title") or ""
    )
    if not title:
        return None

    company = _clean(
        item.get("company_name") or item.get("companyName") or
        item.get("employer_name") or ""
    )
    slug = item.get("url_route") or item.get("slug") or item.get("link") or ""
    url = f"https://internshala.com{slug}" if slug and not slug.startswith("http") else slug

    loc_parts = []
    for lk in ["locations", "job_locations"]:
        locs = item.get(lk, [])
        if isinstance(locs, list):
            loc_parts += [_clean(l.get("location", l) if isinstance(l, dict) else l) for l in locs[:3]]
        elif isinstance(locs, str):
            loc_parts.append(locs)
    location = ", ".join(filter(None, loc_parts)) or _clean(item.get("location", ""))
    if item.get("is_work_from_home") or item.get("work_from_home"):
        location = "Work from Home" if not location else f"Work from Home / {location}"

    stipend = _clean(item.get("stipend", item.get("salary", "")))
    raw_desc = item.get("description", item.get("about", ""))
    clean_desc = _clean(re.sub(r"<[^>]+>", " ", str(raw_desc)) if raw_desc else "")

    desc = f"{title} at {company}."
    if location:
        desc += f" Location: {location}."
    if stipend:
        desc += f" Stipend: {stipend}."
    if clean_desc:
        desc += f" {clean_desc[:600]}"

    iid = str(item.get("id", item.get("internship_id", url)))
    prefix = "internshala_job_" if "job" in item.get("type", "").lower() else "internshala_"

    return {
        "id": f"{prefix}{iid}",
        "title": title,
        "company": company,
        "url": url or INTERNSHIP_URL,
        "location": location,
        "description": desc[:3000],
        "source": "Internshala",
        "posted_at": _clean(item.get("start_date", item.get("posted_at", ""))),
    }


def _parse_response(data) -> List[dict]:
    jobs = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = (
            data.get("internships_list", data.get("internshipsList")) or
            data.get("jobs_list", data.get("jobsList")) or
            data.get("data", data.get("results", data.get("opportunities", [])))
        )
        if isinstance(items, dict):
            items = list(items.values())
    else:
        items = []

    for item in (items or []):
        if not isinstance(item, dict):
            continue
        j = _make_job(item)
        if j:
            jobs.append(j)
    return jobs


# JavaScript to extract job card data from Internshala's server-rendered DOM
# Skips heading/SEO containers by requiring a real href to internship/detail
_JS_EXTRACT = """
() => {
  const results = [];
  const seen = new Set();
  // Internshala renders each card as .individual_internship
  // but also has SEO heading containers with id containing 'internship_'
  // so we filter by looking for the actual detail link inside
  document.querySelectorAll('.individual_internship, .individual_job').forEach(card => {
    const linkEl = card.querySelector(
      'a[href*="/internship/detail/"], a[href*="/job/detail/"], a[href*="/job-detail/"]'
    );
    if (!linkEl) return;
    const href = linkEl.href;
    if (seen.has(href)) return;
    seen.add(href);
    const titleEl = card.querySelector('.profile a, .profile_name a, h3 a, .heading_4_5 a, h3');
    const compEl  = card.querySelector('.company_name, .company-name, h4, .link_display_like_text');
    const title   = (titleEl?.innerText || linkEl.innerText || '').trim().split('\\n')[0];
    const company = (compEl?.innerText  || '').trim().split('\\n')[0];
    if (title) results.push({ title, company, url: href });
  });
  return results;
}
"""


async def _fetch(headless: bool) -> List[dict]:
    from scrapers.browser_base import get_context, has_saved_session, is_logged_in, wait_for_manual_login

    force_headed = not has_saved_session("internshala")
    effective_headless = False if force_headed else headless

    pw, context = await get_context("internshala", headless=effective_headless)
    jobs: List[dict] = []

    try:
        page = await context.new_page()

        # Login check: if session exists, verify by URL redirect only (not page content)
        # Internshala headless may get a different page, so we just check no auth redirect
        if force_headed:
            logged_in = False
        elif has_saved_session("internshala"):
            try:
                await page.goto("https://internshala.com/", wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(2)
                final_url = page.url.lower()
                logged_in = not any(kw in final_url for kw in ("login", "signin", "sign-in", "auth", "register"))
                log.debug(f"[Internshala] Session check URL={page.url} -> logged_in={logged_in}")
            except Exception:
                logged_in = False
        else:
            logged_in = False

        if not logged_in:
            if not force_headed:
                await page.close()
                await context.close()
                await pw.stop()
                pw, context = await get_context("internshala", headless=False)
                page = await context.new_page()
            ok = await wait_for_manual_login(
                page, LOGIN_URL, LOGGED_IN_INDICATOR,
                "Internshala", timeout_seconds=120
            )
            if not ok:
                return []

        collected = []
        seen_urls: set = set()

        # --- Strategy 1: XHR interception ---
        async def handle_response(response):
            if "internshala.com" not in response.url:
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                body = await response.json()
                parsed = _parse_response(body)
                for j in parsed:
                    if j["url"] not in seen_urls:
                        seen_urls.add(j["url"])
                        collected.append(j)
                if parsed:
                    log.debug(f"[Internshala] XHR: +{len(parsed)} from {response.url[:70]}")
            except Exception:
                pass

        page.on("response", handle_response)

        for url, label in [(INTERNSHIP_URL, "internships"), (JOBS_URL, "jobs")]:
            try:
                await page.goto(url, wait_until="networkidle", timeout=40000)
                await asyncio.sleep(4)
                for _ in range(6):
                    await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
                    await asyncio.sleep(1)
            except Exception as e:
                log.debug(f"[Internshala] Nav error ({label}): {e}")

        log.info(f"[Internshala] XHR strategy: {len(collected)} listings")

        # --- Strategy 0 (fastest): Direct requests + HTML regex using saved cookies ---
        # Runs in parallel with browser; uses the fact that the session is valid.
        if not collected:
            try:
                import urllib.request as _req
                cookies = await context.cookies()
                cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("value"))

                for page_url in [INTERNSHIP_URL, JOBS_URL]:
                    r = _req.Request(page_url)
                    r.add_header("Cookie", cookie_str)
                    r.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
                    r.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
                    r.add_header("Accept-Language", "en-IN,en;q=0.9")
                    try:
                        with _req.urlopen(r, timeout=20) as resp:
                            html = resp.read().decode("utf-8", errors="ignore")
                        log.debug(f"[Internshala] requests HTML length on {page_url}: {len(html)}")
                        # Extract all /internship/detail/ and /jobs/detail/ links
                        detail_links = list(dict.fromkeys(
                            re.findall(r'href="(/internship/detail/[^"]+)"', html) +
                            re.findall(r'href="(/jobs/detail/[^"]+)"', html)
                        ))
                        log.debug(f"[Internshala] requests found {len(detail_links)} detail links on {page_url}")
                        for slug in detail_links:
                            href = f"https://internshala.com{slug}"
                            if href in seen_urls:
                                continue
                            seen_urls.add(href)
                            parts = re.sub(r'\d+$', '', slug.split("/")[-1]).strip("-")
                            title = parts.replace("-", " ").title()
                            collected.append({
                                "id": f"internshala_req_{hash(href)}",
                                "title": title,
                                "company": "",
                                "url": href,
                                "location": "",
                                "description": f"{title} — Internshala",
                                "source": "Internshala",
                                "posted_at": "",
                            })
                    except Exception as e:
                        log.debug(f"[Internshala] requests error on {page_url}: {e}")
            except Exception as e:
                log.debug(f"[Internshala] Strategy-0 setup failed: {e}")

        if collected:
            log.info(f"[Internshala] Strategy-0 (requests): {len(collected)} listings")

        # --- Strategy 2: JS evaluation + HTML parse on live page ---
        if not collected:
            for page_url in [INTERNSHIP_URL, JOBS_URL]:
                try:
                    await page.goto(page_url, wait_until="networkidle", timeout=40000)
                    await asyncio.sleep(4)

                    # 2a: JS eval
                    cards = await page.evaluate(_JS_EXTRACT)
                    log.debug(f"[Internshala] JS eval found {len(cards or [])} cards on {page_url}")
                    for card in (cards or []):
                        href = card.get("url", "")
                        if href and href not in seen_urls:
                            seen_urls.add(href)
                            collected.append({
                                "id": f"internshala_js_{hash(href)}",
                                "title": card.get("title", ""),
                                "company": card.get("company", ""),
                                "url": href,
                                "location": "",
                                "description": f"{card.get('title', '')} at {card.get('company', '')} — Internshala",
                                "source": "Internshala",
                                "posted_at": "",
                            })

                    # 2b: Regex parse of raw HTML — extract /internship/detail/ links directly
                    if not collected:
                        html = await page.content()
                        log.debug(f"[Internshala] HTML length: {len(html)}, has 'internship/detail': {'internship/detail' in html}")
                        hrefs = list(dict.fromkeys(re.findall(
                            r'href="(/internship/detail/[^"]+)"',
                            html
                        )))
                        # Also try jobs
                        hrefs += list(dict.fromkeys(re.findall(
                            r'href="(/jobs/detail/[^"]+)"',
                            html
                        )))
                        log.debug(f"[Internshala] Regex found {len(hrefs)} unique detail links on {page_url}")
                        for slug in hrefs:
                            href = f"https://internshala.com{slug}"
                            if href in seen_urls:
                                continue
                            seen_urls.add(href)
                            # Extract a human-readable title from the slug
                            # e.g. /internship/detail/software-engineer-internship-at-techco-123 -> Software Engineer Internship At Techco
                            parts = slug.split("/")[-1]  # last slug segment
                            parts = re.sub(r'\d+$', '', parts).strip('-')
                            title = parts.replace("-", " ").title()
                            collected.append({
                                "id": f"internshala_re_{hash(href)}",
                                "title": title,
                                "company": "",
                                "url": href,
                                "location": "",
                                "description": f"{title} — Internshala internship",
                                "source": "Internshala",
                                "posted_at": "",
                            })

                except Exception as e:
                    log.debug(f"[Internshala] Strategy-2 error ({page_url}): {e}")

        # --- Strategy 3: Direct API call via browser cookies ---
        if not collected:
            try:
                cookies = await context.cookies()
                cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                import urllib.request
                for api_url in [
                    "https://internshala.com/internships/ajax_load_internships/",
                    "https://internshala.com/jobs/ajax_load_jobs_listing/",
                ]:
                    req = urllib.request.Request(api_url, method="GET")
                    req.add_header("Cookie", cookie_str)
                    req.add_header("X-Requested-With", "XMLHttpRequest")
                    req.add_header("Referer", INTERNSHIP_URL)
                    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
                    try:
                        with urllib.request.urlopen(req, timeout=15) as resp:
                            data = json.loads(resp.read())
                        parsed = _parse_response(data)
                        for j in parsed:
                            if j["url"] not in seen_urls:
                                seen_urls.add(j["url"])
                                collected.append(j)
                        log.debug(f"[Internshala] Direct API: +{len(parsed)} from {api_url}")
                    except Exception as e:
                        log.debug(f"[Internshala] Direct API error ({api_url}): {e}")
            except Exception as e:
                log.debug(f"[Internshala] Cookie extract failed: {e}")

        jobs = collected
        log.info(f"[Internshala] Collected {len(jobs)} listings")

    except Exception as e:
        log.warning(f"Internshala scraper error: {e}")
    finally:
        try:
            await context.close()
            await pw.stop()
        except Exception:
            pass

    return jobs


def fetch_internshala(headless: bool = True) -> List[dict]:
    """Synchronous entry point called from job_search_agent.py."""
    try:
        return asyncio.run(_fetch(headless))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_fetch(headless))
        finally:
            loop.close()
    except Exception as e:
        log.warning(f"Internshala fetch failed: {e}")
        return []
