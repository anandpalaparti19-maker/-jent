"""
Auto-Apply Engine for JENT.

Handles automated job application submission on:
  - Indeed Easy Apply
  - Internshala Apply Now
  - Unstop Register

Only fires for jobs scoring >= AUTO_APPLY_THRESHOLD and not already applied.
Tracks progress stages in applied_jobs.json for dashboard live updates.
"""

import asyncio
import logging
import re
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

warnings.filterwarnings("ignore", category=ResourceWarning)

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
import sys
sys.path.insert(0, str(BASE_DIR))
import db


# -----------------------------------------------------------------------
# APPLIED JOBS STATE
# -----------------------------------------------------------------------

def load_applied(user_id: str = None) -> dict:
    return db.load_applied(user_id)


def save_applied(data: dict, user_id: str = None):
    db.save_applied(data, user_id)


def already_applied(job_id: str, user_id: str = None) -> bool:
    return job_id in load_applied(user_id)


def record_application(job: dict, status: str = "submitted", note: str = "",
                       user_id: str = None, platform: str = ""):
    db.record_application(job, status, note, user_id=user_id, platform=platform)


def _progress(job: dict, stage: str, user_id: str = None, platform: str = ""):
    db.update_application_progress(job, stage, user_id=user_id, platform=platform)
    log.info(f"[AutoApply] {stage}: {job.get('title', '')} [{platform}]")


# -----------------------------------------------------------------------
# RESUME HELPERS
# -----------------------------------------------------------------------

def find_resume_file() -> Optional[Path]:
    for ext in (".pdf", ".docx", ".doc"):
        path = BASE_DIR / f"resume{ext}"
        if path.exists() and path.stat().st_size > 0:
            return path
    return None


def extract_resume_info(resume_text: str) -> dict:
    """Extract key fields from raw resume text using regex heuristics."""
    info = {"name": "", "email": "", "phone": "", "top_skills": "", "level": "student"}

    emails = re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", resume_text)
    if emails:
        info["email"] = emails[0]

    phones = re.findall(r"(?:\+91[\s\-]?)?[6-9]\d{9}", resume_text)
    if phones:
        info["phone"] = phones[0].replace(" ", "").replace("-", "")

    lines = [l.strip() for l in resume_text.split("\n") if l.strip()]
    if lines:
        first = lines[0]
        if "@" not in first and "http" not in first and len(first.split()) <= 5:
            info["name"] = first

    text_lower = resume_text.lower()
    if any(k in text_lower for k in ["final year", "4th year", "senior year"]):
        info["level"] = "final year"
    elif any(k in text_lower for k in ["pre-final", "3rd year", "penultimate"]):
        info["level"] = "pre-final year"
    elif any(k in text_lower for k in ["2nd year", "sophomore"]):
        info["level"] = "2nd year"
    elif any(k in text_lower for k in ["1st year", "freshman", "first year"]):
        info["level"] = "1st year"

    skill_keywords = [
        "python", "javascript", "typescript", "java", "c\\+\\+", "c#", "go", "rust",
        "react", "node", "django", "flask", "fastapi", "spring",
        "machine learning", "deep learning", "pytorch", "tensorflow",
        "sql", "mysql", "postgresql", "mongodb", "redis",
        "aws", "gcp", "azure", "docker", "kubernetes",
        "html", "css", "vue", "angular", "next\\.?js",
        "git", "linux", "data science", "nlp", "opencv",
    ]
    found = []
    for skill in skill_keywords:
        if re.search(skill, text_lower):
            found.append(re.sub(r"[\\.]", "", skill).replace("+\\+", "++"))
    info["top_skills"] = ", ".join(found[:6]) if found else "software development"

    return info


def render_cover_letter(template: str, job: dict, resume_info: dict) -> str:
    return template.format(
        title=job.get("title", "this role"),
        company=job.get("company", "your company"),
        level=resume_info.get("level", "student"),
        top_skills=resume_info.get("top_skills", "software development"),
        name=resume_info.get("name", ""),
    ).strip()


# -----------------------------------------------------------------------
# SHARED FORM HELPERS
# -----------------------------------------------------------------------

SUCCESS_KEYWORDS = (
    "application submitted", "thank you for applying", "application sent",
    "successfully applied", "application received", "we have received your application",
    "applied successfully", "registration successful",
)

SUBMIT_SELECTORS = [
    'button:has-text("Submit application")',
    'button:has-text("Submit your application")',
    'button:has-text("Submit")',
    'button:has-text("Apply")',
    'input[type="submit"]',
    'button[type="submit"]',
]

CONTINUE_SELECTORS = [
    'button:has-text("Continue")',
    'button:has-text("Next")',
    'button:has-text("Review")',
    'button:has-text("Proceed")',
]


async def _page_has_success(page) -> bool:
    content = (await page.content()).lower()
    return any(kw in content for kw in SUCCESS_KEYWORDS)


async def _fill_visible_fields(page, cover_letter: str, resume_info: dict):
    """Fill common text fields on the current step."""
    if resume_info.get("phone"):
        phone_field = await page.query_selector(
            'input[type="tel"], input[name*="phone"], input[placeholder*="phone" i]'
        )
        if phone_field:
            try:
                await phone_field.fill(resume_info["phone"])
                await asyncio.sleep(0.3)
            except Exception:
                pass

    if resume_info.get("email"):
        email_field = await page.query_selector(
            'input[type="email"], input[name*="email"], input[placeholder*="email" i]'
        )
        if email_field:
            try:
                val = await email_field.input_value()
                if not val:
                    await email_field.fill(resume_info["email"])
                    await asyncio.sleep(0.3)
            except Exception:
                pass

    if resume_info.get("name"):
        name_field = await page.query_selector(
            'input[name*="name" i], input[placeholder*="full name" i], input[placeholder*="your name" i]'
        )
        if name_field:
            try:
                val = await name_field.input_value()
                if not val:
                    await name_field.fill(resume_info["name"])
                    await asyncio.sleep(0.3)
            except Exception:
                pass

    cover_fields = await page.query_selector_all('textarea, [contenteditable="true"]')
    for field in cover_fields[:3]:
        try:
            current = await field.input_value()
            if not current:
                await field.fill(cover_letter)
                await asyncio.sleep(0.3)
        except Exception:
            try:
                await field.fill(cover_letter)
            except Exception:
                pass

    resume_path = find_resume_file()
    if resume_path:
        file_input = await page.query_selector('input[type="file"]')
        if file_input:
            try:
                await file_input.set_input_files(str(resume_path))
                await asyncio.sleep(1)
            except Exception:
                pass


async def _click_first_visible(page, selectors: list):
    for sel in selectors:
        btn = await page.query_selector(sel)
        if btn:
            try:
                if await btn.is_visible():
                    await btn.click()
                    return True
            except Exception:
                try:
                    await btn.click()
                    return True
                except Exception:
                    pass
    return False


async def _advance_multi_step(page, cover_letter: str, resume_info: dict, max_steps: int = 8) -> str:
    """Click through multi-step apply wizards (Indeed, etc.)."""
    for _ in range(max_steps):
        if await _page_has_success(page):
            return "submitted"

        await _fill_visible_fields(page, cover_letter, resume_info)

        if await _page_has_success(page):
            return "submitted"

        if await _click_first_visible(page, SUBMIT_SELECTORS):
            await asyncio.sleep(2)
            if await _page_has_success(page):
                return "submitted"
            continue

        if await _click_first_visible(page, CONTINUE_SELECTORS):
            await asyncio.sleep(2)
            continue

        break
    return "skipped"


# -----------------------------------------------------------------------
# LOGIN CHECKS PER PLATFORM
# -----------------------------------------------------------------------

PLATFORM_LOGIN = {
    "indeed": {
        "url": "https://in.indeed.com/",
        "indicator": "Sign out",
    },
    "internshala": {
        "url": "https://internshala.com/student/dashboard",
        "indicator": "dashboard",
    },
    "unstop": {
        "url": "https://unstop.com/",
        "indicator": "My Profile",
    },
}


async def _verify_logged_in(page, platform_key: str) -> bool:
    from scrapers.browser_base import is_logged_in
    cfg = PLATFORM_LOGIN.get(platform_key, {})
    if not cfg:
        return True
    return await is_logged_in(page, cfg["url"], cfg["indicator"])


# -----------------------------------------------------------------------
# INDEED AUTO-APPLY
# -----------------------------------------------------------------------

async def _apply_indeed(job: dict, context, resume_info: dict, cover_letter: str,
                        on_progress: Optional[Callable] = None):
    page = await context.new_page()
    try:
        if on_progress:
            on_progress("checking_login")
        if not await _verify_logged_in(page, "indeed"):
            log.info("[AutoApply/Indeed] Not logged in — skipping")
            return "skipped", "not logged in"

        if on_progress:
            on_progress("navigating")
        log.info(f"[AutoApply/Indeed] Navigating to {job['url'][:80]}")
        await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        if on_progress:
            on_progress("finding_apply_button")
        apply_btn = await page.query_selector(
            'button[id*="apply"], button[data-jk], '
            '[aria-label*="Apply"], [class*="apply-button"], '
            'button:has-text("Easy Apply"), button:has-text("Apply now")'
        )
        if not apply_btn:
            log.info("[AutoApply/Indeed] No Easy Apply button found — skipping")
            return "skipped", "no easy apply button"

        await apply_btn.click()
        await asyncio.sleep(3)

        content = (await page.content()).lower()
        if "easy apply" not in content and "application" not in content:
            return "skipped", "apply modal did not open"

        if on_progress:
            on_progress("filling_form")
        status = await _advance_multi_step(page, cover_letter, resume_info, max_steps=10)

        if status == "submitted":
            if on_progress:
                on_progress("submitting")
            log.info(f"[AutoApply/Indeed] Submitted: {job['title']} @ {job.get('company', '')}")
            return "submitted", ""
        return "skipped", "could not complete wizard"

    except Exception as e:
        log.warning(f"[AutoApply/Indeed] Error: {e}")
        return "failed", str(e)
    finally:
        try:
            await page.close()
        except Exception:
            pass


# -----------------------------------------------------------------------
# INTERNSHALA AUTO-APPLY
# -----------------------------------------------------------------------

async def _apply_internshala(job: dict, context, resume_info: dict, cover_letter: str,
                              on_progress: Optional[Callable] = None):
    page = await context.new_page()
    try:
        if on_progress:
            on_progress("checking_login")
        if not await _verify_logged_in(page, "internshala"):
            log.info("[AutoApply/Internshala] Not logged in — skipping")
            return "skipped", "not logged in"

        if on_progress:
            on_progress("navigating")
        log.info(f"[AutoApply/Internshala] Navigating to {job['url'][:80]}")
        await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        if on_progress:
            on_progress("finding_apply_button")
        apply_btn = await page.query_selector(
            '#apply-button, .apply-button, button:has-text("Apply Now"), '
            'button:has-text("Apply"), a:has-text("Apply Now")'
        )
        if not apply_btn:
            return "skipped", "no apply button"

        await apply_btn.click()
        await asyncio.sleep(3)

        if on_progress:
            on_progress("filling_form")
        cover_fields = await page.query_selector_all(
            "textarea[name*='cover'], textarea[placeholder*='cover'], "
            "textarea[placeholder*='why'], textarea[id*='cover'], textarea"
        )
        for field in cover_fields[:2]:
            try:
                current_val = await field.input_value()
                if not current_val:
                    await field.fill(cover_letter)
                    await asyncio.sleep(0.3)
            except Exception:
                pass

        avail_field = await page.query_selector(
            "input[placeholder*='availab'], input[name*='availab'], "
            "input[placeholder*='notice'], input[placeholder*='join']"
        )
        if avail_field:
            try:
                val = await avail_field.input_value()
                if not val:
                    await avail_field.fill("Immediately")
                    await asyncio.sleep(0.3)
            except Exception:
                pass

        resume_path = find_resume_file()
        if resume_path:
            file_input = await page.query_selector('input[type="file"]')
            if file_input:
                try:
                    await file_input.set_input_files(str(resume_path))
                    await asyncio.sleep(1)
                except Exception:
                    pass

        if on_progress:
            on_progress("submitting")
        if await _click_first_visible(page, SUBMIT_SELECTORS):
            await asyncio.sleep(3)
            if await _page_has_success(page):
                log.info(f"[AutoApply/Internshala] Submitted: {job['title']} @ {job.get('company', '')}")
                return "submitted", ""

        content = (await page.content()).lower()
        if "applied" in content or "application" in content:
            return "submitted", ""
        return "skipped", "submit button not found"

    except Exception as e:
        log.warning(f"[AutoApply/Internshala] Error: {e}")
        return "failed", str(e)
    finally:
        try:
            await page.close()
        except Exception:
            pass


# -----------------------------------------------------------------------
# UNSTOP AUTO-APPLY
# -----------------------------------------------------------------------

async def _apply_unstop(job: dict, context, resume_info: dict, cover_letter: str,
                        on_progress: Optional[Callable] = None):
    page = await context.new_page()
    try:
        if on_progress:
            on_progress("checking_login")
        if not await _verify_logged_in(page, "unstop"):
            log.info("[AutoApply/Unstop] Not logged in — skipping")
            return "skipped", "not logged in"

        if on_progress:
            on_progress("navigating")
        log.info(f"[AutoApply/Unstop] Navigating to {job['url'][:80]}")
        await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        if on_progress:
            on_progress("finding_apply_button")
        apply_btn = await page.query_selector(
            'button:has-text("Register"), button:has-text("Apply"), '
            'button:has-text("Apply Now"), [class*="register-btn"], '
            '[class*="apply-btn"]'
        )
        if not apply_btn:
            return "skipped", "no register button"

        await apply_btn.click()
        await asyncio.sleep(3)

        if on_progress:
            on_progress("filling_form")
        team_field = await page.query_selector('input[placeholder*="team"], input[name*="team"]')
        if team_field:
            name_part = (resume_info.get("name") or resume_info.get("email", "Team")).split()[0]
            try:
                await team_field.fill(f"{name_part}'s Team")
                await asyncio.sleep(0.3)
            except Exception:
                pass

        await _fill_visible_fields(page, cover_letter, resume_info)

        if on_progress:
            on_progress("submitting")
        status = await _advance_multi_step(page, cover_letter, resume_info, max_steps=6)
        if status == "submitted":
            log.info(f"[AutoApply/Unstop] Registered: {job['title']} @ {job.get('company', '')}")
            return "submitted", ""
        return "skipped", "registration incomplete"

    except Exception as e:
        log.warning(f"[AutoApply/Unstop] Error: {e}")
        return "failed", str(e)
    finally:
        try:
            await page.close()
        except Exception:
            pass


# -----------------------------------------------------------------------
# PUBLIC ENTRY POINT
# -----------------------------------------------------------------------

PLATFORM_APPLY_FN = {
    "indeed": _apply_indeed,
    "internshala": _apply_internshala,
    "unstop": _apply_unstop,
}


def auto_apply(job: dict, resume_text: str, cover_letter_template: str,
               enabled_platforms: list, dry_run: bool = False, user_id: str = None):
    """
    Synchronous entry point. Called from job_search_agent.py after notify().

    Checks platform support, score threshold (caller), duplicate apply, and session.
    Reports progress stages to applied_jobs for dashboard tracking.
    """
    source = job.get("source", "").lower()
    platform_key = None
    for key in PLATFORM_APPLY_FN:
        if key in source:
            platform_key = key
            break

    if platform_key is None:
        return

    if platform_key not in [p.lower() for p in enabled_platforms]:
        return

    if already_applied(job["id"], user_id):
        log.info(f"[AutoApply] Already applied to {job['id']} — skipping")
        return

    if dry_run:
        log.info(f"[AutoApply/DRY RUN] Would apply: {job['title']} @ {job.get('company', '')} ({platform_key})")
        return

    resume_info = extract_resume_info(resume_text)
    cover_letter = render_cover_letter(cover_letter_template, job, resume_info)

    def on_progress(stage: str):
        _progress(job, stage, user_id=user_id, platform=platform_key)

    async def _run():
        from scrapers.browser_base import get_context, has_saved_session
        _progress(job, "starting", user_id=user_id, platform=platform_key)

        if not has_saved_session(platform_key):
            log.warning(f"[AutoApply] No browser session for {platform_key} — log in first")
            record_application(job, "skipped", "no browser session", user_id=user_id, platform=platform_key)
            return

        pw, context = await get_context(platform_key, headless=True)
        try:
            fn = PLATFORM_APPLY_FN[platform_key]
            status, note = await fn(job, context, resume_info, cover_letter, on_progress=on_progress)
            record_application(job, status, note, user_id=user_id, platform=platform_key)
            log.info(f"[AutoApply] {status.upper()}: {job['title']} @ {job.get('company', '')} [{platform_key}]")
        finally:
            try:
                await context.close()
                await pw.stop()
            except Exception:
                pass

    try:
        asyncio.run(_run())
    except Exception as e:
        log.warning(f"[AutoApply] Unexpected error: {e}")
        record_application(job, "failed", str(e), user_id=user_id, platform=platform_key or "")
