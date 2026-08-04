"""
Auto-Apply Engine for JENT.

Handles automated job application submission on:
  - Indeed Easy Apply
  - Internshala Apply Now
  - Unstop Register

Only fires for jobs scoring >= AUTO_APPLY_THRESHOLD and not already applied.
Tracks all attempts in applied_jobs.json.
"""

import asyncio
import json
import logging
import re
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

warnings.filterwarnings("ignore", category=ResourceWarning)

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
import sys
sys.path.insert(0, str(BASE_DIR))
import db


# -----------------------------------------------------------------------
# APPLIED JOBS STATE
# -----------------------------------------------------------------------

def load_applied() -> dict:
    return db.load_applied()


def save_applied(data: dict):
    db.save_applied(data)


def already_applied(job_id: str) -> bool:
    return job_id in load_applied()


def record_application(job: dict, status: str = "submitted", note: str = ""):
    db.record_application(job, status, note)


# -----------------------------------------------------------------------
# RESUME INFO EXTRACTION
# -----------------------------------------------------------------------

def extract_resume_info(resume_text: str) -> dict:
    """
    Extract key fields from raw resume text using regex heuristics.
    Returns a dict with: name, email, phone, top_skills, level
    """
    info = {"name": "", "email": "", "phone": "", "top_skills": "", "level": "student"}

    # Email
    emails = re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", resume_text)
    if emails:
        info["email"] = emails[0]

    # Phone (Indian: 10-digit, may have +91)
    phones = re.findall(r"(?:\+91[\s\-]?)?[6-9]\d{9}", resume_text)
    if phones:
        info["phone"] = phones[0].replace(" ", "").replace("-", "")

    # Name: usually the first non-empty line of the resume
    lines = [l.strip() for l in resume_text.split("\n") if l.strip()]
    if lines:
        first = lines[0]
        # If it looks like a name (no @, not a URL, <= 5 words)
        if "@" not in first and "http" not in first and len(first.split()) <= 5:
            info["name"] = first

    # Level detection
    text_lower = resume_text.lower()
    if any(k in text_lower for k in ["final year", "4th year", "senior year"]):
        info["level"] = "final year"
    elif any(k in text_lower for k in ["pre-final", "3rd year", "penultimate"]):
        info["level"] = "pre-final year"
    elif any(k in text_lower for k in ["2nd year", "sophomore"]):
        info["level"] = "2nd year"
    elif any(k in text_lower for k in ["1st year", "freshman", "first year"]):
        info["level"] = "1st year"

    # Top skills: extract known tech keywords
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
            # Clean up the regex pattern for display
            found.append(re.sub(r"[\\.]", "", skill).replace("+\\+", "++"))
    info["top_skills"] = ", ".join(found[:6]) if found else "software development"

    return info


def render_cover_letter(template: str, job: dict, resume_info: dict) -> str:
    """Fill cover letter template with job and resume data."""
    return template.format(
        title=job.get("title", "this role"),
        company=job.get("company", "your company"),
        level=resume_info.get("level", "student"),
        top_skills=resume_info.get("top_skills", "software development"),
        name=resume_info.get("name", ""),
    ).strip()


# -----------------------------------------------------------------------
# INDEED AUTO-APPLY
# -----------------------------------------------------------------------

async def _apply_indeed(job: dict, context, resume_info: dict, cover_letter: str):
    """
    Attempt Indeed Easy Apply on a job listing.
    Returns 'submitted', 'skipped', or 'failed'.
    """
    page = await context.new_page()
    try:
        log.info(f"[AutoApply/Indeed] Navigating to {job['url'][:80]}")
        await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # Look for Easy Apply button
        apply_btn = await page.query_selector(
            'button[id*="apply"], button[data-jk], '
            '[aria-label*="Apply"], [class*="apply-button"], '
            'button:has-text("Easy Apply"), button:has-text("Apply now")'
        )
        if not apply_btn:
            log.info(f"[AutoApply/Indeed] No Easy Apply button found — skipping")
            return "skipped"

        await apply_btn.click()
        await asyncio.sleep(3)

        # Check if an application modal/page opened
        content = await page.content()
        if "easy apply" not in content.lower() and "application" not in content.lower():
            return "skipped"

        # Fill in any visible text fields (name, phone)
        if resume_info.get("phone"):
            phone_field = await page.query_selector(
                'input[type="tel"], input[name*="phone"], input[placeholder*="phone"]'
            )
            if phone_field:
                await phone_field.fill(resume_info["phone"])
                await asyncio.sleep(0.5)

        # Handle cover letter / additional info fields
        cover_fields = await page.query_selector_all(
            'textarea, [contenteditable="true"]'
        )
        for field in cover_fields[:2]:
            try:
                await field.fill(cover_letter)
                await asyncio.sleep(0.3)
            except Exception:
                pass

        # Submit
        submit_btn = await page.query_selector(
            'button[type="submit"], button:has-text("Submit"), '
            'button:has-text("Apply"), button:has-text("Continue")'
        )
        if submit_btn:
            await submit_btn.click()
            await asyncio.sleep(3)
            log.info(f"[AutoApply/Indeed] Submitted: {job['title']} @ {job.get('company', '')}")
            return "submitted"
        else:
            return "skipped"

    except Exception as e:
        log.warning(f"[AutoApply/Indeed] Error: {e}")
        return "failed"
    finally:
        try:
            await page.close()
        except Exception:
            pass


# -----------------------------------------------------------------------
# INTERNSHALA AUTO-APPLY
# -----------------------------------------------------------------------

async def _apply_internshala(job: dict, context, resume_info: dict, cover_letter: str):
    """Attempt to apply to an Internshala listing."""
    page = await context.new_page()
    try:
        log.info(f"[AutoApply/Internshala] Navigating to {job['url'][:80]}")
        await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # Apply button
        apply_btn = await page.query_selector(
            '#apply-button, .apply-button, button:has-text("Apply Now"), '
            'button:has-text("Apply"), a:has-text("Apply Now")'
        )
        if not apply_btn:
            return "skipped"

        await apply_btn.click()
        await asyncio.sleep(3)

        # Cover letter / "Why should you be hired" field
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

        # Availability field (default: "Immediately")
        avail_field = await page.query_selector(
            "input[placeholder*='availab'], input[name*='availab'], "
            "input[placeholder*='notice'], input[placeholder*='join']"
        )
        if avail_field:
            await avail_field.fill("Immediately")
            await asyncio.sleep(0.3)

        # Submit
        submit_btn = await page.query_selector(
            'button[type="submit"], button:has-text("Submit"), '
            'button:has-text("Apply"), input[type="submit"]'
        )
        if submit_btn:
            await submit_btn.click()
            await asyncio.sleep(3)
            log.info(f"[AutoApply/Internshala] Submitted: {job['title']} @ {job.get('company', '')}")
            return "submitted"
        return "skipped"

    except Exception as e:
        log.warning(f"[AutoApply/Internshala] Error: {e}")
        return "failed"
    finally:
        try:
            await page.close()
        except Exception:
            pass


# -----------------------------------------------------------------------
# UNSTOP AUTO-APPLY
# -----------------------------------------------------------------------

async def _apply_unstop(job: dict, context, resume_info: dict, cover_letter: str):
    """Attempt to register for an Unstop opportunity."""
    page = await context.new_page()
    try:
        log.info(f"[AutoApply/Unstop] Navigating to {job['url'][:80]}")
        await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # Register / Apply button
        apply_btn = await page.query_selector(
            'button:has-text("Register"), button:has-text("Apply"), '
            'button:has-text("Apply Now"), [class*="register-btn"], '
            '[class*="apply-btn"]'
        )
        if not apply_btn:
            return "skipped"

        await apply_btn.click()
        await asyncio.sleep(3)

        # Fill team name if asked (use name or email prefix)
        team_field = await page.query_selector(
            'input[placeholder*="team"], input[name*="team"]'
        )
        if team_field:
            name_part = (resume_info.get("name") or resume_info.get("email", "Team")).split()[0]
            await team_field.fill(f"{name_part}'s Team")
            await asyncio.sleep(0.3)

        # Fill any cover/motivation fields
        cover_fields = await page.query_selector_all("textarea")
        for field in cover_fields[:2]:
            try:
                current_val = await field.input_value()
                if not current_val:
                    await field.fill(cover_letter)
                    await asyncio.sleep(0.3)
            except Exception:
                pass

        # Submit
        submit_btn = await page.query_selector(
            'button[type="submit"], button:has-text("Submit"), '
            'button:has-text("Register"), button:has-text("Proceed")'
        )
        if submit_btn:
            await submit_btn.click()
            await asyncio.sleep(3)
            log.info(f"[AutoApply/Unstop] Registered: {job['title']} @ {job.get('company', '')}")
            return "submitted"
        return "skipped"

    except Exception as e:
        log.warning(f"[AutoApply/Unstop] Error: {e}")
        return "failed"
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

PLATFORM_BROWSER = {
    "indeed": "indeed",
    "internshala": "internshala",
    "unstop": "unstop",
}


def auto_apply(job: dict, resume_text: str, cover_letter_template: str,
               enabled_platforms: list, dry_run: bool = False):
    """
    Synchronous entry point. Called from job_search_agent.py after notify().

    Checks:
      - platform is in enabled_platforms
      - not already applied
      - platform browser session exists
    Then applies.
    """
    source = job.get("source", "").lower()
    platform_key = None
    for key in PLATFORM_APPLY_FN:
        if key in source:
            platform_key = key
            break

    if platform_key is None:
        return  # Platform not supported for auto-apply

    if platform_key not in [p.lower() for p in enabled_platforms]:
        return  # Platform disabled by config

    if already_applied(job["id"]):
        log.info(f"[AutoApply] Already applied to {job['id']} — skipping")
        return

    if dry_run:
        log.info(f"[AutoApply/DRY RUN] Would apply: {job['title']} @ {job.get('company', '')} ({platform_key})")
        return

    resume_info = extract_resume_info(resume_text)
    cover_letter = render_cover_letter(cover_letter_template, job, resume_info)

    async def _run():
        from scrapers.browser_base import get_context, has_saved_session
        if not has_saved_session(platform_key):
            log.warning(f"[AutoApply] No browser session for {platform_key} — log in first")
            record_application(job, "skipped", "no browser session")
            return

        pw, context = await get_context(platform_key, headless=True)
        try:
            fn = PLATFORM_APPLY_FN[platform_key]
            status = await fn(job, context, resume_info, cover_letter)
            record_application(job, status)
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
        record_application(job, "failed", str(e))
