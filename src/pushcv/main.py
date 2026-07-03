"""pushcv CLI entry point.

Configures the global Typer application, the thread-safe local-first SQLite
engine, and the interactive commands (init / add / status) with a high-fidelity
Rich text user interface.
"""
import csv
import io
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

import typer
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text
from sqlalchemy import inspect as sa_inspect, text as sa_text
from sqlmodel import Session, SQLModel, create_engine, select

# Importing the models registers JobApplication on SQLModel.metadata so that
# create_all() knows about every table.
from pushcv import models  # noqa: F401  (imported for metadata registration)
from pushcv.models import JobApplication
from pushcv.scraper import fetch_linkedin_debug, fetch_linkedin_job
from pushcv.ai_engine import (
    currency_for_location,
    estimate_compensation,
    generate_cover_letter,
    generate_tailored_resume,
)
from pushcv.search import extract_salary, get_salary_snippets
from pushcv import config

# --------------------------------------------------------------------------- #
# Paths & database engine
# --------------------------------------------------------------------------- #
# Local-first: the SQLite file and profile template live next to wherever the
# user runs pushcv.
DB_PATH = Path("pushcv.db")
PROFILE_PATH = Path("profile.md")
DRAFTS_DIR = Path("drafts")
DATABASE_URL = f"sqlite:///{DB_PATH}"

# Default local model id (must match a model loaded in the Lemonade server).
DEFAULT_AI_MODEL = "Qwen3-8B-GGUF"

# check_same_thread=False makes the connection usable across threads, which is
# required for a CLI that may dispatch work onto worker/background threads.
engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    """Create the database file and all tables if they do not yet exist.

    Idempotent: ``create_all`` only emits ``CREATE TABLE`` for missing tables.
    A lightweight column migration then backfills any columns added to the model
    after a database was first created (``create_all`` never ALTERs).
    """
    SQLModel.metadata.create_all(engine)
    _migrate_columns()


def _migrate_columns() -> None:
    """Add model columns missing from an existing job_application table.

    Keeps older local databases compatible without a migration framework: each
    nullable column added to :class:`JobApplication` is appended on demand.
    """
    table = JobApplication.__tablename__
    inspector = sa_inspect(engine)
    if table not in inspector.get_table_names():
        return  # create_all will have made it with every column.

    existing = {col["name"] for col in inspector.get_columns(table)}
    # column name -> SQLite type for any nullable columns added post-creation.
    additions = {
        "location": "TEXT",
        "description": "TEXT",
        "salary_estimate": "VARCHAR",
        "applied_at": "TIMESTAMP",
        "notes": "TEXT",
    }
    with engine.begin() as conn:
        for name, sql_type in additions.items():
            if name not in existing:
                conn.execute(sa_text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))


def get_session() -> Session:
    """Return a new SQLModel Session bound to the global engine."""
    return Session(engine)


# --------------------------------------------------------------------------- #
# Positional addressing
# --------------------------------------------------------------------------- #
# Users reference jobs by the 1..N position shown in `pushcv status`, not by the
# raw (gappy) database id. Position is the rank in a single canonical ordering,
# computed here so the board and every command stay in agreement.
def _ordered_job_ids() -> List[int]:
    """Return all job database ids in the canonical display order (1..N)."""
    with get_session() as session:
        jobs = session.exec(
            select(JobApplication).order_by(
                JobApplication.created_at, JobApplication.id
            )
        ).all()
    return [job.id for job in jobs]


def _resolve_position(position: int) -> Optional[int]:
    """Map a 1-based display position to its underlying database id.

    Returns ``None`` if the position is out of range.
    """
    ids = _ordered_job_ids()
    if 1 <= position <= len(ids):
        return ids[position - 1]
    return None


def _invalid_position(position: int) -> "typer.Exit":
    """Print a 'no job at that position' error panel and return an Exit(1)."""
    console.print(
        Panel(
            Text(
                f"No job at position {position}.\n"
                "Run 'pushcv status' to see your jobs and their position numbers.",
                style="white",
            ),
            title="[bold red]✗ Invalid position[/]",
            title_align="left",
            border_style="red",
            box=box.ROUNDED,
            padding=(1, 2),
        )
    )
    return typer.Exit(code=1)


# --------------------------------------------------------------------------- #
# Kanban pipeline configuration
# --------------------------------------------------------------------------- #
# Ordered Kanban columns: (canonical key, display label, accent colour).
STATUS_COLUMNS = [
    ("drafting", "Drafting", "yellow"),
    ("applied", "Applied", "cyan"),
    ("interviewing", "Interviewing", "magenta"),
    ("closed", "Closed", "red"),
]

# Map free-text status values onto canonical Kanban columns so the board stays
# robust to synonyms and minor variations.
STATUS_ALIASES = {
    "drafting": "drafting",
    "draft": "drafting",
    # A drafted resume is prepared but not yet submitted — still pre-application.
    "ready_to_apply": "drafting",
    "ready": "drafting",
    "applied": "applied",
    "submitted": "applied",
    "interviewing": "interviewing",
    "interview": "interviewing",
    "screen": "interviewing",
    "onsite": "interviewing",
    "closed": "closed",
    "rejected": "closed",
    "offer": "closed",
    "accepted": "closed",
    "declined": "closed",
    "ghosted": "closed",
}

# Per-column symbol used as the explicit highlight designator on every card.
STATUS_SYMBOL = {
    "drafting": "✎",
    "applied": "➤",
    "interviewing": "★",
    "closed": "⏹",
}


def _bucket(status: str) -> str:
    """Resolve an arbitrary status string to a canonical Kanban column key."""
    key = (status or "").strip().lower()
    return STATUS_ALIASES.get(key, "closed")


def _link(url: str, label: Optional[str] = None) -> Text:
    """Build a clickable terminal hyperlink (OSC 8).

    The *full* ``url`` is always the click target regardless of the visible
    ``label`` — so a truncated/short display still opens the complete link in
    terminals that support OSC 8 hyperlinks (VS Code, iTerm2, GNOME Terminal,
    Windows Terminal, etc.).
    """
    return Text(label or url, style=Style(color="blue", underline=True, link=url))


# --------------------------------------------------------------------------- #
# Profile template
# --------------------------------------------------------------------------- #
PROFILE_TEMPLATE = """\
# pushcv Profile

> Your master profile. pushcv uses these sections as the source of truth when
> tailoring applications. Fill them in once, reuse everywhere.

## Name

<!-- Your full name — used to sign resumes and cover letters. -->

Your Name

## Contact

<!-- Optional: email, phone, location, links (LinkedIn, GitHub, portfolio). -->

- **Email:**
- **Location:**
- **Links:**

## Experience

<!-- List your roles, most recent first. -->

- **Company** — _Title_ (Start – End)
  - Impact-focused highlight (what you did, the result, the number).

## Tech Stack

<!-- The languages, frameworks, and tools you work with. -->

- **Languages:**
- **Frameworks:**
- **Tools & Platforms:**

## Projects

<!-- Notable projects worth showcasing. -->

- **Project name** — one-line description.
  - Link:
"""


def _write_profile_template() -> bool:
    """Create profile.md from the template if it does not already exist.

    Returns True if the file was created, False if it already existed.
    """
    if PROFILE_PATH.exists():
        return False
    PROFILE_PATH.write_text(PROFILE_TEMPLATE, encoding="utf-8")
    return True


# --------------------------------------------------------------------------- #
# Typer application
# --------------------------------------------------------------------------- #
app = typer.Typer(
    name="pushcv",
    help="pushcv — a local-first CLI to track your job applications.",
    no_args_is_help=True,
    add_completion=True,
)

console = Console()


@app.callback()
def main() -> None:
    """pushcv — track your job applications from the terminal."""
    # No side effects here: each command manages the database itself so that
    # `init` can accurately detect a first-time setup.


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #
@app.command()
def init() -> None:
    """Initialize the local pushcv database and profile template."""
    db_existed = DB_PATH.exists()

    # Generate all SQLModel metadata structures (idempotent).
    init_db()

    # Drop a base profile.md template alongside the database.
    profile_created = _write_profile_template()

    # ----- success banner ----- #
    db_line = Text()
    db_line.append("  database  ", style="bold")
    if db_existed:
        db_line.append("already present  ", style="yellow")
    else:
        db_line.append("created  ", style="green")
    db_line.append(str(DB_PATH.resolve()), style="cyan")

    profile_line = Text()
    profile_line.append("  profile   ", style="bold")
    if profile_created:
        profile_line.append("created  ", style="green")
    else:
        profile_line.append("already present  ", style="yellow")
    profile_line.append(str(PROFILE_PATH.resolve()), style="cyan")

    hint = Text("\nNext: ", style="dim")
    hint.append("pushcv add \"Company\" \"Title\" --url <link>", style="bold white")

    banner = Panel(
        Group(
            Text("pushcv is ready 🚀", style="bold green"),
            Text(""),
            db_line,
            profile_line,
            hint,
        ),
        title="[bold green]✓ Initialization complete[/]",
        title_align="left",
        border_style="green",
        box=box.DOUBLE,
        padding=(1, 2),
    )
    console.print(banner)


# --------------------------------------------------------------------------- #
# add
# --------------------------------------------------------------------------- #
@app.command()
def add(
    company: str = typer.Argument(..., help="Company offering the role."),
    title: str = typer.Argument(..., help="Job title you're applying for."),
    url: Optional[str] = typer.Option(
        None, "--url", "-u", help="Link to the job posting."
    ),
) -> None:
    """Add a new job application to the pipeline (starts in Drafting)."""
    # Ensure the schema exists even if the user skipped `init`.
    init_db()

    job = JobApplication(company=company, title=title, url=url)
    with get_session() as session:
        session.add(job)
        session.commit()
        session.refresh(job)

    # Newly added jobs sort last, so their display position is the current count.
    position = len(_ordered_job_ids())

    body = Text()
    body.append("✎ ", style="bold yellow")  # drafting designator
    body.append(f"[{position}]  ", style="bold yellow")
    body.append(job.company, style="bold white")
    body.append("  ·  ", style="dim")
    body.append(job.title, style="white")
    if job.url:
        body.append("\n   ", style="dim")
        body.append(job.url, style=Style(color="blue", underline=True, link=job.url))
    body.append("\n   status: ", style="dim")
    body.append(job.status, style="bold yellow")

    console.print(
        Panel(
            body,
            title="[bold green]＋ Application added[/]",
            title_align="left",
            border_style="green",
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )

    # First time only: decide how salary estimates are produced (shown on status).
    _ensure_ai_salary_preference()


# --------------------------------------------------------------------------- #
# fetch (scrape a LinkedIn posting)
# --------------------------------------------------------------------------- #
def _fetch_debug(url: str) -> None:
    """Dump raw LinkedIn HTML and report where apply URLs were (or weren't) found."""
    try:
        report = fetch_linkedin_debug(url)
    except ValueError as exc:
        console.print(f"[bold red]✗[/] {exc}")
        raise typer.Exit(code=1)

    for entry in report:
        console.print(f"\n[bold cyan]── {entry['source']} ──[/]")
        console.print(f"  url:    [dim]{entry['url']}[/]")
        if entry.get("error"):
            console.print(f"  [red]error:[/] {entry['error']}")
            continue
        console.print(f"  status: {entry['status']}   length: {entry['length']} bytes")
        console.print(f"  code#applyUrl:   {entry['code_apply_url'] or '—'}")
        console.print(f"  json model URL:  {entry['json_apply_url'] or '—'}")
        console.print(f"  selector URL:    {entry['selector_apply_url'] or '—'}")
        hrefs = entry["external_hrefs"]
        console.print(f"  external <a> hrefs ({len(hrefs)}):")
        for href in hrefs[:15]:
            console.print(f"    • [blue]{href}[/]")
        if len(hrefs) > 15:
            console.print(f"    [dim]… {len(hrefs) - 15} more[/]")

        # Persist the raw HTML so selectors can be checked against real markup.
        out = Path(f"linkedin_{entry['source']}.html")
        out.write_text(entry["html"], encoding="utf-8")
        console.print(f"  [green]raw HTML →[/] {out.resolve()}")


@app.command()
def fetch(
    url: str = typer.Argument(..., help="LinkedIn job URL (any form)."),
    save: bool = typer.Option(
        False,
        "--save",
        "-s",
        help="Skip the confirmation prompt and add the job immediately.",
    ),
    debug: bool = typer.Option(
        False,
        "--debug",
        help="Dump raw HTML from both sources and report candidate apply URLs.",
    ),
) -> None:
    """Scrape a LinkedIn job posting, then confirm before tracking it."""
    if debug:
        _fetch_debug(url)
        return

    try:
        data = fetch_linkedin_job(url)
    except ValueError as exc:
        # Bad/unrecognized URL — normalize_linkedin_url couldn't find a job ID.
        console.print(f"[bold red]✗[/] {exc}")
        raise typer.Exit(code=1)
    except Exception as exc:  # network / HTTP / parse failures
        console.print(f"[bold red]✗ Failed to fetch posting:[/] {exc}")
        raise typer.Exit(code=1)

    body = Text()
    body.append("★ ", style="bold magenta")
    body.append(data["title"] or "(no title)", style="bold white")
    body.append("\n   company:  ", style="dim")
    body.append(data["company"] or "—", style="white")
    body.append("\n   location: ", style="dim")
    body.append(data["location"] or "—", style="white")
    body.append("\n   apply:    ", style="dim")
    if data["apply_type"] == "offsite" and data["apply_url"]:
        body.append(
            data["apply_url"],
            style=Style(color="blue", underline=True, link=data["apply_url"]),
        )
    elif data["apply_type"] == "offsite_gated":
        body.append("External (URL hidden by LinkedIn sign-in)", style="yellow")
    else:
        body.append("Easy Apply", style="yellow")
    body.append("\n   source:   ", style="dim")
    body.append(
        data["original_linkedin_url"],
        style=Style(color="blue", underline=True, link=data["original_linkedin_url"]),
    )

    desc = data["description_text"]
    if desc:
        preview = desc if len(desc) <= 300 else desc[:300].rstrip() + " …"
        body.append("\n\n", style="dim")
        body.append(preview, style="dim")

    console.print(
        Panel(
            body,
            title="[bold green]🔍 LinkedIn posting[/]",
            title_align="left",
            border_style="green",
            box=box.ROUNDED,
            padding=(1, 2),
        )
    )

    # ----- confirm, then persist ----- #
    # Default to an interactive prompt; --save/-s bypasses it for scripting.
    # A declined *or* aborted (Ctrl-C / EOF) prompt is treated as a clean
    # discard rather than an error.
    if save:
        proceed = True
    else:
        try:
            proceed = typer.confirm(
                "Do you want to add this job to your tracking pipeline?"
            )
        except typer.Abort:
            proceed = False

    if not proceed:
        console.print("[yellow]Job discarded.[/yellow]")
        raise typer.Exit()

    init_db()
    job = JobApplication(
        title=data["title"] or "Unknown",
        company=data["company"] or "Unknown",
        location=data["location"],
        url=data["original_linkedin_url"],
        description=data["description_text"],
    )
    with get_session() as session:
        session.add(job)
        session.commit()

    console.print(
        "[bold green]Successfully added to your pipeline under 'Drafting'.[/bold green]"
        " Run 'pushcv status' to view."
    )

    # First time only: decide how salary estimates are produced (shown on status).
    _ensure_ai_salary_preference()


# --------------------------------------------------------------------------- #
# Salary estimation (shared by add / fetch / status)
# --------------------------------------------------------------------------- #
# Salary is not a user-facing command — it's filled automatically. Web
# extraction is the zero-dependency default; the local AI model is an opt-in
# enhancement remembered per workspace (see `config`).
def _currency_symbol(currency: str) -> str:
    """Pull the currency symbol from a 'INR (₹, ...)'-style string."""
    return next((c for c in currency if c in "$€£₹¥"), "$")


# Seniority keywords to lift from a job title (longest/most-specific first).
_SENIORITY_WORDS = [
    "principal", "staff", "senior", "sr.", "sr", "lead", "head of", "head",
    "director", "vp", "vice president", "manager", "mid-level", "mid",
    "junior", "jr.", "jr", "entry level", "entry-level", "intern", "associate",
]


def _role_experience(title: str, description: str) -> str:
    """Build a seniority/experience hint for the search query and AI prompt.

    Combines the seniority word from the title with the years-of-experience the
    posting asks for (parsed from the description), e.g. "senior, 5+ years".
    """
    title_l = (title or "").lower()
    seniority = next((w for w in _SENIORITY_WORDS if w in title_l), "")

    years = ""
    match = re.search(
        r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:years?|yrs?)"
        r"(?:[^.]{0,20}experience)?",
        description or "",
        re.IGNORECASE,
    )
    if match:
        years = f"{match.group(1)}+ years"

    return ", ".join(p for p in (seniority, years) if p)


def _candidate_yoe() -> Optional[int]:
    """Best-effort total years of experience parsed from profile.md."""
    if not PROFILE_PATH.exists():
        return None
    text = PROFILE_PATH.read_text(encoding="utf-8")
    # Prefer an explicit "X+ years" statement; else infer span from year ranges.
    match = re.search(r"(\d{1,2})\s*\+?\s*years", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    years = [int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", text)]
    return (max(years) - min(years)) if len(years) >= 2 else None


def _estimate_salary(job: JobApplication, ai_enabled: bool, model: str) -> Optional[str]:
    """Estimate compensation for a job, returning a display string or None.

    The query is anchored to the role's seniority/required experience so figures
    reflect the right level (not a company-wide average). With AI enabled, the
    model also positions the estimate using the candidate's YOE; it falls back to
    plain web extraction if the model errors or is unreachable.
    """
    location = job.location or ""
    currency = currency_for_location(location)
    experience = _role_experience(job.title, job.description or "")

    if ai_enabled:
        context = get_salary_snippets(job.title, job.company, location, experience)
        estimate = estimate_compensation(
            job_title=job.title,
            company=job.company,
            location=location,
            search_context=context,
            model_name=model,
            currency=currency,
            role_experience=experience,
            candidate_yoe=_candidate_yoe(),
        )
        if estimate and not estimate.startswith("ERROR:"):
            return estimate
        # AI unavailable/failed — fall through to web extraction.

    return extract_salary(
        job.title, job.company, location, _currency_symbol(currency), experience
    )


def _ensure_ai_salary_preference() -> None:
    """Ask once whether to use AI for salary estimates; remember the choice."""
    if config.get_ai_salary_enabled() is not None:
        return

    console.print(
        Panel(
            Text(
                "pushcv can estimate compensation for your applications.\n\n"
                "• Web search (default): figures pulled from the web, with the "
                "source cited. No setup required.\n"
                "• AI synthesis (opt-in): a local Lemonade model cleans up the "
                "web data into a tighter range. Needs a running model.\n\n"
                "Estimates are experimental — a ballpark from public web data, "
                "not an offer benchmark.",
                style="white",
            ),
            title="[bold cyan]💰 Salary estimates (experimental)[/]",
            title_align="left",
            border_style="cyan",
            box=box.ROUNDED,
            padding=(1, 2),
        )
    )
    try:
        enabled = typer.confirm(
            "Enable AI-powered salary estimates?", default=False
        )
    except typer.Abort:
        enabled = False

    config.set_ai_salary_enabled(enabled)
    chosen = "AI synthesis" if enabled else "web extraction"
    console.print(
        f"[dim]Using [bold]{chosen}[/bold] for salary estimates "
        f"(saved to {config.CONFIG_PATH}; edit or delete it to change).[/dim]"
    )


# --------------------------------------------------------------------------- #
# status (Kanban board)
# --------------------------------------------------------------------------- #
# Applications older than this (in the Applied column) get a follow-up nudge.
FOLLOW_UP_DAYS = 14


def _days_since(dt: Optional[datetime]) -> Optional[int]:
    """Whole days elapsed since ``dt`` (UTC). SQLite returns naive datetimes."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - dt).days)


def _job_card(job: JobApplication, accent: str, symbol: str, position: int) -> Panel:
    """Render a single application as a compact Kanban card.

    The leading coloured symbol + position acts as the explicit highlight
    designator (and the handle used in commands) so each row is instantly
    scannable and actionable.
    """
    header = Text()
    header.append(f"{symbol} ", style=f"bold {accent}")  # highlight designator
    header.append(f"[{position}]", style=f"bold {accent}")
    header.append(f"  {job.company}", style="bold white")

    lines: List[Text] = [header, Text(job.title, style="dim")]
    if job.salary_estimate:
        # AI salary estimate sits directly under company/title in a distinct
        # green so candidates can scan compensation at a glance.
        lines.append(Text(f"💰 {job.salary_estimate}", style="bold green"))
    if job.location:
        # Location helps candidates triage on-site vs remote / commute at a glance.
        loc = Text()
        loc.append("📍 ", style="dim")
        loc.append(job.location, style="dim")
        lines.append(loc)
    if job.url:
        # Compact, non-wrapping label (the domain); full URL is the click target.
        netloc = urlparse(job.url).netloc or job.url
        lines.append(_link(job.url, f"🔗 {netloc}"))
    if _bucket(job.status) == "applied":
        # Time-in-column nudge: how long since applying, red once it's stale
        # enough that a follow-up is overdue.
        days = _days_since(job.applied_at or job.created_at)
        if days is not None:
            stale = days >= FOLLOW_UP_DAYS
            age = Text()
            age.append(
                f"⏱ applied {days}d ago", style="bold red" if stale else "dim"
            )
            if stale:
                age.append(" — follow up?", style="red")
            lines.append(age)

    return Panel(
        Group(*lines),
        border_style=accent,
        box=box.ROUNDED,
        padding=(0, 1),
    )


def _backfill_salary_estimates(model: str = DEFAULT_AI_MODEL) -> None:
    """Populate salary_estimate for any job that doesn't have one yet.

    Uses AI synthesis or plain web extraction per the workspace preference (see
    :func:`_estimate_salary`). Best-effort: any failure (no network, model down,
    rate limit) is skipped so it never blocks rendering the board. Once a job has
    an estimate it is not re-queried, keeping repeat ``status`` calls fast.
    """
    ai_enabled = bool(config.get_ai_salary_enabled())
    with get_session() as session:
        missing = session.exec(
            select(JobApplication).where(JobApplication.salary_estimate.is_(None))
        ).all()
        if not missing:
            return

        source = "AI" if ai_enabled else "web"
        with console.status(
            f"[bold green]Fetching {source} compensation for {len(missing)} "
            f"listing(s)...[/bold green]"
        ) as spinner:
            for job in missing:
                spinner.update(
                    f"[bold green]Estimating compensation for "
                    f"{job.company}...[/bold green]"
                )
                try:
                    estimate = _estimate_salary(job, ai_enabled, model)
                except Exception:
                    continue  # never let estimation break the board
                if estimate:
                    job.salary_estimate = estimate
                    session.add(job)
                    # Commit per job so a slow/interrupted run keeps its progress
                    # (AI estimates can take a while each).
                    session.commit()


@app.command()
def status() -> None:
    """Show the application pipeline as a multi-column Kanban board."""
    init_db()

    # Ensure every listing has compensation info before rendering the board.
    _backfill_salary_estimates()

    with get_session() as session:
        jobs = session.exec(
            select(JobApplication).order_by(
                JobApplication.created_at, JobApplication.id
            )
        ).all()

    # Assign each job its 1-based display position (the handle used in commands).
    position_by_id = {job.id: i for i, job in enumerate(jobs, start=1)}

    # Bucket every job into its canonical column.
    buckets = {key: [] for key, _, _ in STATUS_COLUMNS}
    for job in jobs:
        buckets[_bucket(job.status)].append(job)

    if not jobs:
        console.print(
            Panel(
                Text(
                    "No applications yet.\n"
                    'Add one with:  pushcv add "Company" "Title" --url <link>',
                    style="dim",
                ),
                title="[bold]pushcv · Pipeline[/]",
                title_align="left",
                border_style="cyan",
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )
        return

    # Build the Kanban table: one column per status, one block per column.
    table = Table(
        title="[bold]pushcv · Application Pipeline[/]",
        caption=(
            f"[dim]{len(jobs)} application(s) tracked · "
            "💰 estimates are experimental[/]"
        ),
        box=box.HEAVY_HEAD,
        show_lines=False,
        expand=True,
        padding=(0, 1),
    )

    for key, label, accent in STATUS_COLUMNS:
        count = len(buckets[key])
        table.add_column(
            f"{label}  [{accent}]({count})[/]",
            header_style=f"bold {accent}",
            justify="left",
            vertical="top",
            ratio=1,
        )

    # Each column becomes a vertical stack of cards (a clean "column block").
    cells = []
    for key, label, accent in STATUS_COLUMNS:
        col_jobs = buckets[key]
        symbol = STATUS_SYMBOL[key]
        if not col_jobs:
            cells.append(Text("— empty —", style="dim italic", justify="center"))
        else:
            cells.append(
                Group(
                    *[
                        _job_card(j, accent, symbol, position_by_id[j.id])
                        for j in col_jobs
                    ]
                )
            )

    table.add_row(*cells)
    console.print(table)


# --------------------------------------------------------------------------- #
# move (advance a job through the pipeline)
# --------------------------------------------------------------------------- #
@app.command()
def move(
    position: int = typer.Argument(
        ..., help="Position number shown in 'pushcv status'."
    ),
    status: str = typer.Argument(
        ...,
        help="New status — a column (drafting/applied/interviewing/closed) "
        "or a synonym like offer, rejected, onsite, ghosted.",
    ),
) -> None:
    """Move a job to a new status on the pipeline board."""
    init_db()

    # Normalize free-text input ("Ready to apply" -> "ready_to_apply") and
    # validate against the known synonyms so a typo never corrupts the board.
    new_status = re.sub(r"[\s-]+", "_", status.strip().lower())
    if new_status not in STATUS_ALIASES:
        valid = ", ".join(sorted(STATUS_ALIASES))
        console.print(
            Panel(
                Text(
                    f"Unknown status '{status}'.\nValid values: {valid}",
                    style="white",
                ),
                title="[bold red]✗ Invalid status[/]",
                title_align="left",
                border_style="red",
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )
        raise typer.Exit(code=1)

    job_id = _resolve_position(position)
    if job_id is None:
        raise _invalid_position(position)

    column_key = _bucket(new_status)
    applied_recorded = False
    with get_session() as session:
        job = session.get(JobApplication, job_id)
        old_status = job.status
        job.status = new_status
        # First arrival in the Applied column stamps the application date,
        # which drives the follow-up staleness hint on the board.
        if column_key == "applied" and job.applied_at is None:
            job.applied_at = datetime.now(timezone.utc)
            applied_recorded = True
        session.add(job)
        session.commit()
        company, title = job.company, job.title

    # Style the confirmation with the destination column's accent/symbol.
    label, accent = next(
        (lbl, acc) for key, lbl, acc in STATUS_COLUMNS if key == column_key
    )
    symbol = STATUS_SYMBOL[column_key]

    body = Text()
    body.append(f"{symbol} ", style=f"bold {accent}")
    body.append(f"[{position}]  ", style=f"bold {accent}")
    body.append(company, style="bold white")
    body.append("  ·  ", style="dim")
    body.append(title, style="white")
    body.append("\n   status: ", style="dim")
    body.append(old_status, style="dim")
    body.append("  →  ", style="dim")
    body.append(new_status, style=f"bold {accent}")
    body.append(f"   ({label} column)", style="dim")
    if applied_recorded:
        body.append("\n   applied date recorded — ", style="dim")
        body.append(datetime.now(timezone.utc).strftime("%Y-%m-%d"), style="cyan")

    console.print(
        Panel(
            body,
            title=f"[bold {accent}]⇒ Job moved[/]",
            title_align="left",
            border_style=accent,
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )


# --------------------------------------------------------------------------- #
# show (full details for one job)
# --------------------------------------------------------------------------- #
@app.command()
def show(
    position: int = typer.Argument(
        ..., help="Position number shown in 'pushcv status'."
    ),
) -> None:
    """Show everything stored for one job, including the full description."""
    init_db()

    job_id = _resolve_position(position)
    if job_id is None:
        raise _invalid_position(position)
    with get_session() as session:
        job = session.get(JobApplication, job_id)

    column_key = _bucket(job.status)
    label, accent = next(
        (lbl, acc) for key, lbl, acc in STATUS_COLUMNS if key == column_key
    )
    symbol = STATUS_SYMBOL[column_key]

    header = Text()
    header.append(f"{symbol} ", style=f"bold {accent}")
    header.append(f"[{position}]  ", style=f"bold {accent}")
    header.append(job.company, style="bold white")
    header.append("  ·  ", style="dim")
    header.append(job.title, style="white")

    rows = Text()
    rows.append("status    ", style="dim")
    rows.append(job.status, style=f"bold {accent}")
    rows.append(f"  ({label})", style="dim")
    rows.append("\nsalary    ", style="dim")
    if job.salary_estimate:
        rows.append(f"💰 {job.salary_estimate}", style="bold green")
        rows.append("  (experimental)", style="dim")
    else:
        rows.append("—", style="dim")
    rows.append("\nlocation  ", style="dim")
    rows.append(job.location or "—", style="white" if job.location else "dim")
    rows.append("\nadded     ", style="dim")
    rows.append(
        job.created_at.strftime("%Y-%m-%d %H:%M UTC") if job.created_at else "—",
        style="white",
    )
    if job.applied_at:
        days = _days_since(job.applied_at)
        rows.append("\napplied   ", style="dim")
        rows.append(job.applied_at.strftime("%Y-%m-%d"), style="white")
        rows.append(f"  ({days}d ago)", style="dim")
    rows.append("\nurl       ", style="dim")
    if job.url:
        rows.append(job.url, style=Style(color="blue", underline=True, link=job.url))
    else:
        rows.append("—", style="dim")

    parts: List[Text] = [header, Text(""), rows, Text("")]
    if job.notes:
        parts.append(Text("🗒  Notes", style="bold"))
        parts.append(Text(job.notes, style="white"))
        parts.append(Text(""))
    if job.description:
        parts.append(Text("─" * 40, style="dim"))
        parts.append(Text(job.description, style="white"))
    else:
        parts.append(
            Text(
                "No description stored. 'pushcv fetch <url>' captures the full "
                "posting automatically.",
                style="dim italic",
            )
        )

    console.print(
        Panel(
            Group(*parts),
            title=f"[bold {accent}]📄 Job details[/]",
            title_align="left",
            border_style=accent,
            box=box.ROUNDED,
            padding=(1, 2),
        )
    )


# --------------------------------------------------------------------------- #
# note (per-job timeline)
# --------------------------------------------------------------------------- #
@app.command()
def note(
    position: int = typer.Argument(
        ..., help="Position number shown in 'pushcv status'."
    ),
    text: str = typer.Argument(
        ..., help='The note, e.g. "recruiter call Friday 3pm".'
    ),
) -> None:
    """Add a dated note to a job — its timeline shows up in 'pushcv show'."""
    init_db()

    body_text = text.strip()
    if not body_text:
        console.print("[bold red]✗[/] Note text is empty.")
        raise typer.Exit(code=1)

    job_id = _resolve_position(position)
    if job_id is None:
        raise _invalid_position(position)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    line = f"[{stamp}] {body_text}"
    with get_session() as session:
        job = session.get(JobApplication, job_id)
        job.notes = f"{job.notes}\n{line}" if job.notes else line
        session.add(job)
        session.commit()
        company, title = job.company, job.title
        note_count = job.notes.count("\n") + 1

    body = Text()
    body.append("🗒  ", style="bold cyan")
    body.append(f"[{position}]  ", style="bold cyan")
    body.append(company, style="bold white")
    body.append("  ·  ", style="dim")
    body.append(title, style="white")
    body.append(f"\n   {line}", style="white")
    body.append(
        f"\n   {note_count} note(s) — see them all with 'pushcv show {position}'",
        style="dim",
    )

    console.print(
        Panel(
            body,
            title="[bold cyan]＋ Note added[/]",
            title_align="left",
            border_style="cyan",
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )


# --------------------------------------------------------------------------- #
# export (your data is yours)
# --------------------------------------------------------------------------- #
_EXPORT_FIELDS = [
    "position", "company", "title", "status", "location", "salary_estimate",
    "url", "created_at", "applied_at", "notes", "description",
]


@app.command()
def export(
    fmt: str = typer.Option(
        "json", "--format", "-f", help="Output format: json or csv."
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Write to this file instead of stdout."
    ),
) -> None:
    """Export every tracked job as JSON or CSV (stdout by default)."""
    init_db()

    fmt_l = fmt.strip().lower()
    if fmt_l not in ("json", "csv"):
        console.print(f"[bold red]✗[/] Unknown format '{fmt}'. Use json or csv.")
        raise typer.Exit(code=1)

    with get_session() as session:
        jobs = session.exec(
            select(JobApplication).order_by(
                JobApplication.created_at, JobApplication.id
            )
        ).all()

    records = [
        {
            "position": i,
            "company": job.company,
            "title": job.title,
            "status": job.status,
            "location": job.location,
            "salary_estimate": job.salary_estimate,
            "url": job.url,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "applied_at": job.applied_at.isoformat() if job.applied_at else None,
            "notes": job.notes,
            "description": job.description,
        }
        for i, job in enumerate(jobs, start=1)
    ]

    if fmt_l == "json":
        payload = json.dumps(records, indent=2, ensure_ascii=False) + "\n"
    else:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=_EXPORT_FIELDS)
        writer.writeheader()
        writer.writerows(records)
        payload = buf.getvalue()

    if output is None:
        # Plain stdout (no Rich styling/wrapping) so the export pipes cleanly.
        typer.echo(payload, nl=False)
        return

    output.write_text(payload, encoding="utf-8")
    console.print(
        f"[bold green]✓[/] Exported {len(records)} job(s) → "
        f"[cyan]{output.resolve()}[/]"
    )


# --------------------------------------------------------------------------- #
# draft (AI synthesis engine)
# --------------------------------------------------------------------------- #
def _sanitize_filename(name: str) -> str:
    """Make a string safe for use as a filename component.

    Spaces become underscores; any remaining filesystem-unsafe characters are
    stripped so the path is portable across operating systems.
    """
    collapsed = re.sub(r"\s+", "_", name.strip())
    safe = re.sub(r"[^A-Za-z0-9._-]", "", collapsed)
    return safe or "company"


@app.command()
def draft(
    position: int = typer.Argument(
        ..., help="Position number shown in 'pushcv status'."
    ),
    model: str = typer.Option(
        DEFAULT_AI_MODEL,
        "--model",
        "-m",
        help="Local model name to synthesize with (must match a Lemonade model id).",
    ),
    cover_letter: bool = typer.Option(
        False,
        "--cover-letter",
        "-c",
        help="Draft a tailored cover letter instead of a resume.",
    ),
) -> None:
    """Synthesize a tailored resume (or cover letter) for a tracked job."""
    init_db()
    artifact = "cover letter" if cover_letter else "resume"

    # ----- a. Look up the job by display position ----- #
    job_id = _resolve_position(position)
    if job_id is None:
        raise _invalid_position(position)
    with get_session() as session:
        job = session.get(JobApplication, job_id)

    # ----- b. Read the master profile ----- #
    if not PROFILE_PATH.exists():
        console.print(
            Panel(
                Text(
                    f"Profile not found at {PROFILE_PATH.resolve()}.\n"
                    "Run 'pushcv init' to create it, then fill in your "
                    "experience, skills, and projects.",
                    style="white",
                ),
                title="[bold red]✗ Missing profile.md[/]",
                title_align="left",
                border_style="red",
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )
        raise typer.Exit(code=1)

    user_profile = PROFILE_PATH.read_text(encoding="utf-8")

    # ----- c. + d. Synthesize via the local AI engine ----- #
    with console.status(
        f"[bold cyan]Synthesizing {artifact} for {job.company} "
        f"using {model}...[/bold cyan]"
    ):
        if cover_letter:
            draft_md = generate_cover_letter(
                job_title=job.title,
                company=job.company,
                job_description=job.description or "",
                user_profile=user_profile,
                model_name=model,
            )
        else:
            draft_md = generate_tailored_resume(
                job_title=job.title,
                job_description=job.description or "",
                user_profile=user_profile,
                model_name=model,
            )

    # The engine returns a clean error string (never raises) on failure — don't
    # write a bogus draft or advance the job's status in that case.
    if draft_md.startswith("ERROR:"):
        console.print(
            Panel(
                Text(draft_md, style="white"),
                title="[bold red]✗ Synthesis failed[/]",
                title_align="left",
                border_style="red",
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )
        raise typer.Exit(code=1)

    # ----- e. Persist the draft to drafts/ ----- #
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "cover_letter" if cover_letter else "resume"
    out_path = DRAFTS_DIR / f"{job_id}_{_sanitize_filename(job.company)}_{suffix}.md"
    out_path.write_text(draft_md, encoding="utf-8")

    # ----- f. Advance the job's status (resume only) ----- #
    # A drafted resume means the application is ready to submit; a cover letter
    # is supplementary and leaves the pipeline state untouched.
    if not cover_letter:
        with get_session() as session:
            db_job = session.get(JobApplication, job_id)
            if db_job is not None:
                db_job.status = "ready_to_apply"
                session.add(db_job)
                session.commit()

    # ----- g. Success panel ----- #
    body = Text()
    body.append("✓ ", style="bold green")
    body.append(f"{job.company}", style="bold white")
    body.append("  ·  ", style="dim")
    body.append(job.title, style="white")
    body.append("\n\n  draft saved to  ", style="dim")
    body.append(str(out_path.resolve()), style="cyan")
    if not cover_letter:
        body.append("\n  status         ", style="dim")
        body.append("ready_to_apply", style="bold green")

    title_label = "✉️ Cover letter drafted" if cover_letter else "✨ Resume drafted"
    console.print(
        Panel(
            body,
            title=f"[bold green]{title_label}[/]",
            title_align="left",
            border_style="green",
            box=box.ROUNDED,
            padding=(1, 2),
        )
    )


# --------------------------------------------------------------------------- #
# delete (remove a job)
# --------------------------------------------------------------------------- #
@app.command()
def delete(
    position: int = typer.Argument(
        ..., help="Position number shown in 'pushcv status'."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
) -> None:
    """Remove a job application (and its generated draft) from the pipeline."""
    init_db()

    job_id = _resolve_position(position)
    if job_id is None:
        raise _invalid_position(position)

    with get_session() as session:
        job = session.get(JobApplication, job_id)
        # Capture details before the row is gone (for confirmation + messaging).
        company, title = job.company, job.title

    # Confirm — destructive and irreversible. The company/title is shown so a
    # mistyped position can't silently delete the wrong job. A declined or
    # aborted prompt is a clean no-op; --yes bypasses it for scripting.
    if not yes:
        try:
            proceed = typer.confirm(
                f"Remove [{position}] {company} · {title} from your pipeline?"
            )
        except typer.Abort:
            proceed = False
        if not proceed:
            console.print("[yellow]Deletion cancelled.[/yellow]")
            raise typer.Exit()

    with get_session() as session:
        job = session.get(JobApplication, job_id)
        if job is not None:
            session.delete(job)
            session.commit()

    # Remove any generated draft(s) for this job (drafts/{id}_*.md).
    removed_drafts = []
    for draft_file in DRAFTS_DIR.glob(f"{job_id}_*"):
        try:
            draft_file.unlink()
            removed_drafts.append(draft_file.name)
        except OSError:
            pass

    body = Text()
    body.append("🗑️  ", style="bold red")
    body.append(f"[{position}]  ", style="bold white")
    body.append(f"{company}", style="white")
    body.append("  ·  ", style="dim")
    body.append(title, style="white")
    if removed_drafts:
        body.append("\n\n  also removed draft: ", style="dim")
        body.append(", ".join(removed_drafts), style="cyan")

    console.print(
        Panel(
            body,
            title="[bold red]✓ Job removed[/]",
            title_align="left",
            border_style="red",
            box=box.ROUNDED,
            padding=(1, 2),
        )
    )


if __name__ == "__main__":
    app()
