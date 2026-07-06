"""LinkedIn portal adapter.

Thin wrapper that exposes the battle-tested LinkedIn scraper
(:func:`pushcv.scraper.fetch_linkedin_job`) through the normalized portal
interface.
"""
from __future__ import annotations

from typing import Any, Dict

from pushcv import scraper
from pushcv.portals.base import host_of, posting

PORTAL = "linkedin"
LABEL = "LinkedIn"


def matches(url: str) -> bool:
    host = host_of(url)
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def fetch_job(url: str, *, timeout: int = scraper.DEFAULT_TIMEOUT) -> Dict[str, Any]:
    data = scraper.fetch_linkedin_job(url, timeout=timeout)
    return posting(
        portal=PORTAL,
        portal_label=LABEL,
        canonical_url=data["original_linkedin_url"],
        title=data["title"],
        company=data["company"],
        location=data["location"],
        description_text=data["description_text"],
        apply_url=data["apply_url"],
        apply_type=data["apply_type"],
    )
