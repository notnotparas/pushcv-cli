"""pushcv CLI entry point.

The terminal presentation layer: a global Typer application rendering the
pipeline with a high-fidelity Rich text user interface. All data operations
(statuses, positions, notes, migrations) live in the service layer,
:mod:`pushcv.core`, shared with external frontends such as ``pushcv-ui``.
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
from sqlmodel import select

from pushcv.core import (
    FOLLOW_UP_DAYS,
    STATUS_COLUMNS,
    STATUS_SYMBOL,
    PositionError,
    Workspace,
    bucket as _bucket,
    column_meta,
    days_since as _days_since,
)
from pushcv.models import JobApplication
from pushcv import portals
from pushcv.scraper import fetch_linkedin_debug
from pushcv.ai_engine import (
    currency_for_location,
    estimate_compensation,
    generate_cover_letter,
    generate_tailored_resume,
)
from pushcv.search import extract_salary, get_salary_snippets
from pushcv import config

# --------------------------------------------------------------------------- #
# Workspace
# --------------------------------------------------------------------------- #
# Local-first: the SQLite file, profile template, and drafts live next to
# wherever the user runs pushcv. The Workspace (pushcv.core) owns the engine,
# schema migrations, positional addressing, and every pipeline operation.
ws = Workspace()

# Default local model id (must match a model available on the local inference
# server — see PUSHCV_AI_BASE in ai_engine.py). Override with PUSHCV_AI_MODEL,
# e.g. PUSHCV_AI_MODEL=qwen3:8b for Ollama.
DEFAULT_AI_MODEL = os.getenv("PUSHCV_AI_MODEL", "Qwen3-8B-GGUF")


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


# Kanban pipeline configuration (STATUS_COLUMNS / STATUS_ALIASES /
# STATUS_SYMBOL / _bucket) is shared with other frontends via pushcv.core.


def _link(url: str, label: Optional[str] = None) -> Text:
    """Build a clickable terminal hyperlink (OSC 8).

    The *full* ``url`` is always the click target regardless of the visible
    ``label`` — so a truncated/short display still opens the complete link in
    terminals that support OSC 8 hyperlinks (VS Code, iTerm2, GNOME Terminal,
    Windows Terminal, etc.).
    """
    return Text(label or url, style=Style(color="blue", underline=True, link=url))


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
    db_existed = ws.db_path.exists()

    # Generate all SQLModel metadata structures (idempotent).
    ws.init_db()

    # Drop a base profile.md template alongside the database.
    profile_created = ws.write_profile_template()

    # ----- success banner ----- #
    db_line = Text()
    db_line.append("  database  ", style="bold")
    if db_existed:
        db_line.append("already present  ", style="yellow")
    else:
        db_line.append("created  ", style="green")
    db_line.append(str(ws.db_path.resolve()), style="cyan")

    profile_line = Text()
    profile_line.append("  profile   ", style="bold")
    if profile_created:
        profile_line.append("created  ", style="green")
    else:
        profile_line.append("already present  ", style="yellow")
    profile_line.append(str(ws.profile_path.resolve()), style="cyan")

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
    job, position = ws.add_job(company=company, title=title, url=url)

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
# fetch (scrape a job posting — LinkedIn, Greenhouse, Lever, SmartRecruiters,
# or any page with schema.org JobPosting metadata)
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
    url: str = typer.Argument(
        ...,
        help="Job posting URL — LinkedIn, Greenhouse, Lever, SmartRecruiters, "
        "or any page with JobPosting metadata.",
    ),
    save: bool = typer.Option(
        False,
        "--save",
        "-s",
        help="Skip the confirmation prompt and add the job immediately.",
    ),
    debug: bool = typer.Option(
        False,
        "--debug",
        help="LinkedIn only: dump raw HTML from both sources and report "
        "candidate apply URLs.",
    ),
) -> None:
    """Scrape a job posting from a supported portal, then confirm tracking it."""
    if debug:
        _fetch_debug(url)
        return

    try:
        data = portals.scrape_job(url)
    except ValueError as exc:
        # A portal claimed the URL but couldn't find its identifiers in it.
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
    if data["apply_url"]:
        body.append(
            data["apply_url"],
            style=Style(color="blue", underline=True, link=data["apply_url"]),
        )
    elif data["apply_type"] == "offsite_gated":
        body.append("External (URL hidden by LinkedIn sign-in)", style="yellow")
    elif data["apply_type"] == "easy_apply":
        body.append("Easy Apply (on LinkedIn)", style="yellow")
    else:
        body.append("—", style="dim")
    body.append("\n   source:   ", style="dim")
    body.append(
        data["canonical_url"],
        style=Style(color="blue", underline=True, link=data["canonical_url"]),
    )

    desc = data["description_text"]
    if desc:
        preview = desc if len(desc) <= 300 else desc[:300].rstrip() + " …"
        body.append("\n\n", style="dim")
        body.append(preview, style="dim")

    console.print(
        Panel(
            body,
            title=f"[bold green]🔍 {data['portal_label']} posting[/]",
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

    ws.add_scraped(data)

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
    if not ws.profile_path.exists():
        return None
    text = ws.profile_path.read_text(encoding="utf-8")
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

    Estimation sends job title/company/location to a web search engine; the
    ``salary_estimates_enabled: false`` workspace preference disables it (and
    every network call it makes) entirely.
    """
    if not config.get_salary_estimates_enabled():
        return
    ai_enabled = bool(config.get_ai_salary_enabled())
    with ws.session() as session:
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
    ws.init_db()

    # Ensure every listing has compensation info before rendering the board.
    _backfill_salary_estimates()

    jobs = ws.ordered_jobs()

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
    try:
        result = ws.move_job(position, status)
    except ValueError as exc:
        # Unknown status — the message lists the valid synonyms.
        console.print(
            Panel(
                Text(str(exc).replace(". Valid values:", ".\nValid values:"), style="white"),
                title="[bold red]✗ Invalid status[/]",
                title_align="left",
                border_style="red",
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )
        raise typer.Exit(code=1)
    except PositionError:
        raise _invalid_position(position)

    # Style the confirmation with the destination column's accent/symbol.
    label, accent = column_meta(result.column_key)
    symbol = STATUS_SYMBOL[result.column_key]

    body = Text()
    body.append(f"{symbol} ", style=f"bold {accent}")
    body.append(f"[{position}]  ", style=f"bold {accent}")
    body.append(result.company, style="bold white")
    body.append("  ·  ", style="dim")
    body.append(result.title, style="white")
    body.append("\n   status: ", style="dim")
    body.append(result.old_status, style="dim")
    body.append("  →  ", style="dim")
    body.append(result.new_status, style=f"bold {accent}")
    body.append(f"   ({label} column)", style="dim")
    if result.applied_recorded:
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
    ws.init_db()

    job = ws.job_at(position)
    if job is None:
        raise _invalid_position(position)

    column_key = _bucket(job.status)
    label, accent = column_meta(column_key)
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
    if job.apply_url and job.apply_url != job.url:
        rows.append("\napply     ", style="dim")
        rows.append(
            job.apply_url,
            style=Style(color="blue", underline=True, link=job.apply_url),
        )

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
    try:
        result = ws.add_note(position, text)
    except ValueError:
        console.print("[bold red]✗[/] Note text is empty.")
        raise typer.Exit(code=1)
    except PositionError:
        raise _invalid_position(position)

    body = Text()
    body.append("🗒  ", style="bold cyan")
    body.append(f"[{position}]  ", style="bold cyan")
    body.append(result.company, style="bold white")
    body.append("  ·  ", style="dim")
    body.append(result.title, style="white")
    body.append(f"\n   {result.line}", style="white")
    body.append(
        f"\n   {result.note_count} note(s) — see them all with 'pushcv show {position}'",
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
    "url", "apply_url", "created_at", "applied_at", "notes", "description",
]


def _csv_safe(value):
    """Neutralize spreadsheet formula injection in CSV exports.

    Scraped titles/companies are attacker-influenced; a cell starting with
    =, +, -, or @ executes as a formula when the CSV is opened in Excel or
    Sheets. A leading apostrophe forces text interpretation (OWASP guidance).
    """
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


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
    ws.init_db()

    fmt_l = fmt.strip().lower()
    if fmt_l not in ("json", "csv"):
        console.print(f"[bold red]✗[/] Unknown format '{fmt}'. Use json or csv.")
        raise typer.Exit(code=1)

    records = ws.export_records()

    if fmt_l == "json":
        payload = json.dumps(records, indent=2, ensure_ascii=False) + "\n"
    else:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=_EXPORT_FIELDS)
        writer.writeheader()
        writer.writerows(
            [{k: _csv_safe(v) for k, v in record.items()} for record in records]
        )
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
    ws.init_db()
    artifact = "cover letter" if cover_letter else "resume"

    # ----- a. Look up the job by display position ----- #
    job = ws.job_at(position)
    if job is None:
        raise _invalid_position(position)

    # ----- b. Read the master profile ----- #
    if not ws.profile_path.exists():
        console.print(
            Panel(
                Text(
                    f"Profile not found at {ws.profile_path.resolve()}.\n"
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

    user_profile = ws.profile_path.read_text(encoding="utf-8")

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
    ws.drafts_dir.mkdir(parents=True, exist_ok=True)
    suffix = "cover_letter" if cover_letter else "resume"
    out_path = ws.drafts_dir / f"{job.id}_{_sanitize_filename(job.company)}_{suffix}.md"
    out_path.write_text(draft_md, encoding="utf-8")

    # ----- f. Advance the job's status (resume only) ----- #
    # A drafted resume means the application is ready to submit; a cover letter
    # is supplementary and leaves the pipeline state untouched. Only jobs still
    # in the Drafting column advance (enforced by the service layer) —
    # re-drafting a resume for a job that is already applied/interviewing/closed
    # must never pull it back in the pipeline.
    status_advanced = ws.mark_ready_to_apply(job.id) if not cover_letter else False

    # ----- g. Success panel ----- #
    body = Text()
    body.append("✓ ", style="bold green")
    body.append(f"{job.company}", style="bold white")
    body.append("  ·  ", style="dim")
    body.append(job.title, style="white")
    body.append("\n\n  draft saved to  ", style="dim")
    body.append(str(out_path.resolve()), style="cyan")
    if status_advanced:
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
    ws.init_db()

    job = ws.job_at(position)
    if job is None:
        raise _invalid_position(position)

    # Confirm — destructive and irreversible. The company/title is shown so a
    # mistyped position can't silently delete the wrong job. A declined or
    # aborted prompt is a clean no-op; --yes bypasses it for scripting.
    if not yes:
        try:
            proceed = typer.confirm(
                f"Remove [{position}] {job.company} · {job.title} from your pipeline?"
            )
        except typer.Abort:
            proceed = False
        if not proceed:
            console.print("[yellow]Deletion cancelled.[/yellow]")
            raise typer.Exit()

    # Delete by the id resolved pre-confirmation, so the prompt and the
    # deletion can never disagree about which job is meant.
    try:
        result = ws.delete_job_id(job.id, position=position)
    except PositionError:
        raise _invalid_position(position)

    body = Text()
    body.append("🗑️  ", style="bold red")
    body.append(f"[{position}]  ", style="bold white")
    body.append(f"{result.company}", style="white")
    body.append("  ·  ", style="dim")
    body.append(result.title, style="white")
    if result.removed_drafts:
        body.append("\n\n  also removed draft: ", style="dim")
        body.append(", ".join(result.removed_drafts), style="cyan")

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
