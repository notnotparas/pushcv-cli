"""Generic fallback for job pages on unrecognized hosts.

Most ATS-hosted postings (Ashby, Workable, Teamtailor, company career sites…)
embed a schema.org ``JobPosting`` JSON-LD block, so a best-effort structured
parse usually succeeds even without a dedicated adapter. When no JSON-LD is
present, OpenGraph/title metadata still yields a usable title/company.
"""
from __future__ import annotations

from typing import Any, Dict

from bs4 import BeautifulSoup

from pushcv import scraper
from pushcv.portals.base import posting
from pushcv.scraper import (
    _find_job_posting,
    _iter_json_ld,
    _job_location,
    _org_name,
    _strip_html,
)

PORTAL = "generic"
LABEL = "Web"  # rendered as "Web posting" in the fetch panel


def matches(url: str) -> bool:  # catch-all — used only as the explicit fallback
    return True


def parse_html(html_text: str, *, url: str) -> Dict[str, Any]:
    """Extract a normalized posting from page HTML (pure — no network)."""
    soup = BeautifulSoup(html_text, "html.parser")
    job = _find_job_posting(_iter_json_ld(soup)) or {}

    # OpenGraph/<title>/<h1> fallback for anything JSON-LD didn't provide.
    meta = scraper.parse(html_text, url=url)

    title = job.get("title")
    return posting(
        portal=PORTAL,
        portal_label=LABEL,
        canonical_url=url,
        title=(title.strip() if isinstance(title, str) else None) or meta.title,
        company=_org_name(job.get("hiringOrganization")) or meta.company,
        location=_job_location(job.get("jobLocation")),
        description_text=_strip_html(job.get("description")) or meta.description,
        apply_url=None,
        apply_type="unknown",
    )


def fetch_job(url: str, *, timeout: int = scraper.DEFAULT_TIMEOUT) -> Dict[str, Any]:
    html_text = scraper.fetch(url, timeout=timeout)
    return parse_html(html_text, url=url)
