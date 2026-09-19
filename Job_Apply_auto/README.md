# AI Job Application Platform

One system that finds jobs across portals and company career pages, scores
them against your real profile, writes a truthful job-specific resume when
that genuinely helps, prepares the application, and **stops and asks you**
whenever it does not know something.

It will not answer a question on your behalf that your own data does not
answer, and it will not put a skill on your resume that your profile does not
support. Those are enforced structurally, not by convention — see
[Truthfulness](#truthfulness).

---

## Contents

- [What it does](#what-it-does)
- [Truthfulness](#truthfulness)
- [Architecture](#architecture)
- [Installation](#installation)
- [Getting started](#getting-started)
- [Configuration](#configuration)
- [Your profile](#your-profile)
- [Job sources](#job-sources)
- [Company career pages](#company-career-pages)
- [Resume generation](#resume-generation)
- [Human-in-the-loop](#human-in-the-loop)
- [Google Sheets and Gmail](#google-sheets-and-gmail)
- [Interfaces](#interfaces)
- [Database](#database)
- [Docker](#docker)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Security](#security)
- [Limitations](#limitations)

---

## What it does

```
your profile + preferences
        │
        ▼
  job discovery ──── 8 portals + any company career page
        │
        ▼
  normalize & deduplicate ──── one job, however many places it was posted
        │
        ▼
  JD analysis ──── skills, hard requirements, nice-to-haves
        │
        ▼
  AI matching ──── score + the reasoning behind it
        │
        ├── below threshold ──▶ rejected, with the reason recorded
        ▼
  resume decision ──── tailor only if there is something truthful to surface
        │
        ▼
  application engine
        │
        ├── question it can answer from your data ──▶ answers it
        ├── question it cannot ────────────────────▶ PAUSES, asks you
        ├── sensitive question ────────────────────▶ PAUSES, always
        ▼
  submit ──── only when AUTO_APPLY is on and DRY_RUN is off
        │
        ▼
  tracking ──── database, Google Sheets, Gmail status updates, dashboard
```

---

## Truthfulness

Two guarantees, both enforced by structure rather than by asking a model to
behave.

**Your resume cannot claim something your profile does not support.**
`app/resume/gap_analysis.py` classifies every keyword a job asks for:

| Verdict | Meaning | What the resume does |
|---|---|---|
| `SUPPORTED_BUT_ABSENT` | Your profile proves it; the resume omits it | Surfaces it |
| `UNDER_REPRESENTED` | On the resume, but buried | Promotes it |
| `WELL_REPRESENTED` | Already stated clearly | Nothing |
| `TRANSFERABLE` | You have an adjacent skill | States only the **real** skill |
| `ABSENT` | Nothing supports it | **Never appears** |

The generator can only use what this module cleared, every cleared keyword is
stored with the evidence that cleared it, and the validator then re-reads the
finished document and **fails the resume** if an `ABSENT` or `TRANSFERABLE`
keyword appears. A failed resume is never attached to an application.

So a candidate with GCP applying to an AWS job gets a resume saying "GCP" —
never "AWS", however much the job wants it.

**No question is answered by guessing.** `KnowledgeBase.resolve()` returns
either an answer or a reason to stop. There is no default and no third
outcome, so a caller has nothing to fall through to. Anything sensitive —
work authorization, sponsorship, disability, veteran status, race, gender,
criminal history, salary, relocation, legal declarations, age — goes to you
**even when a confident stored answer exists**.

---

## Architecture

```
  API (FastAPI)  ┐
  CLI (typer)    ├──▶  shared service layer  ──▶  Playwright  ──▶  portals
  MCP server     ┘            │                                    career pages
  dashboard                   │
                              ├──▶ SQLite / Postgres   (source of truth)
                              ├──▶ ChromaDB + Ollama   (embeddings, optional)
                              ├──▶ Google Sheets       (reporting only)
                              └──▶ Gmail               (read-only)
```

| Module | Responsibility |
|---|---|
| `app/sources/` | Job discovery. One `JobSourceAdapter` per portal or ATS |
| `app/analysis/` | JD parsing: skills, requirements, responsibilities |
| `app/matching/` | Scoring a job against the profile across six dimensions |
| `app/resume/` | Gap analysis, LaTeX generation, compilation, validation |
| `app/qa/` | Question knowledge base and sensitive-question policy |
| `app/engine/` | The application engine and its safety gates |
| `app/services/` | Orchestration the API, CLI and MCP all share |
| `app/integrations/` | Google Sheets and Gmail |
| `app/api/`, `app/cli.py`, `app/mcp_server.py` | Front ends. No logic |
| `app/scheduler.py` | Timed loops and crash recovery |

Business logic lives only in the service layer, so a rule enforced in the
engine cannot be sidestepped by using a different front end.

---

## Installation

Requires **Python 3.11+**.

```bash
git clone https://github.com/PruthviRaj3009/Job_Apply_auto.git
cd Job_Apply_auto

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -e ".[dev]"
playwright install firefox
```

Two optional pieces, both degrade gracefully:

- **LaTeX** — needed for PDF resumes. Without it the `.tex` is still
  generated and validated, and you are told no PDF was produced.
  - Debian/Ubuntu: `sudo apt install texlive-latex-recommended texlive-fonts-recommended lmodern`
  - macOS: `brew install --cask basictex`
  - Windows: [MiKTeX](https://miktex.org/download)
- **Ollama** — adds semantic matching and semantic question lookup. Without
  it, matching runs on rules and says so in `signals_used`.
  ```bash
  ollama pull mxbai-embed-large && ollama pull phi3
  ```

---

## Getting started

```bash
cp .env.example .env            # review it; the defaults are safe
jobpilot init                   # create the database

jobpilot profile my_profile.json   # load your master profile
jobpilot resume master             # generate the baseline resume

jobpilot login linkedin         # log in yourself; the session is saved
jobpilot discover               # find jobs
jobpilot analyze                # score them
jobpilot apply                  # prepare applications (submits nothing yet)

jobpilot status                 # where everything stands
jobpilot questions list         # what needs your answer
jobpilot questions answer 3 "No"
```

Nothing is submitted until you explicitly set both:

```bash
DRY_RUN=false
AUTO_APPLY=true
```

Watch a few dry runs first. `jobpilot apply` with the defaults produces the
tailored resume and shows you exactly which questions a real run would ask.

---

## Configuration

Everything is environment-driven; `.env.example` documents each setting. The
ones that matter most:

| Variable | Default | Effect |
|---|---|---|
| `DRY_RUN` | `true` | While true, nothing is ever submitted |
| `AUTO_APPLY` | `false` | Must be true before anything is submitted |
| `MIN_MATCH_SCORE` | `0.70` | Below this, a job is rejected |
| `ANSWER_CONFIDENCE_THRESHOLD` | `0.82` | Below this, you are asked instead |
| `MAX_APPLICATIONS_PER_COMPANY` | `2` | Per `COMPANY_WINDOW_DAYS` |
| `DATABASE_URL` | SQLite | Set a `postgresql+psycopg://` DSN to share |
| `API_KEY` | empty | Required if `API_HOST` is not loopback |

Binding the API to a non-loopback interface without an API key **fails at
startup** rather than serving an unauthenticated service that can act as you.

---

## Your profile

The profile is the single source of truth about you. Nothing else in the
system may assert anything it does not contain.

```json
{
  "full_name": "Your Name",
  "email": "you@example.com",
  "phone": "+91 90000 00000",
  "location": "Pune, India",
  "total_experience_years": 4,
  "github_url": "https://github.com/you",
  "linkedin_url": "https://linkedin.com/in/you",
  "target_roles": ["DevOps Engineer", "Platform Engineer"],
  "preferred_locations": ["Pune", "Remote"],
  "excluded_keywords": ["helpdesk", "desktop support"],
  "excluded_companies": ["Some Agency"],
  "skills": [
    {
      "name": "Kubernetes",
      "years": 3,
      "aliases": ["k8s", "EKS", "GKE"],
      "evidence": "Ran production GKE clusters at Acme"
    }
  ],
  "experience": [
    {
      "company": "Acme Corp",
      "title": "DevOps Engineer",
      "start_date": "2022-01",
      "is_current": true,
      "bullets": ["Ran production GKE clusters serving 40M requests/day"],
      "technologies": ["Kubernetes", "Terraform", "GCP"]
    }
  ],
  "education": [
    {
      "institution": "Pune University",
      "degree": "Bachelor of Engineering",
      "field_of_study": "Computer Science",
      "end_date": "2020-05"
    }
  ],
  "projects": [
    {
      "name": "Cluster Autoscaler",
      "technologies": ["Go", "Kubernetes"],
      "repo_url": "https://github.com/you/autoscaler",
      "bullets": ["Reduced idle node cost by 35%"]
    }
  ],
  "certifications": [
    { "name": "CKA", "issuer": "CNCF", "issued_date": "2023-03" }
  ]
}
```

Write `evidence` on your skills. It is what lets a skill be surfaced on a
tailored resume, and it is what you would point at if an interviewer asked.

Reloading **replaces** the profile rather than merging, so removing a skill
from the file removes it everywhere.

---

## Job sources

LinkedIn, Naukri, Indeed, Wellfound, Hirist, Glassdoor, Instahyre, Cutshort.

```bash
jobpilot discover --source naukri --source linkedin
```

Sessions are persistent browser profiles, not cookie jars — LinkedIn ties a
session to browser fingerprint and local storage, so a cookie replay logs
straight back out. Log in once with `jobpilot login <source>`; it survives
restarts.

Adding a source means subclassing `JobSourceAdapter` and calling `@register`.
Nothing else changes.

---

## Company career pages

Almost no company writes its own careers site. Paste a careers URL and the
platform identifies the ATS behind it:

```python
from app.sources import detect_ats
detect_ats("https://boards.greenhouse.io/acmecorp")   # ('greenhouse', 'acmecorp')
```

Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee and Workable are
supported, which covers a very large share of employers. These are also the
best source for *new* postings, because their APIs return the whole board
every time.

---

## Resume generation

```bash
jobpilot resume master           # baseline
jobpilot resume for-job 42       # tailored, if tailoring is warranted
jobpilot resume list
```

Templates live in `resume_templates/` — `base_resume.tex` plus
`styles/ats.sty`. The style is single-column and table-free because
multi-column layouts are the most common reason an ATS scrambles a resume,
and links are rendered into the text layer because most parsers read text and
ignore link annotations.

Every generated resume records what it emphasized, **what it omitted and
why**, and the profile evidence behind each emphasized keyword.

If tailoring is not warranted — your master resume already covers what the
job asks for — you are told so and the master is used. That is the correct
outcome, not a failure.

---

## Human-in-the-loop

When the engine meets a question it cannot answer from your data, it stops.

```bash
$ jobpilot questions list

  [3] Do you hold a valid driving licence?
  [4] Are you legally authorized to work in the US?  SENSITIVE
        suggestion: Yes

$ jobpilot questions answer 3 "Yes"
Stored.
Job 17 released back into the pipeline.
```

Your answer is stored and semantically indexed, so the same question phrased
differently on another portal will not stop you again. Sensitive questions
still ask every time — that is deliberate.

---

## Google Sheets and Gmail

**Sheets** is a reporting layer. The database is the source of truth and
nothing is ever read back, so editing a cell cannot change application state.
Rows are keyed per attempt and updated in place.

```bash
GOOGLE_SHEET_ID=<id> jobpilot sync
```

**Gmail** is read-only (`gmail.readonly`). It classifies recruiter mail and
updates the matching application — interview invitations, rejections, offers —
so the dashboard reflects reality without you forwarding anything. Nothing is
sent, deleted or marked read. An application's status never walks backwards.

```bash
jobpilot inbox --days 7
```

Both need OAuth credentials at `.credentials/client_secret.json`; the first
run opens a consent flow.

---

## Interfaces

**API** — `jobpilot serve`, then `http://127.0.0.1:8000/docs`.

```
GET  /api/jobs                        POST /api/jobs/search
GET  /api/jobs/{id}                   POST /api/jobs/{id}/analyze
GET  /api/jobs/{id}/match
GET  /api/applications                GET  /api/applications/{id}
POST /api/applications/{id}/apply     POST /api/applications/{id}/retry
GET  /api/questions                   GET  /api/questions/pending
POST /api/questions/{id}/answer
GET  /api/resume                      GET  /api/resume/{id}
GET  /api/profile                     GET  /api/settings
GET  /api/dashboard                   GET  /api/analytics
```

The API prepares applications but never submits them — routing submission
through HTTP would invite a client to drive it past the safety gates.

**MCP** — point an assistant at `python -m app.mcp_server`:

```json
{
  "mcpServers": {
    "jobpilot": {
      "command": "python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "/path/to/Job_Apply_auto"
    }
  }
}
```

There is deliberately no tool that submits an application.

---

## Database

16 tables. `jobs` are keyed by a **fingerprint** over normalized company,
title and location rather than by URL, because portals rewrite URLs and
repost roles — so the same job found in three places is one row, while the
same role in two cities stays two jobs.

`application_events` is append-only. Every status change goes through
`move_job()`, which validates the transition and writes the event in one
transaction — which is what makes a crashed run resumable.

```bash
alembic upgrade head        # apply migrations
jobpilot recover            # rescue jobs from an interrupted run
```

---

## Docker

```bash
cp .env.example .env        # set POSTGRES_PASSWORD and API_KEY
docker compose up -d
```

Three services: Postgres, the API, and the scheduler. API and scheduler are
separate because their failure modes differ — a browser hang in discovery
should not take the dashboard down with it. The image includes Firefox and a
minimal TeX Live.

---

## Testing

```bash
pytest                      # ~390 tests, no network, no real applications
pytest -m latex             # only if pdflatex is installed
```

No test ever submits a real application. Scrapers are tested against recorded
fixtures, Google integrations against in-memory fakes, and the browser is
never launched.

---

## Troubleshooting

**"No profile configured"** — run `jobpilot profile <file.json>`. Matching and
resume generation both read from it.

**A source returns nothing** — its session probably expired. Run
`jobpilot login <source>`. Portals also change their markup; run
`pytest tests/test_sources.py` to see whether parsing or fetching broke.

**"pdflatex not installed"** — expected without LaTeX. The `.tex` is still
produced and validated; install a TeX distribution for PDFs.

**A CAPTCHA or MFA prompt** — the run stops and tells you. Complete it
yourself with `jobpilot login <source>`. This is never bypassed.

**Jobs stuck in `ANALYZING` or `APPLYING`** — a previous run was interrupted.
`jobpilot recover` returns them to a runnable state; the scheduler does this
on startup too.

**Nothing is being submitted** — check `jobpilot status`. With the shipped
defaults, that is correct behaviour.

**Matching seems crude** — Ollama is probably not running, so semantic scoring
is unavailable. Check `signals_used` in a job's match detail.

---

## Security

- Portal credentials are read from the environment only. Nothing writes them
  to disk.
- The Google token is stored `chmod 600`; Gmail scope is read-only.
- The API refuses to bind a public interface without a key, and compares keys
  in constant time.
- CORS defaults to an explicit localhost origin, not `*`.
- Secrets, local state, user data and browser profiles are all gitignored.
- CAPTCHA, OTP and MFA are never bypassed — the run pauses and hands over.

Do not commit `.env`, `.credentials/`, `data/`, or your profile JSON.

---

## Limitations

- **Portal scraping is brittle by nature.** Sites change markup without
  notice. The parsers are fixture-tested so breakage is diagnosable, but
  expect to update selectors.
- **Respect each site's terms.** Several portals prohibit automated access.
  You are responsible for what you run against them.
- **PDF text extraction is best-effort.** Compressed PDFs may yield nothing;
  that is reported as a warning, never used to fail a resume.
- **Cover letters are modelled and versioned but not yet generated.**
- **Single-user.** The schema supports one profile; multi-tenant use would
  need per-user scoping throughout.
- **No frontend ships yet.** The API serves the dashboard's data
  (`/api/dashboard`, `/api/analytics`); the UI itself is not built.
- **Gmail classification is pattern-based.** It is conservative — mail it
  cannot classify is left alone rather than guessed at.
