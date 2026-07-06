"""Workspace-scoped data operations for pushcv — the service layer.

Pipeline semantics (statuses, positions, notes, timestamps, migrations) live
here in exactly one place, so every frontend behaves identically: the Typer
CLI (:mod:`pushcv.main`) renders these operations in the terminal, and
external frontends (e.g. the ``pushcv-ui`` local web app) call them over a
thin HTTP layer.

A :class:`Workspace` owns one job-hunt directory (SQLite database, profile,
drafts) — the same "everything lives where you run it" model the CLI has
always had.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import inspect as sa_inspect, text as sa_text
from sqlmodel import Session, SQLModel, create_engine, select

# Importing the models registers JobApplication on SQLModel.metadata so that
# create_all() knows about every table.
from pushcv import models  # noqa: F401  (imported for metadata registration)
from pushcv.models import JobApplication

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

# Applications older than this (in the Applied column) get a follow-up nudge.
FOLLOW_UP_DAYS = 14


def bucket(status: str) -> str:
    """Resolve an arbitrary status string to a canonical Kanban column key."""
    key = (status or "").strip().lower()
    return STATUS_ALIASES.get(key, "closed")


def normalize_status(text: str) -> str:
    """Normalize free-text status input to a known status value.

    ``"Ready to apply"`` → ``"ready_to_apply"``. Raises :class:`ValueError`
    (message lists the valid values) for anything outside the known synonyms,
    so a typo never corrupts the board.
    """
    normalized = re.sub(r"[\s-]+", "_", text.strip().lower())
    if normalized not in STATUS_ALIASES:
        valid = ", ".join(sorted(STATUS_ALIASES))
        raise ValueError(f"Unknown status '{text}'. Valid values: {valid}")
    return normalized


def column_meta(column_key: str) -> Tuple[str, str]:
    """Return ``(label, accent)`` for a canonical column key."""
    return next(
        (label, accent) for key, label, accent in STATUS_COLUMNS if key == column_key
    )


def _http_url(value: Any) -> Optional[str]:
    """Pass through http(s) URLs; reject anything else from scraped data.

    Scraped postings control these values — a javascript:/data:/file: scheme
    must never be stored where a frontend would render it as a clickable link.
    """
    if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
        return value
    return None


def days_since(dt: Optional[datetime]) -> Optional[int]:
    """Whole days elapsed since ``dt`` (UTC). SQLite returns naive datetimes."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - dt).days)


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


class PositionError(LookupError):
    """No job exists at the requested board position."""

    def __init__(self, position: int):
        self.position = position
        super().__init__(f"No job at position {position}.")


# --------------------------------------------------------------------------- #
# Operation results
# --------------------------------------------------------------------------- #
@dataclass
class MoveResult:
    position: int
    company: str
    title: str
    old_status: str
    new_status: str
    column_key: str
    applied_recorded: bool


@dataclass
class NoteResult:
    position: int
    company: str
    title: str
    line: str
    note_count: int


@dataclass
class DeleteResult:
    position: int
    company: str
    title: str
    removed_drafts: List[str]


# --------------------------------------------------------------------------- #
# Workspace
# --------------------------------------------------------------------------- #
class Workspace:
    """One job-hunt directory: database, profile, drafts, and the operations
    on them.

    Local-first: every path is relative to ``root`` (default: the current
    working directory), matching the CLI's "run it from your job-hunt folder"
    convention.
    """

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root is not None else Path(".")
        self.db_path = self.root / "pushcv.db"
        self.profile_path = self.root / "profile.md"
        self.drafts_dir = self.root / "drafts"
        # check_same_thread=False makes the connection usable across threads
        # (worker threads in the CLI, request threads in a local web frontend).
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            echo=False,
            connect_args={"check_same_thread": False},
        )

    # ----- schema ----- #
    def init_db(self) -> None:
        """Create the database file and all tables if they do not yet exist.

        Idempotent: ``create_all`` only emits ``CREATE TABLE`` for missing
        tables. A lightweight column migration then backfills any columns
        added to the model after a database was first created (``create_all``
        never ALTERs).
        """
        SQLModel.metadata.create_all(self.engine)
        self._migrate_columns()

    def _migrate_columns(self) -> None:
        """Add model columns missing from an existing job_application table.

        Keeps older local databases compatible without a migration framework:
        each nullable column added to :class:`JobApplication` is appended on
        demand.
        """
        table = JobApplication.__tablename__
        inspector = sa_inspect(self.engine)
        if table not in inspector.get_table_names():
            return  # create_all will have made it with every column.

        existing = {col["name"] for col in inspector.get_columns(table)}
        # column name -> SQLite type for nullable columns added post-creation.
        additions = {
            "location": "TEXT",
            "description": "TEXT",
            "salary_estimate": "VARCHAR",
            "applied_at": "TIMESTAMP",
            "notes": "TEXT",
            "apply_url": "TEXT",
        }
        with self.engine.begin() as conn:
            for name, sql_type in additions.items():
                if name not in existing:
                    conn.execute(
                        sa_text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
                    )

    def session(self) -> Session:
        """Return a new SQLModel Session bound to this workspace's engine."""
        return Session(self.engine)

    # ----- positional addressing ----- #
    # Users reference jobs by the 1..N position shown on the board, not by the
    # raw (gappy) database id. Position is the rank in a single canonical
    # ordering, computed here so every frontend stays in agreement.
    def ordered_jobs(self) -> List[JobApplication]:
        """All jobs in the canonical display order (position 1..N)."""
        with self.session() as session:
            return list(
                session.exec(
                    select(JobApplication).order_by(
                        JobApplication.created_at, JobApplication.id
                    )
                ).all()
            )

    def ordered_job_ids(self) -> List[int]:
        return [job.id for job in self.ordered_jobs()]

    def resolve_position(self, position: int) -> Optional[int]:
        """Map a 1-based display position to its underlying database id."""
        ids = self.ordered_job_ids()
        if 1 <= position <= len(ids):
            return ids[position - 1]
        return None

    def job_at(self, position: int) -> Optional[JobApplication]:
        """Return the job at a display position, or None."""
        job_id = self.resolve_position(position)
        if job_id is None:
            return None
        with self.session() as session:
            return session.get(JobApplication, job_id)

    # ----- operations ----- #
    def add_job(
        self,
        company: str,
        title: str,
        url: Optional[str] = None,
    ) -> Tuple[JobApplication, int]:
        """Add a manually-entered job; returns ``(job, board_position)``."""
        self.init_db()
        job = JobApplication(company=company, title=title, url=url)
        with self.session() as session:
            session.add(job)
            session.commit()
            session.refresh(job)
        # Newly added jobs sort last: their position is the current count.
        return job, len(self.ordered_job_ids())

    def add_scraped(self, data: Dict[str, Any]) -> Tuple[JobApplication, int]:
        """Persist a normalized scraped posting (see ``pushcv.portals``)."""
        self.init_db()
        apply_url = _http_url(data.get("apply_url"))
        canonical = _http_url(data.get("canonical_url"))
        job = JobApplication(
            title=data.get("title") or "Unknown",
            company=data.get("company") or "Unknown",
            location=data.get("location"),
            url=canonical,
            apply_url=apply_url if apply_url != canonical else None,
            description=data.get("description_text"),
        )
        with self.session() as session:
            session.add(job)
            session.commit()
            session.refresh(job)
        return job, len(self.ordered_job_ids())

    def move_job(self, position: int, status_text: str) -> MoveResult:
        """Move the job at ``position`` to a new status.

        Raises :class:`ValueError` for an unknown status and
        :class:`PositionError` for an out-of-range position. First arrival in
        the Applied column stamps ``applied_at``, which drives the follow-up
        staleness hint.
        """
        self.init_db()
        new_status = normalize_status(status_text)  # validate before resolving
        job_id = self.resolve_position(position)
        if job_id is None:
            raise PositionError(position)

        column_key = bucket(new_status)
        applied_recorded = False
        with self.session() as session:
            job = session.get(JobApplication, job_id)
            if job is None:  # row vanished between resolve and load
                raise PositionError(position)
            old_status = job.status
            job.status = new_status
            if column_key == "applied" and job.applied_at is None:
                job.applied_at = datetime.now(timezone.utc)
                applied_recorded = True
            session.add(job)
            session.commit()
            company, title = job.company, job.title

        return MoveResult(
            position=position,
            company=company,
            title=title,
            old_status=old_status,
            new_status=new_status,
            column_key=column_key,
            applied_recorded=applied_recorded,
        )

    def add_note(self, position: int, text: str) -> NoteResult:
        """Append a dated note line to the job's timeline.

        Raises :class:`ValueError` for empty text and :class:`PositionError`
        for an out-of-range position.
        """
        self.init_db()
        body = text.strip()
        if not body:
            raise ValueError("Note text is empty.")
        job_id = self.resolve_position(position)
        if job_id is None:
            raise PositionError(position)

        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        line = f"[{stamp}] {body}"
        with self.session() as session:
            job = session.get(JobApplication, job_id)
            if job is None:
                raise PositionError(position)
            job.notes = f"{job.notes}\n{line}" if job.notes else line
            session.add(job)
            session.commit()
            company, title = job.company, job.title
            # Count dated entries, not raw lines — a note's own text may
            # contain newlines.
            note_count = len(
                re.findall(r"^\[\d{4}-\d{2}-\d{2}\]", job.notes, re.MULTILINE)
            )

        return NoteResult(
            position=position,
            company=company,
            title=title,
            line=line,
            note_count=note_count,
        )

    def delete_job_id(self, job_id: int, *, position: int) -> DeleteResult:
        """Delete a job by database id (resolved earlier, e.g. pre-confirm).

        Also removes any generated drafts (``drafts/{id}_*``). ``position`` is
        carried through for display only.
        """
        self.init_db()
        with self.session() as session:
            job = session.get(JobApplication, job_id)
            if job is None:
                raise PositionError(position)
            company, title = job.company, job.title
            session.delete(job)
            session.commit()

        removed: List[str] = []
        for draft_file in self.drafts_dir.glob(f"{job_id}_*"):
            try:
                draft_file.unlink()
                removed.append(draft_file.name)
            except OSError:
                pass

        return DeleteResult(
            position=position, company=company, title=title, removed_drafts=removed
        )

    def delete_job(self, position: int) -> DeleteResult:
        """Delete the job at a display position (see :meth:`delete_job_id`)."""
        job_id = self.resolve_position(position)
        if job_id is None:
            raise PositionError(position)
        return self.delete_job_id(job_id, position=position)

    def mark_ready_to_apply(self, job_id: int) -> bool:
        """Advance a job to ``ready_to_apply`` after a resume draft.

        Only jobs still in the Drafting column advance — re-drafting a resume
        for a job that is already applied/interviewing/closed must never pull
        it back in the pipeline. Returns True when the status changed.
        """
        with self.session() as session:
            job = session.get(JobApplication, job_id)
            if job is None or bucket(job.status) != "drafting":
                return False
            job.status = "ready_to_apply"
            session.add(job)
            session.commit()
            return True

    # ----- profile ----- #
    def write_profile_template(self) -> bool:
        """Create profile.md from the template if it does not already exist.

        Returns True if the file was created, False if it already existed.
        """
        if self.profile_path.exists():
            return False
        self.profile_path.write_text(PROFILE_TEMPLATE, encoding="utf-8")
        return True

    def read_profile(self) -> Optional[str]:
        """The profile.md contents, or None when it doesn't exist yet."""
        if not self.profile_path.exists():
            return None
        return self.profile_path.read_text(encoding="utf-8")

    def write_profile(self, content: str) -> None:
        """Persist profile.md (creating it if needed)."""
        self.profile_path.write_text(content, encoding="utf-8")

    def profile_ready(self) -> bool:
        """Whether the profile looks filled-in enough to tailor drafts from.

        Heuristic: the file exists and the template's "Your Name" placeholder
        has been replaced. Deliberately soft — it only drives UX nudges, never
        blocks anything.
        """
        content = self.read_profile()
        return content is not None and "Your Name" not in content

    # ----- drafts ----- #
    def drafts_for(self, job_id: int) -> List[Path]:
        """Generated draft files for a job (``drafts/{id}_*``), newest first."""
        if not self.drafts_dir.is_dir():
            return []
        return sorted(
            self.drafts_dir.glob(f"{job_id}_*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    def export_records(self) -> List[Dict[str, Any]]:
        """Every tracked job as a plain dict, in board order with positions."""
        return [
            {
                "position": i,
                "company": job.company,
                "title": job.title,
                "status": job.status,
                "location": job.location,
                "salary_estimate": job.salary_estimate,
                "url": job.url,
                "apply_url": job.apply_url,
                "created_at": job.created_at.isoformat() if job.created_at else None,
                "applied_at": job.applied_at.isoformat() if job.applied_at else None,
                "notes": job.notes,
                "description": job.description,
            }
            for i, job in enumerate(self.ordered_jobs(), start=1)
        ]
