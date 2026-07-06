"""Greenhouse portal adapter.

Greenhouse-hosted boards expose a public, unauthenticated Job Board API:

    GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{job_id}

so no HTML scraping is needed. Hosted posting URLs look like:

* https://job-boards.greenhouse.io/{board_token}/jobs/{job_id}
* https://boards.greenhouse.io/{board_token}/jobs/{job_id}   (legacy)
* .eu. variants of both, served by the EU API host
* https://boards.greenhouse.io/embed/job_app?for={board_token}&token={job_id}

The ``content`` field arrives HTML-*escaped* (``&lt;p&gt;...``), so it is
unescaped before the tags are stripped.
"""
from __future__ import annotations

import html
import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from pushcv import scraper
from pushcv.portals.base import fetch_json, host_of, posting, strip_html

PORTAL = "greenhouse"
LABEL = "Greenhouse"

_HOSTS = {
    "boards.greenhouse.io": "us",
    "job-boards.greenhouse.io": "us",
    "boards.eu.greenhouse.io": "eu",
    "job-boards.eu.greenhouse.io": "eu",
}

_API_BASE = {
    "us": "https://boards-api.greenhouse.io/v1/boards",
    "eu": "https://boards-api.eu.greenhouse.io/v1/boards",
}

_PATH_RE = re.compile(r"^/(?P<board>[^/]+)/jobs/(?P<job_id>\d+)")


def matches(url: str) -> bool:
    return host_of(url) in _HOSTS


def board_and_job(url: str) -> Tuple[str, str, str]:
    """Extract ``(board_token, job_id, region)`` from a Greenhouse URL.

    Raises :class:`ValueError` when the URL doesn't carry both identifiers.
    """
    split = urlsplit(url)
    region = _HOSTS.get((split.hostname or "").lower(), "us")

    match = _PATH_RE.match(split.path)
    if match:
        return match.group("board"), match.group("job_id"), region

    # Embed form: /embed/job_app?for=<board>&token=<job_id>
    if "/embed/job_app" in split.path:
        params = parse_qs(split.query)
        board = (params.get("for") or [None])[0]
        job_id = (params.get("token") or [None])[0]
        if board and job_id and job_id.isdigit():
            return board, job_id, region

    raise ValueError(f"Could not extract a Greenhouse board/job id from: {url!r}")


def parse_payload(
    payload: Dict[str, Any], *, url: str, company_fallback: Optional[str] = None
) -> Dict[str, Any]:
    """Normalize a Job Board API job payload (pure — no network)."""
    location = payload.get("location") or {}
    content = payload.get("content")
    return posting(
        portal=PORTAL,
        portal_label=LABEL,
        canonical_url=payload.get("absolute_url") or url,
        title=payload.get("title"),
        company=payload.get("company_name") or company_fallback,
        location=location.get("name") if isinstance(location, dict) else None,
        description_text=strip_html(html.unescape(content)) if content else None,
        apply_url=payload.get("absolute_url") or url,
        apply_type="direct",
    )


def fetch_job(url: str, *, timeout: int = scraper.DEFAULT_TIMEOUT) -> Dict[str, Any]:
    board, job_id, region = board_and_job(url)
    payload = fetch_json(
        f"{_API_BASE[region]}/{board}/jobs/{job_id}", timeout=timeout
    )
    # The board token is a readable company slug — good enough when the
    # payload omits company_name.
    fallback = board.replace("-", " ").title()
    return parse_payload(payload, url=url, company_fallback=fallback)
