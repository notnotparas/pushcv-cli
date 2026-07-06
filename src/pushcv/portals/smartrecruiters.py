"""SmartRecruiters portal adapter.

SmartRecruiters exposes a public, unauthenticated Posting API:

    GET https://api.smartrecruiters.com/v1/companies/{company}/postings/{id}

Hosted posting URLs look like:

* https://jobs.smartrecruiters.com/{CompanyIdentifier}/{postingId}-{slug}
* https://jobs.smartrecruiters.com/{CompanyIdentifier}/{postingId}

where ``postingId`` is the long leading digit run of the last path segment.
The job ad arrives as titled HTML sections (company description, job
description, qualifications, additional information) that are flattened into
one plain-text description.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from pushcv import scraper
from pushcv.portals.base import fetch_json, host_of, posting, strip_html

PORTAL = "smartrecruiters"
LABEL = "SmartRecruiters"

_HOST = "jobs.smartrecruiters.com"
_API_BASE = "https://api.smartrecruiters.com/v1/companies"

_PATH_RE = re.compile(r"^/(?P<company>[^/]+)/(?P<posting_id>\d{6,})(?:-|/|$)")

# jobAd section keys in their on-page display order.
_SECTION_ORDER = (
    "companyDescription",
    "jobDescription",
    "qualifications",
    "additionalInformation",
)


def matches(url: str) -> bool:
    return host_of(url) == _HOST


def company_and_posting(url: str) -> Tuple[str, str]:
    """Extract ``(company_identifier, posting_id)`` from a SmartRecruiters URL.

    Raises :class:`ValueError` when the URL doesn't carry both identifiers.
    """
    match = _PATH_RE.match(urlsplit(url).path)
    if match is None:
        raise ValueError(
            f"Could not extract a SmartRecruiters company/posting id from: {url!r}"
        )
    return match.group("company"), match.group("posting_id")


def _location(loc: Any) -> Optional[str]:
    """Render the posting's location object as a readable string."""
    if not isinstance(loc, dict):
        return None
    country = loc.get("country")
    parts = [
        loc.get("city"),
        loc.get("region"),
        country.upper() if isinstance(country, str) and len(country) <= 3 else country,
    ]
    joined = ", ".join(p for p in parts if isinstance(p, str) and p.strip())
    if loc.get("remote"):
        joined = f"{joined} (Remote)" if joined else "Remote"
    return joined or None


def _description(payload: Dict[str, Any]) -> Optional[str]:
    """Flatten the jobAd HTML sections into one plain-text description."""
    sections = (payload.get("jobAd") or {}).get("sections") or {}
    parts: List[str] = []
    for key in _SECTION_ORDER:
        section = sections.get(key)
        if not isinstance(section, dict):
            continue
        text = strip_html(section.get("text"))
        if text:
            parts.append(text)
    return "\n\n".join(parts) or None


def parse_payload(payload: Dict[str, Any], *, url: str) -> Dict[str, Any]:
    """Normalize a Posting API payload (pure — no network)."""
    company = payload.get("company") or {}
    company_name = company.get("name") if isinstance(company, dict) else None
    return posting(
        portal=PORTAL,
        portal_label=LABEL,
        canonical_url=url,
        title=payload.get("name"),
        company=company_name,
        location=_location(payload.get("location")),
        description_text=_description(payload),
        apply_url=payload.get("applyUrl") or url,
        apply_type="direct",
    )


def fetch_job(url: str, *, timeout: int = scraper.DEFAULT_TIMEOUT) -> Dict[str, Any]:
    company, posting_id = company_and_posting(url)
    payload = fetch_json(f"{_API_BASE}/{company}/postings/{posting_id}", timeout=timeout)
    result = parse_payload(payload, url=url)
    if not result["company"]:
        result["company"] = company.replace("-", " ").title()
    return result
