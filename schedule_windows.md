# Running on Windows (Task Scheduler)

1. Put the whole `job_agent` folder somewhere permanent, e.g. `C:\Users\<you>\job_agent`.
2. Open **Task Scheduler** → **Create Task** (not "Basic Task", so you get full options).
3. **General tab**: name it `Job Search Agent`. Check "Run whether user is logged on or not" if you want it to run even when locked.
4. **Triggers tab** → New:
   - Begin the task: *On a schedule*
   - Recur every 2 hours (or your preference), starting now.
5. **Actions tab** → New:
   - Action: *Start a program*
   - Program/script: `python` (or the full path from `where python`)
   - Add arguments: `job_search_agent.py`
   - Start in: `C:\Users\<you>\job_agent`
6. **Conditions/Settings tabs**: uncheck "Start the task only if the computer is on AC power" if you're on a laptop and want it to run on battery too.
7. Save. Right-click the task → **Run** to test it once immediately.
8. Check `agent.log` (redirect output yourself, or just watch the Task Scheduler "Last Run Result" column) to confirm it worked.

Set your Zapier webhook URL and resume path either as permanent environment variables (System Properties → Environment Variables) or by editing `ZAPIER_WEBHOOK_URL` / `RESUME_PATH` directly at the top of `job_search_agent.py`.
