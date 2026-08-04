# Continuous Job/Internship Search Agent (v2)

A no-API-key pipeline that runs 24/7: pulls fresh listings from public job
sources, scores them against your resume using local TF-IDF + cosine
similarity, dedupes, and pushes new matches into a Zapier Zap that emails
you.

```
resume.pdf/.docx --------> resume text extraction
                                    |
     RemoteOK / Arbeitnow / Jobicy / Himalayas / HN / WWR / (Greenhouse/Lever)
                                    |
                    TF-IDF + cosine similarity scoring  <-- the "AI" layer
                                    |
                       filter (score + seniority) + dedupe
                                    |
                       POST new matches --> Zapier webhook
                                    |
                Zapier: Catch Hook -> Gmail "Send Email"
```

## Why not LinkedIn / Indeed

Both require a login to see full search results and explicitly forbid
automated scraping in their Terms of Service. There's no keyless,
ToS-compliant way to hit either continuously — this is a hard line, not a
missing feature. If you get official access later (Indeed Publisher API,
LinkedIn Talent Solutions), those use real API keys and slot in as one more
`fetch_*` function each, same pattern as the ones already here.

What's included instead casts a genuinely wide net: RemoteOK, Arbeitnow,
Jobicy, and Himalayas are all general job boards with public JSON APIs.
Greenhouse and Lever are the two most common ATS platforms — thousands of
companies host their real career pages there with a public, unauthenticated
JSON endpoint (not scraping — it's the intended public interface), so you
can track specific companies you care about directly.

## 1. Install

```bash
cd job_agent
pip install -r requirements.txt
```

## 2. Add your resume

Drop it in this folder as `resume.pdf` or `resume.docx` (matches the
default `RESUME_PATH`), or point elsewhere:

```bash
export RESUME_PATH="/path/to/resume.pdf"
```

The whole resume text is used for TF-IDF similarity scoring — no need to
hand-pick keywords. If no resume is found, it falls back to a generic
internship/entry-level text profile.

## 3. (Optional) Track specific companies

Edit `GREENHOUSE_COMPANY_SLUGS` / `LEVER_COMPANY_SLUGS` at the top of
`job_search_agent.py`. Find a company's slug from their careers URL:
`boards.greenhouse.io/stripe` → `"stripe"`, `jobs.lever.co/netflix` →
`"netflix"`.

## 4. Set up Gmail delivery (two options - pick one, or use both)

### Option A - Direct Gmail (simplest, no Zapier needed for email)

1. Turn on 2-Step Verification on your Google account, if it isn't already:
   https://myaccount.google.com/security
2. Generate an **App Password**: https://myaccount.google.com/apppasswords
   - This is a 16-character password Google generates specifically for
     apps/scripts like this one. It is **not** an API key and **not** your
     real password — it only allows SMTP mail sending, and you can revoke
     it any time from the same page.
3. Set these before running the agent:
   ```bash
   export GMAIL_ADDRESS="you@gmail.com"
   export GMAIL_APP_PASSWORD="xxxxxxxxxxxxxxxx"   # the 16-char app password, no spaces
   export GMAIL_TO_ADDRESS="you@gmail.com"          # optional, defaults to GMAIL_ADDRESS
   ```
4. That's it — the agent will email you directly via Gmail's SMTP server.
   No Zapier step required for this path.

### Option B - Via Zapier (adds Sheets/Slack logging, other automations)

Keep this if you still want the Zap for logging matches to a spreadsheet or
pinging Slack alongside the email — see "Build the Zap" below. If both
Zapier and direct Gmail are configured, Zapier is tried first and direct
Gmail is the automatic fallback if the webhook ever fails.

To force *only* direct Gmail even when a Zapier URL is set:
```bash
export FORCE_DIRECT_GMAIL=true
```

## 5. Build the Zap (optional, one-time, ~5 minutes)

1. zapier.com → **Create Zap**.
2. **Trigger**: "Webhooks by Zapier" → **Catch Hook**. Copy the generated URL.
3. **Action**: **Gmail** (already connected) → **Send Email**.
   - Subject: `New job match: {{title}} at {{company}}`
   - Body: include `{{title}}`, `{{company}}`, `{{url}}`, `{{source}}`,
     `{{match_score}}` — map these once you've sent a test payload through.
4. Turn the Zap **ON**.

Want a running log too? Add a second action: **Google Sheets → Create
Spreadsheet Row** with the same fields. Prefer Slack? Swap Gmail for
**Slack → Send Channel Message**.

## 6. Wire in the webhook URL (only if using Option B)

```bash
export ZAPIER_WEBHOOK_URL="https://hooks.zapier.com/hooks/catch/123456/abcdef/"
```

## 7. Test once

```bash
python job_search_agent.py --once
```

Check the printed fetch/filter/score counts, confirm an email arrives, then
run it again — the second run should report far fewer (ideally zero) new
matches for anything already seen.

## 8. Run it 24/7

Three ways, pick one:

- **Foreground, simplest**: `python job_search_agent.py` — loops forever,
  checking every `CHECK_INTERVAL_HOURS` (default 2), until you Ctrl+C it.
  Fine for testing, not durable across reboots/crashes.
- **systemd service (recommended for Linux, true always-on)**: see
  `job_agent.service` — auto-restarts on crash, survives reboots.
- **cron**: still works if you'd rather have short periodic runs instead of
  a persistent process — use `python job_search_agent.py --once` as the
  cron command. See `schedule_windows.md` for the Windows Task Scheduler
  equivalent.

## Tuning

- `SIMILARITY_THRESHOLD` (0–1): raise it for fewer, more precise matches;
  lower it for broader results. Start at the default 0.12 and adjust after
  watching a few cycles.
- `LEVEL_FILTERS`: restricts to internship/entry-level/junior/new-grad
  postings. Set to `[]` to see everything above the similarity bar
  regardless of seniority.
- `CHECK_INTERVAL_HOURS`: how often it checks. Job boards don't update
  faster than hourly, so 1–3 hours is plenty and avoids hammering the free
  APIs.
- `seen_jobs.json`: the dedupe memory. Delete it to force a fresh full
  re-scan and re-notification of everything currently matching.
