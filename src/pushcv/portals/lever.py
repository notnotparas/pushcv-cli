"""Lever portal adapter.

Lever-hosted postings expose a public, unauthenticated Postings API:

    GET https://api.lever.co/v0/postings/{company}/{posting_id}

Hosted posting URLs look like:

* https://jobs.lever.co/{company}/{posting-uuid}
* https://jobs.lever.co/{company}/{posting-uuid}/apply
* https://jobs.eu.lever.co/... (served by api.eu.lever.co)

``descriptionPlain`` covers the opening/body, but the requirement blocks live
separately in ``lists`` (HTML) and ``additional``/``additionalPlain``; all are
folded into one description.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple
from urllib.parse import urlsplit

from pushcv import scraper
from pushcv.portals.base import fetch_json, host_of, posting, strip_html

PORTAL = "lever"
LABEL = "Lever"

_HOSTS = {
    "jobs.lever.co": "https://api.lever.co/v0/postings",
    "jobs.eu.lever.co": "https://api.eu.lever.co/v0/postings",
}

# /{company}/{uuid}, optionally followed by /apply.
_PATH_RE = re.compile(
    r"^/(?P<company>[^/]+)/(?P<posting_id>[0-9a-fA-F-]{16,})(?:/apply)?/?$"
)


def matches(url: str) -> bool:
    return host_of(url) in _HOSTS


def company_and_posting(url: str) -> Tuple[str, str, str]:
    """Extract ``(company_slug, posting_id, api_base)`` from a Lever URL.

    Raises :class:`ValueError` when the URL doesn't carry both identifiers.
    """
    split = urlsplit(url)
    api_base = _HOSTS.get((split.hostname or "").lower())
    match = _PATH_RE.match(split.path)
    if api_base is None or match is None:
        raise ValueError(f"Could not extract a Lever company/posting id from: {url!r}")
    return match.group("company"), match.group("posting_id"), api_base


def _description(payload: Dict[str, Any]) -> str:
    """Fold opening text, requirement lists, and closing text into one string."""
    parts: List[str] = []
    opening = payload.get("descriptionPlain") or strip_html(payload.get("description"))
    if opening:
        parts.append(opening.strip())
    for block in payload.get("lists") or []:
        if not isinstance(block, dict):
            continue
        heading = (block.get("text") or "").strip()
        body = strip_html(block.get("content"))
        if heading or body:
            parts.append(" ".join(p for p in (heading, body) if p))
    closing = payload.get("additionalPlain") or strip_html(payload.get("additional"))
    if closing:
        parts.append(closing.strip())
    return "\n\n".join(parts)


def parse_payload(
    payload: Dict[str, Any], *, url: str, company_fallback: str = ""
) -> Dict[str, Any]:
    """Normalize a Postings API payload (pure — no network)."""
    categories = payload.get("categories") or {}
    location = categories.get("location") if isinstance(categories, dict) else None
    workplace = payload.get("workplaceType")
    if location and workplace == "remote" and "remote" not in location.lower():
        location = f"{location} (Remote)"

    canonical = payload.get("hostedUrl") or url
    return posting(
        portal=PORTAL,
        portal_label=LABEL,
        canonical_url=canonical,
        title=payload.get("text"),
        # The Postings API carries no company display name; the URL slug is
        # the best available signal.
        company=company_fallback.replace("-", " ").title() or None,
        location=location,
        description_text=_description(payload) or None,
        apply_url=payload.get("applyUrl") or canonical,
        apply_type="direct",
    )


def fetch_job(url: str, *, timeout: int = scraper.DEFAULT_TIMEOUT) -> Dict[str, Any]:
    company, posting_id, api_base = company_and_posting(url)
    # mode=json is what forces JSON on Lever's embeddable endpoints; the single
    # posting route returns JSON either way, but be explicit.
    payload = fetch_json(
        f"{api_base}/{company}/{posting_id}?mode=json", timeout=timeout
    )
    return parse_payload(payload, url=url, company_fallback=company)
