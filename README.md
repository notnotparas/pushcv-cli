# pushcv

**Git for job hunting** — a local-first, privacy-focused CLI that tracks your job
applications, scrapes postings, tailors your resume with a *local* AI model, and
estimates compensation — all from your terminal. No account, no cloud, no data
leaving your machine.

Built with [Typer](https://typer.tiangolo.com/) · [Rich](https://rich.readthedocs.io/)
· [SQLModel](https://sqlmodel.tiangolo.com/) on a local SQLite database.

> **Local-first by design.** Your applications live in a single SQLite file on
> your disk. Resume tailoring runs on a model on *your* laptop. The only network
> calls are (1) scraping a posting you explicitly point it at and (2) an optional
> web search for salary data — both off by default until you ask.

---

## Features

- 📋 **Track applications** on a Rich **Kanban board** in your terminal
  (Drafting → Applied → Interviewing → Closed).
- ⏱ **Follow-up nudges** — pushcv records when you apply and flags stale
  applications right on the board ("applied 15d ago — follow up?"). Keep a
  dated timeline per job with `pushcv note`.
- 🔎 **Scrape LinkedIn postings** with one command — TLS/browser impersonation
  (via `curl_cffi`) reaches the public guest view even when the site fights back.
- 💰 **Salary estimates** *(experimental)* grounded in live web data
  (DuckDuckGo), with an optional local-AI synthesis pass for a tighter,
  role-anchored range.
- ✍️ **Tailor your resume — and cover letter** — to any tracked job using a
  **local** LLM (via [LiteLLM](https://github.com/BerriAI/litellm) → an
  OpenAI-compatible server such as
  [Lemonade](https://github.com/lemonade-sdk/lemonade)). No API keys, no cost,
  no data sent anywhere.
- 🔒 **Private by default** — one local SQLite DB, no telemetry, no accounts.
- 📦 **Your data is yours** — export everything to JSON or CSV anytime with
  `pushcv export`.

## Requirements

- **Python ≥ 3.10**
- *(Optional, for AI features)* a local OpenAI-compatible inference server —
  e.g. [Lemonade](https://github.com/lemonade-sdk/lemonade) — serving a chat
  model. Core tracking works without any of this.

## Installation

The recommended way is [pipx](https://pipx.pypa.io/) — it installs the CLI in an
isolated environment and puts `pushcv` on your PATH. Install straight from the
repo:

```bash
pipx install git+https://github.com/notnotparas/pushcv-cli.git
```

Or with pip:

```bash
pip install git+https://github.com/notnotparas/pushcv-cli.git
```

<details>
<summary>From source (development)</summary>

```bash
git clone https://github.com/notnotparas/pushcv-cli.git
cd pushcv-cli
python3 -m venv .venv && source .venv/bin/activate
pip install -e .        # wires up the global `pushcv` command
```
</details>

## Quick start

```bash
pushcv init                                   # create ./pushcv.db + ./profile.md
# → fill in profile.md (your name, experience, skills) before drafting
pushcv add "Acme Corp" "Senior Engineer"      # track a job manually
pushcv fetch "https://www.linkedin.com/jobs/view/<id>/"   # …or scrape one
pushcv status                                 # see your pipeline (Kanban board)
pushcv draft 1                                # tailor a resume for job #1
pushcv move 1 applied                         # advance it on the board
pushcv note 1 "recruiter call Friday 3pm"     # keep a dated timeline
pushcv show 1                                 # full details for one job
```

Everything is written to the current working directory, so keep a dedicated
folder (e.g. `~/job-hunt/`) and run `pushcv` from there.

> **Fill in `profile.md` first.** It's your master profile — name, experience,
> skills, and projects — and the source of truth the AI uses to tailor resumes
> and cover letters. The generated template starts with your name so drafts
> sign off correctly; the prompts never invent facts, so anything you leave
> blank simply won't appear.

## Commands

| Command | What it does |
|---------|--------------|
| `pushcv init` | Create the local `pushcv.db` and a `profile.md` template. |
| `pushcv add <company> <title> [--url]` | Add a job manually (starts in *Drafting*). |
| `pushcv fetch <url> [--save] [--debug]` | Scrape a LinkedIn posting; preview, then confirm to save. `--save` skips the prompt; `--debug` dumps raw HTML for troubleshooting. |
| `pushcv status` | Render the Kanban board. Backfills any missing salary estimates. |
| `pushcv move <n> <status>` | Move the job at position `n` to a new status — a column (`drafting`, `applied`, `interviewing`, `closed`) or a synonym (`offer`, `rejected`, `onsite`, `ghosted`, …). |
| `pushcv show <n>` | Show everything stored for the job at position `n` — status, dates, notes, and the full scraped description. |
| `pushcv note <n> "text"` | Append a dated note to the job's timeline (shown in `show`). |
| `pushcv export [-f json\|csv] [-o file]` | Export all tracked jobs. Prints to stdout by default (pipe-friendly); `-o` writes a file. |
| `pushcv draft <n> [--model] [--cover-letter]` | Generate a tailored, ATS-optimized Markdown resume for the job at board position `n`, saved to `drafts/`. Sets status → *ready to apply*. With `--cover-letter`/`-c`, drafts a short tailored cover letter instead (status unchanged). |
| `pushcv delete <n> [--yes]` | Remove the job at position `n` (and its draft). Confirms first; `--yes` skips. |

> **Positions, not IDs.** `move`, `show`, `note`, `draft`, and `delete` take the **position number**
> (`[1]`, `[2]`, …) shown on the `status` board — not raw database IDs — so
> there are never confusing gaps after a deletion. `delete` always shows the
> company/title and asks before removing.

## Resume & cover letter tailoring (AI setup)

`pushcv draft` (resume or `--cover-letter`) and, optionally, salary synthesis
use a **local** language model through LiteLLM, pointed at an OpenAI-compatible
endpoint:

- **Endpoint:** `http://localhost:13305/v1` (Lemonade's default)
- **Default model:** `Qwen3-8B-GGUF` — override per command with `--model`, or
  change `DEFAULT_AI_MODEL` in `main.py`.

Start your local server (e.g. Lemonade), load a chat model, then:

```bash
pushcv draft 1 --model Qwen3-8B-GGUF     # tailored, ATS-optimized resume
pushcv draft 1 --cover-letter            # short tailored cover letter
```

Both are grounded strictly in your `profile.md` — the prompts forbid inventing
employers, dates, or skills. If the server isn't running, `draft` fails
gracefully with a clear message and does **not** corrupt your data. Nothing is
ever sent to a remote provider.

## Salary estimation (experimental)

> ⚠️ **Experimental.** Estimates come from live public web data and can be
> noisy, stale, or wrong for niche roles and smaller companies. Treat them as
> a triage signal, never as an offer benchmark.

When you add or fetch a job, pushcv asks **once** whether to enable AI salary
estimates (the choice is remembered in `.pushcv.json`):

- **Web extraction (default):** parses figures from reputable salary sites
  (levels.fyi, Glassdoor, AmbitionBox, Payscale, …) and cites the source, e.g.
  `💰 ₹27L - ₹35L · per ambitionbox.com`. No model required.
- **AI synthesis (opt-in):** the local model cleans the web data into a tighter,
  role-anchored range (using the posting's seniority and your years of
  experience from `profile.md`).

Estimates are a **ballpark**, not a quote — they vary with the live search
results. The cited band is the signal, not the exact digits. Currency is
inferred from the job's location (INR, USD, GBP, EUR, …).

## Data model

A single `job_application` table (local SQLite, `pushcv.db`):

| Field | Type | Notes |
|-------|------|-------|
| `id` | INTEGER | Primary key, auto-incrementing (internal). |
| `company` | VARCHAR | Required. |
| `title` | VARCHAR | Required. |
| `url` | TEXT | Posting link (optional). |
| `location` | TEXT | From `fetch` (optional). |
| `description` | TEXT | Scraped job description (optional). |
| `salary_estimate` | VARCHAR | Web/AI compensation estimate (optional). |
| `status` | VARCHAR | Pipeline state; defaults to `drafting`. |
| `created_at` | TIMESTAMP | UTC creation time. |
| `applied_at` | TIMESTAMP | When the job first moved to *Applied* (drives the follow-up nudge). |
| `notes` | TEXT | Dated timeline lines from `pushcv note` (optional). |

New columns are auto-migrated on startup, so upgrading pushcv never breaks an
existing database.

## Configuration & files

Everything pushcv writes lives in your working directory:

| Path | Contents |
|------|----------|
| `pushcv.db` | Your applications (SQLite). |
| `profile.md` | Your master profile — the source of truth for resume tailoring. |
| `.pushcv.json` | Per-workspace preferences (e.g. AI salary toggle). |
| `drafts/` | Generated resume Markdown files. |

All of these are git-ignored by default — they're personal and never meant to be
committed.

## Privacy & responsible use

- **No telemetry, no accounts, no cloud.** Your data stays on your machine.
- The scraper is for **personal use** on postings you're applying to. Respect the
  target site's Terms of Service and rate limits; don't hammer endpoints.
- Salary numbers are estimates aggregated from public web data — verify against
  the cited sources before relying on them.

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
pushcv --help
```

Project layout (src/ layout):

```text
pushcv-cli/
├── pyproject.toml        # PEP 621 metadata, pinned deps, `pushcv` entry point
├── README.md · LICENSE · .gitignore
└── src/pushcv/
    ├── __init__.py       # version
    ├── main.py           # Typer app, DB engine, all commands, Kanban UI
    ├── models.py         # SQLModel table (JobApplication)
    ├── scraper.py        # LinkedIn fetch/parse (curl_cffi + BeautifulSoup)
    ├── search.py         # DuckDuckGo salary search + extraction
    ├── ai_engine.py      # LiteLLM → local model (resume + salary synthesis)
    └── config.py         # per-workspace preferences (.pushcv.json)
```

Contributions welcome — please open an issue to discuss substantial changes
first. (Tests are a great first contribution; see "Roadmap" below.)

## Roadmap — contributions welcome!

These are scoped to be approachable first PRs; open an issue to claim one:

- **More job boards for `fetch`** — Greenhouse and Lever first: both expose
  clean public JSON APIs (`boards-api.greenhouse.io`, `api.lever.co`), far
  friendlier than LinkedIn, and they're where most external apply links land
  anyway. A fetcher just needs to return the same dict shape as
  `fetch_linkedin_job` in [scraper.py](src/pushcv/scraper.py).
- **Unit tests** for the pure helpers — salary parsing (`search.py`), LinkedIn
  URL normalization (`scraper.py`), currency inference (`ai_engine.py`).
- **Optional dependency extras** (`pushcv[ai]`) so a minimal install doesn't
  pull the LLM stack.
- **PDF export** for drafted resumes/cover letters (e.g. via pandoc or typst).

## License

[MIT](./LICENSE) © pushcv contributors
