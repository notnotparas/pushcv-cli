"""Multi-portal job scraping for pushcv.

A small registry dispatches a posting URL to the portal that owns it
(LinkedIn, Greenhouse, Lever, SmartRecruiters), with a generic JSON-LD /
OpenGraph fallback for everything else. Every portal returns the same
normalized dict (see :func:`pushcv.portals.base.posting`), so the CLI renders
and persists all of them through one code path.

Public surface:

* :func:`detect` — which registered portal owns a URL (None if none do).
* :func:`scrape_job` — fetch + normalize a posting from any supported URL.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from pushcv import scraper
from pushcv.portals import generic, greenhouse, lever, linkedin, smartrecruiters

# Registered portals, checked in order. `generic` is deliberately not listed —
# it is the explicit fallback, never a positive match.
PORTALS = (linkedin, greenhouse, lever, smartrecruiters)

# Fields the LinkedIn→ATS chain-scrape may fill in (never overwrite).
_CHAIN_FIELDS = ("title", "company", "location", "description_text")


def detect(url: str):
    """Return the portal module that owns ``url``, or None."""
    for portal in PORTALS:
        if portal.matches(url):
            return portal
    return None


def scrape_job(url: str, *, timeout: int = scraper.DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """Fetch and normalize the job posting at ``url``.

    Dispatches to the matching portal (falling back to the generic JSON-LD
    parser for unknown hosts). When a LinkedIn posting resolves to an off-site
    apply URL on a supported ATS, that ATS is chain-scraped and its richer,
    canonical data fills any gaps LinkedIn left (best-effort — a chain failure
    never degrades the primary result).

    Raises :class:`ValueError` for URLs a portal claims but cannot parse, and
    lets network/HTTP errors propagate.
    """
    portal = detect(url) or generic
    result = portal.fetch_job(url, timeout=timeout)

    apply_url = result.get("apply_url")
    if result["portal"] == linkedin.PORTAL and apply_url:
        target = detect(apply_url)
        if target is not None and target.PORTAL != linkedin.PORTAL:
            chained = _chain_scrape(target, apply_url, timeout=timeout)
            if chained:
                for key in _CHAIN_FIELDS:
                    if not result.get(key) and chained.get(key):
                        result[key] = chained[key]
                # The ATS description is canonical and untruncated — prefer it
                # when it is meaningfully fuller than LinkedIn's.
                ats_desc = chained.get("description_text")
                own_desc = result.get("description_text")
                if ats_desc and (not own_desc or len(ats_desc) > len(own_desc)):
                    result["description_text"] = ats_desc
    return result


def _chain_scrape(portal, url: str, *, timeout: int) -> Optional[Dict[str, Any]]:
    """Best-effort secondary scrape; None on any failure."""
    try:
        return portal.fetch_job(url, timeout=timeout)
    except Exception:
        return None
