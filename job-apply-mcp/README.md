# job-apply-mcp

> **Auto-apply to jobs across 8 Indian job portals using AI-powered form filling.**

A Model Context Protocol (MCP) server that searches, filters, and applies to jobs across major Indian job portals — LinkedIn, Naukri, Wellfound, Indeed, Hirist, Glassdoor, Instahyre, and Cutshort. It uses Playwright to drive a real browser, fills application forms automatically using your config, and tracks every application in a local database.

Originally built for a **DevOps / MLOps** profile, but works for **any role** by editing the config.

---

## Features

- **8 platforms** supported: LinkedIn, Naukri, Wellfound, Indeed, Hirist, Glassdoor, Instahyre, Cutshort
- **Easy-Apply only filter** — skips jobs requiring external application sites
- **Smart form auto-fill** — answers questions in text fields, dropdowns and radio buttons (CTC, experience, notice period, location, gender, etc.)
- **Honest answers** — never claims certifications you don't hold, and answers 0 for technologies missing from your config instead of inheriting your total experience
- **Title gate** — a job's title must match your target roles, so loosely-related results are filtered out before applying
- **Resume upload** — handles visible, hidden, and file-chooser uploads
- **Match scoring** — ranks jobs by skill / role / location relevance
- **Date filter** — skips jobs older than 30 days
- **Pagination** — fetches up to 100 jobs per keyword (5 pages)
- **Application tracking** — SQLite DB prevents duplicate applications
- **Rate limiting** — 20–30 second delay between applies
- **Persistent sessions** — saved login cookies + browser profiles
- **Two ways to use** — interactive CLI (`run.py`) or via Claude Desktop / Kiro IDE (MCP)

---

## Quick Start (5 Minutes)

### 1. Install Python 3.11+
```bash
python3 --version  # must be 3.11 or higher
```

### 2. Clone the repo
```bash
git clone https://github.com/pulkit017/job-apply-mcp.git
cd job-apply-mcp
```

### 3. Install dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate          # macOS/Linux
# .venv\Scripts\activate           # Windows PowerShell

pip install mcp playwright
python -m playwright install firefox
```

### 4. Set up your profile
The config file lives at `~/.job-apply-mcp/config.json`. It will auto-create on first run, but you can create it manually:

```bash
mkdir -p ~/.job-apply-mcp
nano ~/.job-apply-mcp/config.json
```

Paste this template and **fill in your details**:

```json
{
  "resume_path": "/full/path/to/your_resume.pdf",
  "name": "Your Full Name",
  "email": "you@example.com",
  "phone": "+91-XXXXXXXXXX",
  "location": "India",
  "experience_years": 3,
  "credentials": {
    "linkedin": { "email": "", "password": "" },
    "naukri":   { "email": "", "password": "" },
    "wellfound":{ "email": "", "password": "" },
    "indeed":   { "email": "", "password": "" },
    "hirist":   { "email": "", "password": "" }
  },
  "autofill": {
    "gender": "Male",
    "date_of_birth": "DD/MM/YYYY",
    "preferred_locations": ["Remote", "Bangalore", "Pune", "Hyderabad"],
    "experience": {
      "devops": "3.9",
      "kubernetes": "3",
      "docker": "3.5",
      "aws": "1",
      "azure": "3",
      "gcp": "1.5",
      "terraform": "2",
      "python": "3"
    },
    "notice_period": "15 days",
    "current_ctc": "9",
    "expected_ctc": "16",
    "total_experience": "3.9",
    "primary_cloud": "GCP",
    "last_working_day": "Currently Working",
    "contract_based": "Yes"
  }
}
```

> **Note:** Credentials are optional. The recommended approach is to log in manually via `save_session` (browser opens, you log in once, cookies saved).

### 5. Log in to at least one platform

```bash
python -c "
import asyncio, sys; sys.path.insert(0,'.')
from tools.session import interactive_login
print(asyncio.run(interactive_login('naukri')))
"
```

A Firefox window opens — log in (email/password or OTP). You have **2 minutes**
before the window closes and the session is saved.

For **LinkedIn**, run the same command with `'linkedin'`:

```bash
python -c "
import asyncio, sys; sys.path.insert(0,'.')
from tools.session import interactive_login
print(asyncio.run(interactive_login('linkedin')))
"
```

> **How the two differ:** most platforms save a cookie jar to
> `~/.job-apply-mcp/sessions/<platform>.json`. LinkedIn instead keeps the whole
> Firefox profile at `~/.job-apply-mcp/browser-profiles/linkedin/`, because
> LinkedIn ties a session to browser state beyond cookies and a cookie-only
> replay gets logged out. Search and apply relaunch that same profile, so your
> LinkedIn login persists there until LinkedIn expires it (typically days to
> weeks). Re-run the command above whenever searches start returning 0 jobs.

### 6. Run!

**Interactive CLI** (easiest for first-time users):
```bash
python run.py
```

You'll see a menu:
```
  1. Login to a platform (save session)
  2. Search & Apply for jobs
  3. View application status
  4. Search only (no apply)
  5. Exit
```

---

## Customising for Your Job Type

This MCP defaults to DevOps / MLOps roles. To use it for **any other role**, edit two things:

### A. Update your config keywords
In your `~/.job-apply-mcp/config.json`, the `experience` map should list the technologies you know. The chatbot autofill matches these against questions like *"How many years of experience in Python?"*

### B. Update the candidate profile (optional)
For better job matching, edit `tools/profile.py`:
- `skills` — your full skill list
- `target_roles` — job titles you want
- `default_search_keywords` — what to search for by default
- `avoid_keywords` — roles to exclude (e.g., frontend, mobile)

Example for a **Frontend Developer** profile:
```python
skills = ("React", "TypeScript", "Next.js", "Tailwind", "GraphQL", ...)
target_roles = ("Frontend Engineer", "React Developer", "UI Engineer", ...)
default_search_keywords = ("React Developer", "Frontend Engineer", ...)
avoid_keywords = ("backend", "devops", "ml", "data science", ...)
```

---

## Using with Claude Desktop or Kiro IDE

This is also an **MCP server**, so you can talk to it through Claude Desktop or Kiro IDE.

### Claude Desktop
Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):
```json
{
  "mcpServers": {
    "job-apply-mcp": {
      "command": "/full/path/to/job-apply-mcp/.venv/bin/python",
      "args": ["/full/path/to/job-apply-mcp/server.py"]
    }
  }
}
```

### Kiro IDE
Edit `~/.kiro/settings/mcp.json`:
```json
{
  "mcpServers": {
    "job-apply-mcp": {
      "command": "/full/path/to/job-apply-mcp/.venv/bin/python",
      "args": ["/full/path/to/job-apply-mcp/server.py"],
      "disabled": false
    }
  }
}
```

Then in chat: *"Search Naukri for DevOps jobs and apply to the top 10"*

---

## Available Tools

| Tool | Description |
|------|-------------|
| `search_jobs` | Search one or more platforms — keywords, location, `days` recency, `fetch_jd` |
| `filter_jobs` | Apply the title gate + employer exclusions, rank by match score |
| `apply_job` | Apply to a single job URL |
| `bulk_apply` | Apply to many jobs — rate limiting, dedup, `max_per_company` cap |
| `get_application_status` | Show recent applications grouped by platform & status |
| `save_session` | Open a browser to log in manually (cookies, or the LinkedIn profile) |

---

## Prompts That Work Well

Copy-paste these into Claude Desktop / Kiro. They're written to survive the
quirks documented above — LinkedIn's thin result pool, Naukri's deep one, and
the fact that applications can't be undone.

### Naukri — bulk apply (its strength is volume)

```text
Search Naukri for DevOps Engineer, Site Reliability Engineer, Cloud Engineer
and Platform Engineer roles in India, 4 years experience. Run one search per
keyword and merge the results, deduping by URL.

Then filter to jobs posted in the last 7 days, show me the ranked shortlist
(title, company, location, score, posted age), and stop.

After I confirm, bulk_apply to the top 10 with dry_run=false and
max_per_company=2 (the default) so no single recruiter is over-applied to.
Report exactly what was submitted, skipped and failed, plus the questions
answered for each.
```

### LinkedIn — recent jobs (expect a small pool)

```text
Search LinkedIn for DevOps Engineer, Cloud Engineer, Site Reliability Engineer
and Platform Engineer in India with days=1 and fetch_jd=false, 4 years
experience.

LinkedIn only returns ~11 cards per search and repeats jobs under different
tracking IDs, so dedupe by URL with the query string stripped, and tell me how
many unique jobs survived the title gate.

Show me the shortlist with URLs and whether I've already applied. Don't apply
until I confirm. Then bulk_apply with dry_run=false.
```

### Both platforms — daily run

```text
Do my daily job run:
1. Search Naukri and LinkedIn separately for DevOps/SRE/Cloud/Platform Engineer
   roles in India, days=2, fetch_jd=false for LinkedIn.
2. Filter each, drop anything I've already applied to, and merge into one
   ranked list.
3. Show me the list first — I want to see it before anything is submitted.
4. On my go-ahead, apply to at most 10 total.
5. Finish with get_application_status for the last 7 days.
```

### Safe preview (no applications sent)

```text
Search Naukri and LinkedIn for DevOps roles in India posted in the last 3 days.
Show me the ranked shortlist with URLs and match scores. Use dry_run=true —
do not submit anything.
```

**Tips for writing your own:**

- Always ask for the shortlist **before** applying. Applications cannot be
  withdrawn through this tool.
- Say `dry_run=false` explicitly when you *do* want real submissions —
  `bulk_apply` defaults to preview mode.
- The per-company cap is 2 by default. Drop it to `max_per_company=1` on
  staffing-heavy searches where one firm posts dozens of near-identical roles.
- For LinkedIn, add `fetch_jd=false` unless you want the slower, more accurate
  description-based scoring.
- Ask it to report the answers given per job, so you can spot a wrong one early.

---

## How Form Filling Works

When a job has a chatbot/questionnaire, the bot reads the question and matches it against your config:

| Question pattern | Auto-answer |
|------------------|-------------|
| "Current CTC", "Current salary" | `current_ctc` |
| "Expected CTC", "Expected salary" | `expected_ctc` |
| "How many years in Python/AWS/Docker..." | `experience.<tech>` |
| "Notice period", "When can you join" | `notice_period` |
| "Primary cloud", "Preferred cloud" | `primary_cloud` |
| "Last working day", "LWD" | `last_working_day` |
| "Contract", "C2H", "Contractual" | `contract_based` |
| "Gender" | `gender` |
| "Comfortable for face-to-face / onsite / relocate" | `Yes` |
| "Date of birth" | `date_of_birth` |
| "Resume / CV / upload" | uploads `resume_path` |

Questions are answered in **text inputs, dropdowns (`<select>`), and radio
buttons** alike. If a question matches no pattern it answers "Yes" rather than
skipping — an unanswered required field silently blocks the form from
submitting.

### Answer accuracy rules

The bot deliberately avoids overstating your background:

- **Certifications** — answers "Yes" only if the certification appears in your
  `certifications` list; otherwise "No".
- **Years of experience** — a named technology in your `experience` map uses
  that value. A technology *not* in the map answers **0** rather than inheriting
  your total. Genuinely "total/overall" questions use `total_experience`.
- **Experience ranges** — picks the option whose range *contains* your figure
  (e.g. `2-5 years` for 4), not the nearest midpoint.
- **Factual yes/no** — "currently serving notice?" comes from `current_status`,
  and "currently in \<city\>?" is compared against your `location`.
- **Willingness questions** — relocation, background checks, onsite/F2F all
  answer "Yes".

Add any skill you legitimately have to the `experience` map, or it will be
answered as 0.

---

## How LinkedIn Works

LinkedIn is handled differently from the other platforms and is worth
understanding before you run it.

**Search.** Uses LinkedIn's own filters via URL parameters — Easy Apply only
(`f_AL`), experience level, most-recent sort, and its native recency filter
(`f_TPR`), so "past 24 hours" is enforced server-side rather than guessed from
scraped text. The results list is virtualised, so the scraper extracts on every
scroll step and dedupes by URL (LinkedIn returns the same job under different
tracking IDs).

**Expect a small pool.** LinkedIn returns roughly 11 cards per search regardless
of how many results it claims, and different keywords overlap heavily. A dozen
keyword variants typically yields only ~30 unique jobs. Naukri returns far more
per search — use it when you want volume.

**Apply.** The Easy Apply control is an `<a>` link (not a button) pointing at
LinkedIn's server-driven apply flow, which renders as a `role="dialog"`
container rather than a native `<dialog>`. The bot navigates to that URL, then
steps through Contact info → Resume → optional extras → Additional questions →
Review → **Submit application**, filling each page as it goes.

**Relevance filtering.** A hard *title gate* runs before scoring: a job's title
must contain one of `devops`, `sre`, `site reliability`, `cloud engineer`,
`systems engineer`, or `platform engineer`. This exists because LinkedIn's
search pads results with loosely-related roles (Data Engineer, GenAI Engineer,
Full-Stack). Edit `title_must_contain` in `tools/profile.py` for your own roles.

**Safeguards.**

- `avoid_companies` in your config blocks your current and previous employers.
- **Per-company cap — on by default.** `bulk_apply` applies to at most **2 roles
  per company**, counted from applications already recorded in the last day
  (`company_window_days`), so the cap holds across consecutive batches instead
  of resetting each run. Staffing firms repost near-identical roles under
  separate URLs, and without this one recruiter can collect five applications
  in an afternoon. Tune with `max_per_company` / `company_window_days`, or pass
  `max_per_company=null` to disable.
- Jobs recorded as `applied` are never retried; ones that `failed` are.
- 20–30 second delay between applications.

> **Caveat:** LinkedIn changes this UI often. If applies start failing with
> "No Easy Apply button" or "modal did not open", the selectors in
> `_apply_linkedin` (`tools/apply.py`) are the place to look.

---

## File Structure

```
job-apply-mcp/
├── server.py              # MCP server entry point
├── run.py                 # Interactive CLI runner
├── config.py              # Config loader
├── requirements.txt
├── README.md
└── tools/
    ├── profile.py         # Candidate profile + match scoring
    ├── search.py          # Per-platform job search + pagination
    ├── apply.py           # Apply automation + form filling
    ├── session.py         # Login session management
    └── tracker.py         # SQLite application history
```

---

## Data & Privacy

All your data stays local. **Nothing is sent anywhere except the job portals you're applying to.**

| Path | What it contains |
|------|------------------|
| `~/.job-apply-mcp/config.json` | Your name, email, resume path, autofill answers |
| `~/.job-apply-mcp/sessions/*.json` | Saved login cookies (all platforms except LinkedIn) |
| `~/.job-apply-mcp/browser-profiles/linkedin/` | Full Firefox profile holding your LinkedIn login |
| `~/.job-apply-mcp/applications.db` | SQLite log of every application |

**These are NOT in this git repo and will NEVER be pushed.**

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Naukri OK status=200` then `SSL_ERROR_UNKNOWN` later | Network instability — try a different network or mobile hotspot |
| `No direct apply button found` | Job is "Apply on company site" — these are intentionally skipped |
| `CAPTCHA detected` | Run `save_session` to log in manually first |
| Resume upload fails | Check `resume_path` in config is an absolute path that exists |
| All jobs show "Already applied" | The DB has your history — to test fresh, delete `~/.job-apply-mcp/applications.db` |
| Naukri rate-limits ("error processing your request") | Slow down — wait 24 hours, you've done too many in a short time |
| LinkedIn search returns 0 jobs | Session expired — re-run `interactive_login('linkedin')` to log in to the persistent profile |
| LinkedIn: `No Easy Apply button — external apply, skipped` | Either genuinely an external-apply job, or LinkedIn changed its markup — check `_apply_linkedin` in `tools/apply.py` |
| LinkedIn: `Easy Apply modal did not open in time` | Page rendered slowly, or the apply-flow container changed; retry first (failed jobs are retryable) |
| `BrowserType.launch_persistent_context: Timeout` | Stale lock from a browser that didn't close cleanly — delete `~/.job-apply-mcp/browser-profiles/linkedin/parent.lock` |
| Few LinkedIn candidates despite many results | Expected — LinkedIn serves ~11 cards per search and the title gate is strict. Use Naukri for volume |

---

## Contributing

PRs welcome! Common improvements:
- Add new platforms (Foundit, Shine, etc.)
- Improve form-fill heuristics for specific job sites
- Add support for cover-letter generation
- Improve match scoring algorithm

---

## Disclaimer

- This tool **automates form submission**. Always **review the jobs being applied to** before bulk-applying.
- Excessive use may trigger rate limits or temporary account locks on job portals.
- Use responsibly — don't spam recruiters with low-quality applications.
- This is for **personal job search use only**. Don't use it commercially or to harvest job data.
