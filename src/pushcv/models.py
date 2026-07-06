"""SQLModel table definitions for pushcv.

A single table, :class:`JobApplication`, backs the local job-tracking database.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    Used as the default factory for ``created_at`` (``datetime.utcnow`` is
    deprecated from Python 3.12 onwards).
    """
    return datetime.now(timezone.utc)


class JobApplication(SQLModel, table=True):
    """A single tracked job application."""

    __tablename__ = "job_application"

    # Auto-incrementing integer primary key.
    id: Optional[int] = Field(default=None, primary_key=True)

    # Required free-text fields, stored as VARCHAR.
    company: str = Field(nullable=False, index=True)
    title: str = Field(nullable=False)

    # Optional URL stored as TEXT (no length limit).
    url: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))

    # Where to actually apply, when it differs from `url` (e.g. a LinkedIn
    # posting whose application happens on the employer's ATS).
    apply_url: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # Optional scraped metadata (e.g. from `pushcv fetch`), stored as TEXT.
    location: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    description: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # AI-estimated compensation (e.g. "$120k - $140k base + equity") from
    # `pushcv salary`. Stored as a free-text string.
    salary_estimate: Optional[str] = Field(default=None)

    # Pipeline status; defaults to "drafting".
    status: str = Field(default="drafting", nullable=False, index=True)

    # Creation timestamp (TIMESTAMP), defaulting to the current time.
    created_at: datetime = Field(default_factory=_utcnow, nullable=False)

    # When the job first moved to the Applied column (via `pushcv move`).
    # Drives the follow-up staleness hint on the board.
    applied_at: Optional[datetime] = Field(default=None, nullable=True)

    # Free-text timeline: one "[YYYY-MM-DD] text" line per `pushcv note`.
    notes: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
