"""Shared plumbing for portal scrapers.

Every portal module returns the same normalized posting dict (see
:func:`posting`), so the CLI can render and persist any portal's result
through one code path.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from pushcv import scraper


def host_of(url: str) -> str:
    """Return the lowercase hostname of ``url`` ('' when unparseable)."""
    return (urlsplit(url).hostname or "").lower()


def fetch_json(url: str, *, timeout: int = scraper.DEFAULT_TIMEOUT) -> Any:
    """GET ``url`` (browser-impersonated) and decode the JSON body.

    Raises :class:`ValueError` when the body is not valid JSON — which is how
    an ATS signals an unknown company/posting id even on some 200 responses.
    """
    body = scraper.fetch(url, timeout=timeout)
    return json.loads(body)


def strip_html(html_text: Optional[str]) -> Optional[str]:
    """Reduce an HTML fragment to clean plain text (None when empty)."""
    if not isinstance(html_text, str) or not html_text.strip():
        return None
    text = BeautifulSoup(html_text, "html.parser").get_text(separator=" ")
    cleaned = " ".join(text.split())
    return cleaned or None


def posting(
    *,
    portal: str,
    portal_label: str,
    canonical_url: str,
    title: Optional[str] = None,
    company: Optional[str] = None,
    location: Optional[str] = None,
    description_text: Optional[str] = None,
    apply_url: Optional[str] = None,
    apply_type: str = "direct",
) -> Dict[str, Any]:
    """Build the normalized posting dict shared by every portal.

    ``apply_type`` is ``"direct"`` (the application form lives at
    ``apply_url``/the posting page), or one of LinkedIn's ``"offsite"``,
    ``"offsite_gated"``, ``"easy_apply"``, or ``"unknown"`` for the generic
    fallback.
    """
    return {
        "portal": portal,
        "portal_label": portal_label,
        "canonical_url": canonical_url,
        "title": title,
        "company": company,
        "location": location,
        "description_text": description_text,
        "apply_url": apply_url,
        "apply_type": apply_type,
    }
